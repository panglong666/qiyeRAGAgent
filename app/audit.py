from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from app.database import Database
from app.models import Principal


def redact_sensitive_text(value: str) -> str:
    """审计日志默认脱敏，降低身份证、手机号、邮箱进入日志后的暴露风险。"""
    value = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "1**********", value)
    value = re.sub(r"(?<!\d)\d{17}[\dXx](?!\d)", "******************", value)
    value = re.sub(r"([\w.+-])([\w.+-]*)(@[^\s]+)", r"\1***\3", value)
    return value


class AuditLogger:
    """企业级日志审计核心：事件串联哈希，可检测历史记录被篡改。"""

    def __init__(self, database: Database):
        self.database = database

    def log(
        self,
        principal: Principal | None,
        action: str,
        resource: str,
        outcome: str,
        detail: dict[str, Any] | None = None,
        ip_address: str = "unknown",
    ) -> int:
        detail = self._redact(detail or {})
        occurred_at = datetime.now(UTC).isoformat()
        username = principal.username if principal else "anonymous"
        user_id = principal.user_id if principal else None
        # 写锁覆盖“读取前序哈希 + 插入新事件”，避免并发请求形成分叉审计链。
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT event_hash FROM audit_logs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous_hash = str(previous["event_hash"]) if previous else "GENESIS"
            canonical = json.dumps(
                {
                    "occurred_at": occurred_at,
                    "user_id": user_id,
                    "username": username,
                    "action": action,
                    "resource": resource,
                    "outcome": outcome,
                    "detail": detail,
                    "ip_address": ip_address,
                    "previous_hash": previous_hash,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            event_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            cursor = connection.execute(
                """
                INSERT INTO audit_logs
                (occurred_at, user_id, username, action, resource, outcome, detail_json,
                 ip_address, previous_hash, event_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    occurred_at, user_id, username, action, resource, outcome,
                    json.dumps(detail, ensure_ascii=False), ip_address, previous_hash, event_hash,
                ),
            )
            return int(cursor.lastrowid)

    def verify_chain(self) -> bool:
        rows = self.database.fetch_all("SELECT * FROM audit_logs ORDER BY id")
        previous_hash = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous_hash:
                return False
            canonical = json.dumps(
                {
                    "occurred_at": row["occurred_at"],
                    "user_id": row["user_id"],
                    "username": row["username"],
                    "action": row["action"],
                    "resource": row["resource"],
                    "outcome": row["outcome"],
                    "detail": json.loads(row["detail_json"]),
                    "ip_address": row["ip_address"],
                    "previous_hash": row["previous_hash"],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != row["event_hash"]:
                return False
            previous_hash = row["event_hash"]
        return True

    def list_recent(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.database.fetch_all("SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (limit,))
        return [self.database.row_to_dict(row) for row in rows]

    def _redact(self, value: Any) -> Any:
        if isinstance(value, str):
            return redact_sensitive_text(value)
        if isinstance(value, dict):
            return {key: self._redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        return value
