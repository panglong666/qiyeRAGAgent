from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any, Literal, TypedDict

from fastapi import HTTPException
from langchain_core.documents import Document
from langgraph.graph import END, START, StateGraph

from app.audit import AuditLogger
from app.config import Settings
from app.database import Database
from app.llm import GroundedAnswerGenerator
from app.models import AgentStep, Citation, Principal, ToolResult
from app.rag import KnowledgeBase, SearchHit
from app.tools import ToolContext, ToolRegistry


UNKNOWN_ANSWER = "我不知道，知识库中没有相关内容。"
GraphRoute = Literal["answer", "unknown", "tool"]


class AgentState(TypedDict, total=False):
    """LangGraph 在各节点间传递的企业 Agent 状态。"""

    principal: Principal
    ip_address: str
    question: str
    hits: list[SearchHit]
    documents: list[Document]
    citations: list[Citation]
    route: GraphRoute
    tool_name: str
    tool_arguments: dict[str, Any]
    tool_result: ToolResult
    approval_status: str
    escalated: bool
    answer: str
    steps: list[AgentStep]


class EnterpriseAgent:
    """LangGraph Agent：检索 → 判断 → 工具调用 → 审批/人工兜底 → 回答。"""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        knowledge_base: KnowledgeBase,
        tools: ToolRegistry,
        audit: AuditLogger,
        generator: GroundedAnswerGenerator | None = None,
    ):
        self.settings = settings
        self.database = database
        self.knowledge_base = knowledge_base
        self.tools = tools
        self.audit = audit
        self.generator = generator or GroundedAnswerGenerator(settings)
        self.graph = self._build_graph()

    def _build_graph(self):
        workflow = StateGraph(AgentState)
        workflow.add_node("retrieve", self._retrieve_node)
        workflow.add_node("judge", self._judge_node)
        workflow.add_node("tool_call", self._tool_call_node)
        workflow.add_node("approval_human", self._approval_human_node)
        workflow.add_node("answer", self._answer_node)
        workflow.add_edge(START, "retrieve")
        workflow.add_edge("retrieve", "judge")
        workflow.add_conditional_edges(
            "judge",
            self._route_after_judge,
            {"answer": "answer", "unknown": "answer", "tool": "tool_call"},
        )
        workflow.add_edge("tool_call", "approval_human")
        workflow.add_edge("approval_human", "answer")
        workflow.add_edge("answer", END)
        return workflow.compile()

    def run(self, principal: Principal, message: str, ip_address: str) -> dict[str, Any]:
        message = message.strip()
        self.audit.log(principal, "chat.query", "assistant", "received", {"query": message}, ip_address)
        final_state = self.graph.invoke(
            {
                "principal": principal,
                "ip_address": ip_address,
                "question": message,
                "steps": [],
                "citations": [],
                "documents": [],
                "escalated": False,
            }
        )
        citations = final_state.get("citations", [])
        tool_name = final_state.get("tool_name", "policy_search")
        escalated = final_state.get("escalated", False)
        self.audit.log(
            principal,
            "chat.answer",
            "assistant",
            "success",
            {
                "framework": "langgraph",
                "tool": tool_name,
                "citation_count": len(citations),
                "escalated": escalated,
            },
            ip_address,
        )
        return {
            "answer": final_state.get("answer", UNKNOWN_ANSWER),
            "citations": [asdict(item) for item in citations],
            "steps": [asdict(item) for item in final_state.get("steps", [])],
            "tool": tool_name,
            "escalated": escalated,
        }

    def _retrieve_node(self, state: AgentState) -> AgentState:
        hits = self.knowledge_base.retrieve(state["question"], self.settings.rag_top_k)
        self._audit_graph_node(state, "retrieve", {"candidate_count": len(hits)})
        return {
            "hits": hits,
            "steps": self._append_steps(
                state,
                AgentStep("act", f"LangChain BM25 检索到 {len(hits)} 个候选知识块", "policy_search"),
            ),
        }

    def _judge_node(self, state: AgentState) -> AgentState:
        operation = self._plan_operation(state["question"])
        if operation is not None:
            tool_name, arguments, detail = operation
            self._audit_graph_node(state, "judge", {"route": "tool", "tool": tool_name})
            return {
                "route": "tool",
                "tool_name": tool_name,
                "tool_arguments": arguments,
                "steps": self._append_steps(state, AgentStep("plan", detail, tool_name)),
            }

        qualified_hits = [
            hit
            for hit in state.get("hits", [])
            if hit.score >= self.settings.rag_min_score
            and hit.coverage >= self.settings.rag_min_coverage
        ]
        route: GraphRoute = "answer" if qualified_hits else "unknown"
        citations = [hit.to_citation() for hit in qualified_hits]
        documents = [hit.document for hit in qualified_hits]
        top_score = qualified_hits[0].score if qualified_hits else 0.0
        top_coverage = qualified_hits[0].coverage if qualified_hits else 0.0
        self._audit_graph_node(
            state,
            "judge",
            {
                "route": route,
                "qualified_count": len(qualified_hits),
                "top_score": round(top_score, 4),
                "top_coverage": round(top_coverage, 4),
            },
        )
        detail = (
            f"知识库依据充足，命中 {len(qualified_hits)} 条制度片段"
            if qualified_hits
            else "知识库依据不足，按规则直接回复不知道"
        )
        return {
            "route": route,
            "tool_name": "policy_search",
            "documents": documents,
            "citations": citations,
            "steps": self._append_steps(state, AgentStep("observe", detail, "policy_search")),
        }

    @staticmethod
    def _route_after_judge(state: AgentState) -> GraphRoute:
        return state["route"]

    def _tool_call_node(self, state: AgentState) -> AgentState:
        context = ToolContext(
            principal=state["principal"],
            database=self.database,
            knowledge_base=self.knowledge_base,
            audit=self.audit,
            ip_address=state["ip_address"],
        )
        langchain_tools = self.tools.as_langchain_tools(context)
        tool_name = state["tool_name"]
        tool = langchain_tools.get(tool_name)
        arguments = state.get("tool_arguments", {})
        try:
            if tool is None:
                # 仍从原权限入口执行一次，以获得一致的 403 和审计记录。
                result = self.tools.execute(tool_name, context, arguments)
            else:
                result = tool.invoke(arguments)
        except HTTPException as exc:
            # 越权不能中断整轮对话：转成自然语言提示，让流程继续走到 answer 节点，
            # 否则 chat.answer 审计缺失，审计链会出现有头无尾的记录。
            required = self.tools.required_permission(tool_name)
            role = state["principal"].role
            result = ToolResult(
                False,
                f"{exc.detail}。当前角色为 {role}，该操作需要 {required or '更高'} 权限。"
                "请改用有权限的账号登录，或回复“转人工”由人力专员协助处理。",
            )
        except Exception as exc:
            self.audit.log(
                state["principal"], "tool.error", tool_name, "failed",
                {"error_type": type(exc).__name__}, state["ip_address"],
            )
            result = ToolResult(
                False,
                f"工具 {tool_name} 执行失败（{type(exc).__name__}）。请稍后重试，或回复“转人工”由人力专员协助处理。",
            )
        if not isinstance(result, ToolResult):
            raise TypeError(f"LangChain 工具 {tool_name} 返回了无效结果")
        self._audit_graph_node(state, "tool_call", {"tool": tool_name, "ok": result.ok})
        return {
            "tool_result": result,
            "citations": result.citations,
            "steps": self._append_steps(
                state,
                AgentStep("act", f"通过 LangChain StructuredTool 调用：{tool_name}", tool_name),
                AgentStep("observe", result.content, tool_name),
            ),
        }

    def _approval_human_node(self, state: AgentState) -> AgentState:
        tool_name = state["tool_name"]
        escalated = tool_name == "escalate_to_human"
        result_ok = state["tool_result"].ok
        approval_status = (
            "pending_human_approval"
            if tool_name == "submit_leave_request" and result_ok
            else "not_required"
        )
        self._audit_graph_node(
            state,
            "approval_human",
            {"tool": tool_name, "approval_status": approval_status, "escalated": escalated},
        )
        extra_steps: list[AgentStep] = []
        if approval_status == "pending_human_approval":
            extra_steps.append(AgentStep("observe", "该业务动作必须等待 HR 人工审批", tool_name))
        elif escalated:
            extra_steps.append(AgentStep("observe", "用户已明确要求转人工处理", tool_name))
        return {
            "approval_status": approval_status,
            "escalated": escalated,
            "steps": self._append_steps(state, *extra_steps),
        }

    def _answer_node(self, state: AgentState) -> AgentState:
        if state["route"] == "unknown":
            answer = UNKNOWN_ANSWER
        elif state["route"] == "tool":
            answer = state["tool_result"].content
        else:
            answer = self._generate_grounded_answer(state)
        self._audit_graph_node(
            state,
            "answer",
            {"route": state["route"], "used_deepseek": state["route"] == "answer" and self.generator.enabled},
        )
        return {
            "answer": answer,
            "steps": self._append_steps(state, AgentStep("observe", "LangGraph 已生成最终响应", state.get("tool_name"))),
        }

    def _generate_grounded_answer(self, state: AgentState) -> str:
        principal = state["principal"]
        ip_address = state["ip_address"]
        if self.generator.enabled:
            self.audit.log(
                principal,
                "llm.generate",
                "deepseek",
                "started",
                {"model": self.settings.deepseek_model, "document_count": len(state["documents"])},
                ip_address,
            )
        try:
            generated = self.generator.generate(state["question"], state["documents"])
            if generated:
                if self.generator.enabled:
                    self.audit.log(
                        principal,
                        "llm.generate",
                        "deepseek",
                        "success",
                        {"model": self.settings.deepseek_model},
                        ip_address,
                    )
                return generated
        except Exception as exc:
            self.audit.log(
                principal,
                "llm.generate",
                "deepseek",
                "failed",
                {"model": self.settings.deepseek_model, "error_type": type(exc).__name__},
                ip_address,
            )
        # DeepSeek 不可用时只返回已命中的原文，不使用模型自身知识。
        lines = ["DeepSeek 暂时不可用，以下为知识库中的相关原文："]
        for index, citation in enumerate(state.get("citations", [])[:3], start=1):
            lines.append(f"{index}. {citation.quote} [{index}]")
        return "\n".join(lines)

    def _plan_operation(self, message: str) -> tuple[str, dict[str, Any], str] | None:
        """安全敏感的业务路由采用确定性规则，不交给大模型自由决定。"""
        if any(keyword in message for keyword in ("转人工", "人工客服", "找人力", "联系HR", "联系 hr")):
            return "escalate_to_human", {"question": message, "reason": "用户主动要求人工处理"}, "用户明确要求转人工"

        if any(keyword in message for keyword in ("我的工单", "工单进度", "查看工单")):
            return "get_my_tickets", {}, "查询本人已创建的人力工单"

        if "工单" in message and any(keyword in message for keyword in ("创建", "提交", "新建")):
            return (
                "create_hr_ticket",
                {"category": "制度咨询", "subject": message[:50], "description": message},
                "创建可跟踪的人力咨询工单",
            )

        if any(keyword in message for keyword in ("申请请假", "提交请假", "请假申请")):
            dates = re.findall(r"20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?", message)
            normalized = [re.sub(r"[年月/.]", "-", date).rstrip("日-") for date in dates]
            leave_type = next(
                (kind for kind in ("年假", "病假", "事假", "婚假", "产假", "陪产假", "丧假") if kind in message),
                "年假",
            )
            return (
                "submit_leave_request",
                {
                    "leave_type": leave_type,
                    "start_date": normalized[0] if normalized else "",
                    "end_date": normalized[1] if len(normalized) > 1 else (normalized[0] if normalized else ""),
                    "reason": message,
                },
                "识别为请假办理，提交后交由 HR 人工审批",
            )

        if "年假" in message and any(keyword in message for keyword in ("几天", "多少天", "工作满", "工龄")):
            match = re.search(r"(?:工作|工龄|满)?\s*(\d+(?:\.\d+)?)\s*年", message)
            if match:
                return (
                    "calculate_annual_leave",
                    {"service_years": float(match.group(1))},
                    "识别工作年限并调用年假计算工具",
                )
        return None

    def _audit_graph_node(self, state: AgentState, node: str, detail: dict[str, Any]) -> None:
        self.audit.log(
            state["principal"],
            "graph.node",
            node,
            "success",
            detail,
            state["ip_address"],
        )

    @staticmethod
    def _append_steps(state: AgentState, *steps: AgentStep) -> list[AgentStep]:
        return [*state.get("steps", []), *steps]
