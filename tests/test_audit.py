"""F. 审计：脱敏、SHA-256 串联哈希链、篡改检测。"""
from __future__ import annotations

import re

import pytest

from app.audit import AuditLogger, redact_sensitive_text
from app.database import Database
from app.models import Principal

RAW_PHONE = "13812345678"
ID_NUMBER = "110101199003074567"
EMAIL = "zhangsan@example.com"


@pytest.mark.parametrize(
    ("raw", "should_disappear", "should_appear"),
    [
        (f"联系电话 {RAW_PHONE}", RAW_PHONE, r"1\*{10}"),
        (f"身份证号 {ID_NUMBER}", ID_NUMBER, r"\*{18}"),
        (f"邮箱 {EMAIL}", EMAIL, r"z\*{3}@example\.com"),
    ],
)
def test_redact_sensitive_text_masks_each_pattern(
    raw: str, should_disappear: str, should_appear: str
) -> None:
    masked = redact_sensitive_text(raw)
    assert should_disappear not in masked
    assert re.search(should_appear, masked), f"{raw!r} 脱敏结果不符合预期：{masked!r}"


def test_normal_text_is_not_altered() -> None:
    text = "年假为 5 天，工作满 10 年为 10 天。"
    assert redact_sensitive_text(text) == text


def test_sensitive_values_are_redacted_before_persisting(
    database: Database, audit: AuditLogger, employee: Principal
) -> None:
    audit.log(
        employee,
        "test.redact",
        "unit-test",
        "success",
        {"note": f"电话 {RAW_PHONE}，身份证 {ID_NUMBER}，邮箱 {EMAIL}"},
    )

    row = database.fetch_one("SELECT detail_json FROM audit_logs ORDER BY id DESC LIMIT 1")
    stored = row["detail_json"]

    # 原文一律不得落盘
    assert RAW_PHONE not in stored
    assert ID_NUMBER not in stored
    assert EMAIL not in stored
    # 脱敏占位符已写入
    assert re.search(r"1\*{10}", stored)
    assert re.search(r"\*{18}", stored)
    assert re.search(r"z\*{3}@example\.com", stored)
    assert audit.verify_chain() is True


def test_verify_chain_holds_for_fresh_and_growing_log(
    database: Database, audit: AuditLogger, employee: Principal
) -> None:
    assert audit.verify_chain() is True

    audit.log(employee, "test.action", "unit-test", "started", {"step": 1})
    audit.log(employee, "test.action", "unit-test", "success", {"step": 2})
    audit.log(None, "auth.login", "session", "denied", {"username": "attacker"})

    assert audit.verify_chain() is True
    items = audit.list_recent(10)
    assert len(items) == 3
    # 匿名事件的 username 应回退为 anonymous，而不是崩溃或写成 null
    assert items[0]["username"] == "anonymous"


@pytest.mark.parametrize("field", ["outcome", "detail_json", "username", "ip_address"])
def test_verify_chain_detects_field_tampering(
    database: Database, audit: AuditLogger, employee: Principal, field: str
) -> None:
    audit.log(employee, "test.action", "unit-test", "success", {"note": "原始记录"})
    audit.log(employee, "test.action", "unit-test", "success", {"note": "第二条"})
    assert audit.verify_chain() is True

    # ip_address 列是 NOT NULL，篡改时保持类型合法
    replacement = {"outcome": "'tampered'", "detail_json": "'{}'", "username": "'attacker'", "ip_address": "'10.0.0.9'"}
    target = database.fetch_one("SELECT id FROM audit_logs ORDER BY id ASC LIMIT 1")["id"]
    database.execute(
        f"UPDATE audit_logs SET {field}={replacement[field]} WHERE id=?", (target,)
    )

    assert audit.verify_chain() is False


def test_verify_chain_detects_deleted_record(
    database: Database, audit: AuditLogger, employee: Principal
) -> None:
    for index in range(3):
        audit.log(employee, "test.action", "unit-test", "success", {"index": index})
    assert audit.verify_chain() is True

    middle = database.fetch_one("SELECT id FROM audit_logs ORDER BY id ASC LIMIT 1 OFFSET 1")["id"]
    database.execute("DELETE FROM audit_logs WHERE id=?", (middle,))

    # 删除中间记录会破坏 previous_hash 串联
    assert audit.verify_chain() is False


def test_previous_hash_links_to_previous_event(
    database: Database, audit: AuditLogger, employee: Principal
) -> None:
    audit.log(employee, "test.action", "unit-test", "success", {"index": 1})
    audit.log(employee, "test.action", "unit-test", "success", {"index": 2})

    rows = database.fetch_all("SELECT previous_hash, event_hash FROM audit_logs ORDER BY id ASC")
    assert rows[0]["previous_hash"] == "GENESIS"
    assert rows[1]["previous_hash"] == rows[0]["event_hash"]
    assert rows[0]["event_hash"] != rows[1]["event_hash"]
