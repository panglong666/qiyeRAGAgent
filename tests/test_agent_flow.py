"""B. 路由与拒答：检索 → 判断 → 工具调用 三路分流，以及无依据时的固定拒答。"""
from __future__ import annotations

import pytest

from app.agent import UNKNOWN_ANSWER, EnterpriseAgent
from app.database import Database
from app.models import Principal

# 实测可通过双阈值、且能稳定回答的问题
GROUNDED_QUESTION = "迟到超过30分钟如何处理？"
UNKNOWN_QUESTION = "今天天气怎么样"


def run_graph(agent: EnterpriseAgent, principal: Principal, message: str) -> dict:
    """直接驱动 LangGraph 状态图，用于断言内部 route 取值。"""
    return agent.graph.invoke(
        {
            "principal": principal,
            "ip_address": "127.0.0.1",
            "question": message,
            "steps": [],
            "citations": [],
            "documents": [],
            "escalated": False,
        }
    )


def test_grounded_question_routes_to_answer_with_citations(make_agent, employee, fake_generator) -> None:
    generator = fake_generator("迟到超过30分钟可按旷工半日处理。[1]")
    agent = make_agent(generator=generator)

    state = run_graph(agent, employee, GROUNDED_QUESTION)
    assert state["route"] == "answer"
    assert state["documents"], "answer 路由必须带检索到的制度依据"
    assert generator.calls == 1

    result = agent.run(employee, GROUNDED_QUESTION, "127.0.0.1")
    assert result["answer"] == "迟到超过30分钟可按旷工半日处理。[1]"
    assert result["citations"]
    assert result["tool"] == "policy_search"
    # agent.run 会再跑一轮状态图，因此生成器被调用第二次
    assert generator.calls == 2


@pytest.mark.parametrize("question", [UNKNOWN_QUESTION, "帮我订一张机票"])
def test_no_evidence_routes_to_unknown_without_calling_llm(
    make_agent, employee, fake_generator, question
) -> None:
    generator = fake_generator("这条回答不应该出现")
    agent = make_agent(generator=generator)

    state = run_graph(agent, employee, question)
    assert state["route"] == "unknown"

    result = agent.run(employee, question, "127.0.0.1")
    assert result["answer"] == UNKNOWN_ANSWER
    assert result["citations"] == []
    assert result["escalated"] is False
    # 依据不足时不得调用大模型
    assert generator.calls == 0


def test_business_intent_routes_to_tool(agent, employee) -> None:
    state = run_graph(agent, employee, "工作满 8 年有几天年假？")
    assert state["route"] == "tool"
    assert state["tool_name"] == "calculate_annual_leave"

    result = agent.run(employee, "工作满 8 年有几天年假？", "127.0.0.1")
    assert result["tool"] == "calculate_annual_leave"
    assert "5 天" in result["answer"]


def test_consultation_and_application_are_distinguished(
    agent: EnterpriseAgent, employee: Principal, database: Database
) -> None:
    # 咨询：问年假天数 → 走计算工具，不产生请假单
    consultation = agent.run(employee, "工作满 8 年有几天年假？", "127.0.0.1")
    assert consultation["tool"] == "calculate_annual_leave"
    assert database.fetch_one("SELECT id FROM leave_requests LIMIT 1") is None

    # 申请：给出明确日期 → 走请假提交工具，只进入待人工审批
    application = agent.run(employee, "我要申请请假 2026-08-10 到 2026-08-12", "127.0.0.1")
    assert application["tool"] == "submit_leave_request"
    assert "等待人工审批" in application["answer"]

    row = database.fetch_one(
        "SELECT leave_type, start_date, end_date, status FROM leave_requests ORDER BY id DESC LIMIT 1"
    )
    assert row is not None
    assert row["start_date"] == "2026-08-10"
    assert row["end_date"] == "2026-08-12"
    assert row["status"] == "pending_human_approval"


def test_escalation_to_human_is_flagged(agent, employee, database) -> None:
    state = run_graph(agent, employee, "请帮我转人工")
    assert state["route"] == "tool"
    assert state["tool_name"] == "escalate_to_human"

    result = agent.run(employee, "请帮我转人工", "127.0.0.1")
    assert result["tool"] == "escalate_to_human"
    assert result["escalated"] is True
    assert database.fetch_one("SELECT id FROM human_cases ORDER BY id DESC LIMIT 1") is not None


def test_policy_question_about_ticket_approval_does_not_write(
    agent: EnterpriseAgent, employee: Principal, database: Database
) -> None:
    """回归：问"创建工单要走什么审批"是咨询，不得落库建单。

    成因是意图判别只做关键词子串匹配（同时含"工单"与"创建"即写库）；
    现在由"咨询优先"判别拦在写操作之前，疑问句回到检索路由。
    """
    result = agent.run(employee, "创建工单要走什么审批？", "127.0.0.1")

    assert result["tool"] == "policy_search"
    assert database.fetch_one("SELECT COUNT(*) count FROM hr_tickets")["count"] == 0


def test_explicit_ticket_request_still_writes(
    agent: EnterpriseAgent, employee: Principal, database: Database
) -> None:
    """对照：带明确动作词的建单请求必须照常落库，咨询优先不能把正常办理一起挡住。"""
    result = agent.run(employee, "我要创建一个工单，主题是报销流程咨询", "127.0.0.1")

    assert result["tool"] == "create_hr_ticket"
    assert database.fetch_one("SELECT COUNT(*) count FROM hr_tickets")["count"] == 1


def test_policy_question_about_leave_process_is_answered(
    agent: EnterpriseAgent, employee: Principal, database: Database
) -> None:
    """回归：问"请假申请流程是什么"是咨询，应走检索回答并给出制度依据。

    曾在关键词命中「请假申请」时被判定为提交申请，因缺日期而返回"需要开始日期和
    结束日期"，制度问题本身从未被回答，同时留下一条无意义的申请记录。
    """
    state = run_graph(agent, employee, "请假申请流程是什么")

    assert state["route"] == "answer"
    assert state["citations"]
    assert database.fetch_one("SELECT id FROM leave_requests LIMIT 1") is None
