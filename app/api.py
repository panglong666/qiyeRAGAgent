from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.agent import EnterpriseAgent
from app.audit import AuditLogger
from app.config import PROJECT_ROOT, get_settings
from app.database import Database
from app.models import (
    ChatRequest,
    LoginRequest,
    Principal,
    ReindexResponse,
    ResolveCaseRequest,
    ReviewLeaveRequest,
)
from app.rag import KnowledgeBase
from app.security import (
    create_session_token,
    decode_session_token,
    ensure_permission,
    permissions_for,
    verify_password,
)
from app.tools import ToolRegistry, build_tool_registry


settings = get_settings()
database = Database(settings.database_path)
knowledge_base = KnowledgeBase(
    settings.handbook_path,
    settings.rag_index_path,
    top_k=settings.rag_top_k,
    min_score=settings.rag_min_score,
    min_coverage=settings.rag_min_coverage,
    chunk_size=settings.rag_chunk_size,
    chunk_overlap=settings.rag_chunk_overlap,
)
audit = AuditLogger(database)
tool_registry: ToolRegistry = build_tool_registry()
agent = EnterpriseAgent(settings, database, knowledge_base, tool_registry, audit)


@asynccontextmanager
async def lifespan(_: FastAPI):
    database.initialize()
    knowledge_base.initialize()
    yield


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description="基于员工守则的企业级 RAG + Agent 内部助手",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=PROJECT_ROOT / "app" / "static"), name="static")


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def current_principal(
    session_token: str | None = Cookie(default=None),
) -> Principal:
    if not session_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    payload = decode_session_token(session_token, settings.app_secret_key)
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="会话已失效，请重新登录")
    row = database.fetch_one(
        "SELECT id, username, display_name, role, department FROM users WHERE id=? AND active=1",
        (payload["sub"],),
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在或已停用")
    return Principal(row["id"], row["username"], row["display_name"], row["role"], row["department"])


@app.get("/", include_in_schema=False)
def home() -> FileResponse:
    return FileResponse(PROJECT_ROOT / "app" / "static" / "index.html")


@app.get("/api/health")
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "knowledge_chunks": len(knowledge_base.chunks),
        "document_hash": knowledge_base.document_hash[:12],
        "llm_enabled": agent.generator.enabled,
        "llm_provider": "deepseek",
        "llm_model": settings.deepseek_model,
        "agent_framework": "langgraph",
        "retriever": "langchain_bm25",
    }


@app.post("/api/login")
def login(payload: LoginRequest, response: Response, request: Request) -> dict[str, object]:
    row = database.fetch_one("SELECT * FROM users WHERE username=? AND active=1", (payload.username,))
    if not row or not verify_password(payload.password, row["password_hash"]):
        audit.log(None, "auth.login", "session", "denied", {"username": payload.username}, client_ip(request))
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    principal = Principal(row["id"], row["username"], row["display_name"], row["role"], row["department"])
    token = create_session_token(principal, settings.app_secret_key, settings.session_expire_minutes)
    response.set_cookie(
        "session_token", token, httponly=True, samesite="strict",
        max_age=settings.session_expire_minutes * 60,
    )
    audit.log(principal, "auth.login", "session", "success", {}, client_ip(request))
    return principal_payload(principal)


@app.post("/api/logout")
def logout(
    response: Response,
    request: Request,
    principal: Principal = Depends(current_principal),
) -> dict[str, bool]:
    response.delete_cookie("session_token")
    audit.log(principal, "auth.logout", "session", "success", {}, client_ip(request))
    return {"ok": True}


@app.get("/api/me")
def me(principal: Principal = Depends(current_principal)) -> dict[str, object]:
    return principal_payload(principal)


@app.post("/api/chat")
def chat(
    payload: ChatRequest,
    request: Request,
    principal: Principal = Depends(current_principal),
) -> dict[str, object]:
    ensure_permission(principal, "chat.use")
    return agent.run(principal, payload.message, client_ip(request))


@app.get("/api/tickets")
def list_tickets(principal: Principal = Depends(current_principal)) -> list[dict[str, object]]:
    if "ticket.read_all" in permissions_for(principal.role):
        rows = database.fetch_all(
            """
            SELECT t.id, u.display_name creator, t.category, t.subject, t.status, t.created_at
            FROM hr_tickets t JOIN users u ON u.id=t.creator_id ORDER BY t.id DESC LIMIT 100
            """
        )
    else:
        ensure_permission(principal, "ticket.read_own")
        rows = database.fetch_all(
            "SELECT id, category, subject, status, created_at FROM hr_tickets WHERE creator_id=? ORDER BY id DESC",
            (principal.user_id,),
        )
    return [dict(row) for row in rows]


@app.get("/api/human-cases")
def list_human_cases(principal: Principal = Depends(current_principal)) -> list[dict[str, object]]:
    ensure_permission(principal, "human_case.manage")
    rows = database.fetch_all(
        """
        SELECT c.id, u.display_name creator, c.question, c.reason, c.status,
               c.resolution, c.created_at, c.resolved_at
        FROM human_cases c JOIN users u ON u.id=c.creator_id
        ORDER BY CASE c.status WHEN 'pending' THEN 0 ELSE 1 END, c.id DESC
        LIMIT 100
        """
    )
    return [dict(row) for row in rows]


@app.post("/api/human-cases/{case_id}/resolve")
def resolve_human_case(
    case_id: int,
    payload: ResolveCaseRequest,
    request: Request,
    principal: Principal = Depends(current_principal),
) -> dict[str, object]:
    ensure_permission(principal, "human_case.manage")
    existing = database.fetch_one("SELECT id FROM human_cases WHERE id=?", (case_id,))
    if not existing:
        raise HTTPException(status_code=404, detail="人工处理单不存在")
    with database.connect() as connection:
        connection.execute(
            """
            UPDATE human_cases SET status='resolved', resolution=?, resolver_id=?, resolved_at=?
            WHERE id=?
            """,
            (payload.resolution, principal.user_id, datetime.now(UTC).isoformat(), case_id),
        )
    audit.log(
        principal, "human_case.resolve", f"human_case:{case_id}", "success",
        {"resolution": payload.resolution}, client_ip(request),
    )
    return {"id": case_id, "status": "resolved"}


@app.get("/api/leave-requests")
def list_leave_requests(principal: Principal = Depends(current_principal)) -> list[dict[str, object]]:
    ensure_permission(principal, "leave.review")
    rows = database.fetch_all(
        """
        SELECT r.id, u.display_name creator, r.leave_type, r.start_date, r.end_date,
               r.reason, r.status, r.created_at
        FROM leave_requests r JOIN users u ON u.id=r.creator_id ORDER BY r.id DESC LIMIT 100
        """
    )
    return [dict(row) for row in rows]


@app.post("/api/leave-requests/{request_id}/review")
def review_leave_request(
    request_id: int,
    payload: ReviewLeaveRequest,
    request: Request,
    principal: Principal = Depends(current_principal),
) -> dict[str, object]:
    ensure_permission(principal, "leave.review")
    existing = database.fetch_one("SELECT id, status FROM leave_requests WHERE id=?", (request_id,))
    if not existing:
        raise HTTPException(status_code=404, detail="请假申请不存在")
    if existing["status"] != "pending_human_approval":
        raise HTTPException(status_code=409, detail="该申请已处理")
    database.execute("UPDATE leave_requests SET status=? WHERE id=?", (payload.decision, request_id))
    audit.log(
        principal, "leave.review", f"leave_request:{request_id}", payload.decision,
        {"note": payload.note}, client_ip(request),
    )
    return {"id": request_id, "status": payload.decision}


@app.get("/api/audit-logs")
def audit_logs(principal: Principal = Depends(current_principal)) -> dict[str, object]:
    ensure_permission(principal, "audit.read")
    return {"chain_valid": audit.verify_chain(), "items": audit.list_recent(100)}


@app.post("/api/admin/reindex", response_model=ReindexResponse)
def reindex(
    request: Request,
    principal: Principal = Depends(current_principal),
) -> ReindexResponse:
    ensure_permission(principal, "kb.manage")
    count = knowledge_base.rebuild()
    audit.log(
        principal, "knowledge.reindex", settings.handbook_path.name, "success",
        {"chunk_count": count, "document_hash": knowledge_base.document_hash}, client_ip(request),
    )
    return ReindexResponse(chunk_count=count, document_hash=knowledge_base.document_hash)


def principal_payload(principal: Principal) -> dict[str, object]:
    return {
        "id": principal.user_id,
        "username": principal.username,
        "display_name": principal.display_name,
        "role": principal.role,
        "department": principal.department,
        "permissions": sorted(permissions_for(principal.role)),
        "tools": tool_registry.describe_for(principal),
    }
