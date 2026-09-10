from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

from fastapi import HTTPException, status
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from app.audit import AuditLogger
from app.database import Database
from app.models import Principal, ToolResult
from app.rag import KnowledgeBase
from app.security import permissions_for


@dataclass(slots=True)
class ToolContext:
    principal: Principal
    database: Database
    knowledge_base: KnowledgeBase
    audit: AuditLogger
    ip_address: str


ToolHandler = Callable[[ToolContext, dict[str, Any]], ToolResult]


class PolicySearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=4, ge=1, le=6)


class AnnualLeaveInput(BaseModel):
    service_years: float = Field(ge=0, le=80)


class CreateHrTicketInput(BaseModel):
    category: str = Field(default="制度咨询", max_length=50)
    subject: str = Field(max_length=100)
    description: str = Field(max_length=2000)


class EmptyToolInput(BaseModel):
    pass


class SubmitLeaveInput(BaseModel):
    leave_type: str = Field(default="年假", max_length=30)
    start_date: str = Field(max_length=20)
    end_date: str = Field(max_length=20)
    reason: str = Field(default="未填写", max_length=500)


class EscalateToHumanInput(BaseModel):
    question: str = Field(max_length=2000)
    reason: str = Field(default="用户主动要求人工处理", max_length=300)


@dataclass(slots=True)
class ToolDefinition:
    name: str
    description: str
    required_permission: str
    risk_level: str
    args_schema: type[BaseModel]
    handler: ToolHandler


class ToolRegistry:
    """工具调用核心：所有工具必须先过权限门，再记录输入摘要与执行结果。"""

    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        self._tools[definition.name] = definition

    def required_permission(self, name: str) -> str | None:
        """返回工具所需权限，工具不存在时返回 None。用于生成可读的越权提示。"""
        tool = self._tools.get(name)
        return tool.required_permission if tool else None

    def describe_for(self, principal: Principal) -> list[dict[str, str]]:
        allowed = permissions_for(principal.role)
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "risk_level": tool.risk_level,
            }
            for tool in self._tools.values()
            if tool.required_permission in allowed
        ]

    def execute(self, name: str, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"工具不存在：{name}")
        if tool.required_permission not in permissions_for(context.principal.role):
            context.audit.log(
                context.principal, "tool.denied", name, "denied",
                {"required_permission": tool.required_permission}, context.ip_address,
            )
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="没有调用该工具的权限")
        context.audit.log(
            context.principal, "tool.invoke", name, "started",
            {"risk_level": tool.risk_level, "argument_keys": sorted(arguments)}, context.ip_address,
        )
        try:
            result = tool.handler(context, arguments)
        except Exception as exc:
            context.audit.log(
                context.principal, "tool.invoke", name, "failed",
                {"error_type": type(exc).__name__}, context.ip_address,
            )
            raise
        context.audit.log(
            context.principal, "tool.invoke", name, "success" if result.ok else "rejected",
            {"result_keys": sorted(result.data)}, context.ip_address,
        )
        return result

    def as_langchain_tools(self, context: ToolContext) -> dict[str, BaseTool]:
        """将企业工具转换为 LangChain StructuredTool，同时复用权限和审计执行入口。"""
        tools: dict[str, BaseTool] = {}
        for definition in self._tools.values():
            if definition.required_permission not in permissions_for(context.principal.role):
                continue

            def invoke_registered_tool(
                _tool_name: str = definition.name,
                **arguments: Any,
            ) -> ToolResult:
                return self.execute(_tool_name, context, arguments)

            tools[definition.name] = StructuredTool.from_function(
                func=invoke_registered_tool,
                name=definition.name,
                description=definition.description,
                args_schema=definition.args_schema,
            )
        return tools


def build_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        "policy_search", "检索员工守则并返回原文引用", "policy.read", "low", PolicySearchInput, _policy_search,
    ))
    registry.register(ToolDefinition(
        "calculate_annual_leave", "根据累计工作年限计算法定年假天数", "policy.read", "low", AnnualLeaveInput, _calculate_annual_leave,
    ))
    registry.register(ToolDefinition(
        "create_hr_ticket", "创建人力资源咨询工单", "ticket.create", "medium", CreateHrTicketInput, _create_hr_ticket,
    ))
    registry.register(ToolDefinition(
        "get_my_tickets", "查看本人创建的人力工单", "ticket.read_own", "low", EmptyToolInput, _get_my_tickets,
    ))
    registry.register(ToolDefinition(
        "submit_leave_request", "提交请假申请并进入人工审批", "leave.request", "medium", SubmitLeaveInput, _submit_leave_request,
    ))
    registry.register(ToolDefinition(
        "escalate_to_human", "按用户要求将问题转交人力专员", "chat.use", "medium", EscalateToHumanInput, _escalate_to_human,
    ))
    return registry


def _policy_search(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    query = str(arguments.get("query", "")).strip()
    top_k = int(arguments.get("top_k", 4))
    hits = context.knowledge_base.search(query, top_k=max(1, min(top_k, 6)))
    citations = [hit.to_citation() for hit in hits]
    return ToolResult(bool(hits), "检索完成" if hits else "未找到足够相关的制度条款", citations=citations)


def _calculate_annual_leave(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    years = float(arguments.get("service_years", 0))
    if years < 1:
        days = 0
    elif years < 10:
        days = 5
    elif years < 20:
        days = 10
    else:
        days = 15
    hits = context.knowledge_base.search(
        "带薪年假 累计工作 年假天数",
        top_k=1,
        min_score=0.01,
        min_coverage=0.05,
    )
    return ToolResult(
        True,
        f"按累计工作年限 {years:g} 年计算，全年年假标准为 {days} 天。"
        "当年入职还需按实际在职月份折算。",
        {"service_years": years, "annual_leave_days": days},
        [hit.to_citation() for hit in hits],
    )


def _create_hr_ticket(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    category = str(arguments.get("category", "制度咨询"))[:50]
    subject = str(arguments.get("subject", "员工咨询"))[:100]
    description = str(arguments.get("description", ""))[:2000]
    ticket_id = context.database.execute(
        """
        INSERT INTO hr_tickets (creator_id, category, subject, description, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (context.principal.user_id, category, subject, description, datetime.now(UTC).isoformat()),
    )
    return ToolResult(True, f"人力工单 #{ticket_id} 已创建。", {"ticket_id": ticket_id, "status": "open"})


def _get_my_tickets(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    rows = context.database.fetch_all(
        "SELECT id, category, subject, status, created_at FROM hr_tickets WHERE creator_id=? ORDER BY id DESC LIMIT 20",
        (context.principal.user_id,),
    )
    tickets = [dict(row) for row in rows]
    return ToolResult(True, f"共找到 {len(tickets)} 条本人工单。", {"tickets": tickets})


def _submit_leave_request(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    leave_type = str(arguments.get("leave_type", "年假"))[:30]
    start_date = str(arguments.get("start_date", ""))[:20]
    end_date = str(arguments.get("end_date", ""))[:20]
    reason = str(arguments.get("reason", "未填写"))[:500]
    if not start_date or not end_date:
        return ToolResult(False, "提交请假申请需要开始日期和结束日期。")
    request_id = context.database.execute(
        """
        INSERT INTO leave_requests
        (creator_id, leave_type, start_date, end_date, reason, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            context.principal.user_id, leave_type, start_date, end_date, reason,
            datetime.now(UTC).isoformat(),
        ),
    )
    # 中风险动作不由 AI 最终批准：只提交申请，最终决定保留给直属主管/HR。
    return ToolResult(
        True,
        f"请假申请 #{request_id} 已提交，当前状态：等待人工审批。",
        {"request_id": request_id, "status": "pending_human_approval"},
    )


def _escalate_to_human(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    question = str(arguments.get("question", ""))[:2000]
    reason = str(arguments.get("reason", "AI 置信度不足"))[:300]
    case_id = context.database.execute(
        """
        INSERT INTO human_cases (creator_id, question, reason, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (context.principal.user_id, question, reason, datetime.now(UTC).isoformat()),
    )
    return ToolResult(
        True,
        f"已转交人力专员，人工处理单 #{case_id}。请保留编号以便跟进。",
        {"case_id": case_id, "status": "pending"},
    )
