from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, status

from app.models import Principal


# 企业级权限控制核心：角色只获得完成职责所需的最小权限。
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "employee": frozenset({"chat.use", "policy.read", "ticket.create", "ticket.read_own", "leave.request"}),
    "hr": frozenset({
        "chat.use", "policy.read", "ticket.create", "ticket.read_own", "ticket.read_all",
        "leave.request", "leave.review", "human_case.manage",
    }),
    "auditor": frozenset({"chat.use", "policy.read", "audit.read", "ticket.read_all"}),
    "admin": frozenset({
        "chat.use", "policy.read", "ticket.create", "ticket.read_own", "ticket.read_all",
        "leave.request", "leave.review", "human_case.manage", "audit.read", "kb.manage",
    }),
}


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 210_000)
    return f"pbkdf2_sha256${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(digest).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        _, salt_b64, expected_b64 = encoded.split("$", 2)
        salt = base64.urlsafe_b64decode(salt_b64)
        expected = base64.urlsafe_b64decode(expected_b64)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 210_000)
    return hmac.compare_digest(actual, expected)


def permissions_for(role: str) -> frozenset[str]:
    return ROLE_PERMISSIONS.get(role, frozenset())


def ensure_permission(principal: Principal, permission: str) -> None:
    if permission not in permissions_for(principal.role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"当前角色无权限执行：{permission}",
        )


def create_session_token(principal: Principal, secret: str, expires_minutes: int) -> str:
    payload = {
        "sub": principal.user_id,
        "username": principal.username,
        "role": principal.role,
        "exp": int((datetime.now(UTC) + timedelta(minutes=expires_minutes)).timestamp()),
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=")
    signature = hmac.new(secret.encode("utf-8"), encoded, hashlib.sha256).digest()
    return f"{encoded.decode()}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"


def decode_session_token(token: str, secret: str) -> dict[str, Any] | None:
    try:
        payload_part, signature_part = token.split(".", 1)
        encoded = payload_part.encode("ascii")
        signature = base64.urlsafe_b64decode(signature_part + "=" * (-len(signature_part) % 4))
        expected = hmac.new(secret.encode("utf-8"), encoded, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            return None
        raw = base64.urlsafe_b64decode(payload_part + "=" * (-len(payload_part) % 4))
        payload = json.loads(raw)
        if int(payload["exp"]) < int(datetime.now(UTC).timestamp()):
            return None
        return payload
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None

