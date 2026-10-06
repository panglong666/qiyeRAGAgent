"""E. 审批：Human-in-the-loop——AI 只提交申请，批准或拒绝必须由 HR 人工完成。"""
from __future__ import annotations

import sqlite3

import pytest

from app.database import Database
from app.models import Principal

LEAVE_MESSAGE = "我要申请请假 2026-08-10 到 2026-08-12"


def _create_pending_request(agent, employee: Principal, database: Database) -> int:
    agent.run(employee, LEAVE_MESSAGE, "127.0.0.1")
    row = database.fetch_one("SELECT id, status FROM leave_requests ORDER BY id DESC LIMIT 1")
    assert row is not None, "请假申请未写入数据库"
    assert row["status"] == "pending_human_approval"
    return int(row["id"])


def test_ai_only_submits_and_leaves_final_decision_to_human(
    agent, employee: Principal, database: Database
) -> None:
    result = agent.run(employee, LEAVE_MESSAGE, "127.0.0.1")

    assert result["tool"] == "submit_leave_request"
    assert "等待人工审批" in result["answer"]

    row = database.fetch_one(
        "SELECT status, leave_type FROM leave_requests ORDER BY id DESC LIMIT 1"
    )
    assert row is not None
    # 关键约束：AI 不能把状态直接改成 approved
    assert row["status"] == "pending_human_approval"
    assert database.fetch_one("SELECT id FROM leave_requests WHERE status='approved' LIMIT 1") is None


@pytest.mark.parametrize(
    ("decision", "expected_status"), [("approved", "approved"), ("rejected", "rejected")]
)
def test_hr_review_decides_final_status(
    api_client, agent, employee: Principal, database: Database, login_as, decision, expected_status
) -> None:
    request_id = _create_pending_request(agent, employee, database)

    assert login_as(api_client, "hr").status_code == 200
    response = api_client.post(
        f"/api/leave-requests/{request_id}/review",
        json={"decision": decision, "note": "自动化测试"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == expected_status
    stored = database.fetch_one("SELECT status FROM leave_requests WHERE id=?", (request_id,))
    assert stored["status"] == expected_status

    # 已处理的申请不允许重复审批
    duplicate = api_client.post(
        f"/api/leave-requests/{request_id}/review",
        json={"decision": "approved", "note": "重复审批"},
    )
    assert duplicate.status_code == 409


def test_non_hr_role_cannot_review(
    api_client, agent, employee: Principal, database: Database, login_as
) -> None:
    request_id = _create_pending_request(agent, employee, database)

    assert login_as(api_client, "employee").status_code == 200
    response = api_client.post(
        f"/api/leave-requests/{request_id}/review",
        json={"decision": "approved", "note": "自己批自己"},
    )

    assert response.status_code == 403
    stored = database.fetch_one("SELECT status FROM leave_requests WHERE id=?", (request_id,))
    assert stored["status"] == "pending_human_approval"


def test_human_fallback_case_can_be_resolved_by_hr(
    api_client, agent, employee: Principal, database: Database, login_as
) -> None:
    """转人工后由 HR 填写处理结果，闭环才算完成。"""
    result = agent.run(employee, "请帮我转人工", "127.0.0.1")
    assert result["escalated"] is True
    case_id = database.fetch_one("SELECT id FROM human_cases ORDER BY id DESC LIMIT 1")["id"]

    # 员工无权处理人工单
    assert login_as(api_client, "employee").status_code == 200
    assert api_client.post(
        f"/api/human-cases/{case_id}/resolve", json={"resolution": "越权尝试"}
    ).status_code == 403

    assert login_as(api_client, "hr").status_code == 200
    response = api_client.post(
        f"/api/human-cases/{case_id}/resolve", json={"resolution": "已电话答复并同步制度条款"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "resolved"

    stored = database.fetch_one("SELECT status, resolver_id FROM human_cases WHERE id=?", (case_id,))
    assert stored["status"] == "resolved"
    assert stored["resolver_id"] is not None


@pytest.mark.parametrize(
    ("decision", "note"), [("approved", "已核对年假余额"), ("rejected", "该时段人手不足")]
)
def test_review_writes_approval_voucher(
    api_client, agent, employee, hr, database, login_as, decision, note
) -> None:
    """审批必须留下凭证：结论 + 审批人 + 审批时间 + 审批意见，四者缺一不可。"""
    request_id = _create_pending_request(agent, employee, database)

    assert login_as(api_client, "hr").status_code == 200
    response = api_client.post(
        f"/api/leave-requests/{request_id}/review",
        json={"decision": decision, "note": note},
    )
    assert response.status_code == 200

    row = database.fetch_one(
        "SELECT status, reviewer_id, review_note, reviewed_at FROM leave_requests WHERE id=?",
        (request_id,),
    )
    assert row["status"] == decision
    assert row["reviewer_id"] == hr.user_id
    assert row["review_note"] == note
    assert row["reviewed_at"], "审批时间必须落库，否则凭证无法说明何时生效"


def test_leave_list_exposes_voucher_fields(
    api_client, agent, employee, hr, database, login_as
) -> None:
    """审批后，接口应把凭证字段一并返回给前端，而不只是审计日志里有。"""
    request_id = _create_pending_request(agent, employee, database)
    assert login_as(api_client, "hr").status_code == 200
    api_client.post(
        f"/api/leave-requests/{request_id}/review",
        json={"decision": "approved", "note": "同意，做好交接"},
    )

    items = api_client.get("/api/leave-requests").json()
    item = next((row for row in items if row["id"] == request_id), None)
    assert item is not None, "列表接口应返回该申请"

    assert item["reviewer"] == hr.display_name
    assert item["review_note"] == "同意，做好交接"
    assert item["reviewed_at"]
    # 凭证还应能说明"申请了什么"：假别、起止、事由
    assert item["leave_type"]
    assert item["start_date"] and item["end_date"]
    assert item["reason"]


def test_employee_sees_own_voucher_only(
    api_client, agent, employee, hr, database, login_as
) -> None:
    """闭环最后一环：申请人能看到自己那单的审批凭证，但看不到别人的申请。"""
    mine = _create_pending_request(agent, employee, database)

    # HR 也提交一单，作为"别人的申请"
    other = agent.run(hr, "我要申请请假 2026-11-02 到 2026-11-03", "127.0.0.1")
    assert other["tool"] == "submit_leave_request"
    other_id = database.fetch_one("SELECT id FROM leave_requests ORDER BY id DESC LIMIT 1")["id"]
    assert other_id != mine

    # HR 审批掉员工那一单
    assert login_as(api_client, "hr").status_code == 200
    assert api_client.post(
        f"/api/leave-requests/{mine}/review",
        json={"decision": "approved", "note": "已核对，同意"},
    ).status_code == 200

    # 员工视角：只看到自己的，且凭证完整
    assert login_as(api_client, "employee").status_code == 200
    items = api_client.get("/api/leave-requests").json()
    ids = {item["id"] for item in items}
    assert mine in ids, "申请人应能看到自己的申请"
    assert other_id not in ids, "申请人不应看到他人的申请"

    voucher = next(item for item in items if item["id"] == mine)
    assert voucher["status"] == "approved"
    assert voucher["reviewer"] == hr.display_name
    assert voucher["review_note"] == "已核对，同意"
    assert voucher["reviewed_at"]
    assert voucher["start_date"] and voucher["end_date"]

    # HR 视角：整条队列都在
    assert login_as(api_client, "hr").status_code == 200
    hr_ids = {item["id"] for item in api_client.get("/api/leave-requests").json()}
    assert {mine, other_id} <= hr_ids, "审批人应能看到整条队列"


def test_legacy_database_gets_voucher_columns(tmp_path) -> None:
    """历史库没有凭证列，升级后必须自动补列，否则请假列表接口会直接报错。"""
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            display_name TEXT NOT NULL,
            role TEXT NOT NULL,
            department TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE leave_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            creator_id INTEGER NOT NULL,
            leave_type TEXT NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending_human_approval',
            created_at TEXT NOT NULL
        );
        INSERT INTO leave_requests
            (creator_id, leave_type, start_date, end_date, reason, status, created_at)
        VALUES (1, '年假', '2026-08-10', '2026-08-12', '旧库既有申请', 'pending_human_approval',
                '2026-08-01T00:00:00+00:00');
        """
    )
    connection.commit()
    connection.close()

    database = Database(path)
    database.initialize()
    database.initialize()  # 幂等：重复初始化不应报错

    columns = {row["name"] for row in database.fetch_all("PRAGMA table_info(leave_requests)")}
    assert {"reviewer_id", "review_note", "reviewed_at"} <= columns

    # 迁移不得丢数据：旧库那条申请要原样还在
    legacy = database.fetch_one("SELECT reason, status FROM leave_requests WHERE id=1")
    assert legacy is not None, "迁移过程丢失了历史申请"
    assert legacy["reason"] == "旧库既有申请"
    assert legacy["status"] == "pending_human_approval"

    # 补列后凭证字段可正常写入与读回
    database.execute(
        "UPDATE leave_requests SET reviewer_id=2, review_note=?, reviewed_at=? WHERE id=1",
        ("补列后写入", "2026-10-06T00:00:00+00:00"),
    )
    row = database.fetch_one(
        "SELECT reviewer_id, review_note, reviewed_at FROM leave_requests WHERE id=1"
    )
    assert row["reviewer_id"] == 2
    assert row["review_note"] == "补列后写入"
    assert row["reviewed_at"] == "2026-10-06T00:00:00+00:00"
