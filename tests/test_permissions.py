"""D. 权限：角色权限矩阵、API 层与工具层双重 RBAC、越权不中断对话。"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.audit import AuditLogger
from app.database import Database
from app.models import Principal
from app.rag import KnowledgeBase
from app.security import ensure_permission, permissions_for
from app.tools import ToolContext, ToolRegistry

# 六个业务工具所需的权限全部落在 employee 的权限集内，因此"工具层被拒"
# 无法用 employee 构造，需用缺失相应权限的真实角色（auditor 缺 ticket.create /
# leave.request）来覆盖越权分支。
ALL_TOOLS = {
    "policy_search",
    "calculate_annual_leave",
    "create_hr_ticket",
    "get_my_tickets",
    "submit_leave_request",
    "escalate_to_human",
}


def test_role_permission_matrix() -> None:
    employee = permissions_for("employee")
    assert {"chat.use", "policy.read"} <= employee
    assert "leave.review" not in employee
    assert "audit.read" not in employee
    assert "kb.manage" not in employee

    auditor = permissions_for("auditor")
    assert "audit.read" in auditor
    assert "ticket.read_all" in auditor
    assert "ticket.create" not in auditor
    assert "leave.request" not in auditor

    hr = permissions_for("hr")
    assert {"leave.review", "human_case.manage", "ticket.read_all"} <= hr
    assert "audit.read" not in hr
    assert "kb.manage" not in hr

    admin = permissions_for("admin")
    assert {"audit.read", "kb.manage", "leave.review", "human_case.manage"} <= admin

    # 未知角色不继承任何权限
    assert permissions_for("unknown-role") == frozenset()


def test_ensure_permission_blocks_employee_from_audit(employee: Principal) -> None:
    with pytest.raises(HTTPException) as error:
        ensure_permission(employee, "audit.read")
    assert error.value.status_code == 403


def test_tool_catalog_is_filtered_by_role(
    tool_registry: ToolRegistry, employee: Principal, auditor: Principal
) -> None:
    assert {item["name"] for item in tool_registry.describe_for(employee)} == ALL_TOOLS

    auditor_tools = {item["name"] for item in tool_registry.describe_for(auditor)}
    assert "policy_search" in auditor_tools
    assert "create_hr_ticket" not in auditor_tools
    assert "submit_leave_request" not in auditor_tools
    assert "get_my_tickets" not in auditor_tools


def test_tool_layer_rejects_role_without_permission(
    tool_registry: ToolRegistry,
    auditor: Principal,
    database: Database,
    knowledge_base: KnowledgeBase,
    audit: AuditLogger,
) -> None:
    context = ToolContext(
        principal=auditor,
        database=database,
        knowledge_base=knowledge_base,
        audit=audit,
        ip_address="127.0.0.1",
    )

    with pytest.raises(HTTPException) as error:
        tool_registry.execute(
            "create_hr_ticket",
            context,
            {"subject": "越权测试", "description": "auditor 不应能创建工单"},
        )
    assert error.value.status_code == 403
    assert "没有调用该工具的权限" in str(error.value.detail)

    # 被拒绝的调用不得产生任何业务数据
    assert database.fetch_one("SELECT id FROM hr_tickets LIMIT 1") is None


def test_denied_tool_call_is_audited(
    tool_registry: ToolRegistry,
    auditor: Principal,
    database: Database,
    knowledge_base: KnowledgeBase,
    audit: AuditLogger,
) -> None:
    context = ToolContext(
        principal=auditor,
        database=database,
        knowledge_base=knowledge_base,
        audit=audit,
        ip_address="127.0.0.1",
    )
    with pytest.raises(HTTPException):
        tool_registry.execute(
            "create_hr_ticket", context, {"subject": "x", "description": "y"}
        )

    row = database.fetch_one(
        "SELECT username, action, outcome, detail_json FROM audit_logs "
        "WHERE action='tool.denied' ORDER BY id DESC LIMIT 1"
    )
    assert row is not None
    assert row["username"] == "auditor"
    assert row["outcome"] == "denied"
    assert "ticket.create" in row["detail_json"]
    assert audit.verify_chain() is True


def test_permission_denial_does_not_break_the_conversation(
    make_agent: object, auditor: Principal, database: Database, audit: AuditLogger
) -> None:
    """越权应转成自然语言提示并继续走到 answer 节点，否则审计链会有头无尾。"""
    agent = make_agent()

    result = agent.run(auditor, "我要申请请假 2026-08-10 到 2026-08-12", "127.0.0.1")

    assert result["tool"] == "submit_leave_request"
    assert "没有调用该工具的权限" in result["answer"]
    assert "auditor" in result["answer"]
    # 流程走完：chat.answer 审计存在，哈希链完整
    assert database.fetch_one("SELECT id FROM audit_logs WHERE action='chat.answer' LIMIT 1") is not None
    assert audit.verify_chain() is True
    # 越权调用不产生业务数据
    assert database.fetch_one("SELECT id FROM leave_requests LIMIT 1") is None


def test_api_layer_enforces_same_permissions(api_client, login_as) -> None:
    assert login_as(api_client, "employee").status_code == 200
    assert api_client.get("/api/audit-logs").status_code == 403
    assert api_client.get("/api/human-cases").status_code == 403
    # 请假列表对申请人开放，但只返回自己那部分（越界由 test_employee_sees_own_voucher_only 卡住）
    assert api_client.get("/api/leave-requests").status_code == 200

    assert login_as(api_client, "auditor").status_code == 200
    assert api_client.get("/api/audit-logs").status_code == 200
    # 审计员既不是审批人也不是申请人，两个权限都没有
    assert api_client.get("/api/leave-requests").status_code == 403
    assert api_client.post("/api/admin/reindex").status_code == 403


def test_workbench_lists_expose_detail_fields(api_client, agent, employee, login_as) -> None:
    """工作台列表必须带够字段，前端点开才能显示完整内容而不是空壳。"""
    agent.run(employee, "我要创建一个工单，主题是报销流程咨询", "127.0.0.1")
    assert login_as(api_client, "employee").status_code == 200

    tickets = api_client.get("/api/tickets").json()
    assert tickets, "应能查到刚创建的工单"
    for field in ("id", "category", "subject", "description", "status", "created_at"):
        assert field in tickets[0], f"工单列表缺少字段：{field}"
    assert tickets[0]["subject"]

    agent.run(employee, "请帮我转人工", "127.0.0.1")
    assert login_as(api_client, "hr").status_code == 200

    cases = api_client.get("/api/human-cases").json()
    assert cases, "应能查到人工处理单"
    for field in (
        "id", "creator", "question", "reason", "status",
        "resolution", "created_at", "resolved_at", "resolver",
    ):
        assert field in cases[0], f"人工兜底列表缺少字段：{field}"
