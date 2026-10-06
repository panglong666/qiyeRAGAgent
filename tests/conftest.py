"""共享 fixture：为「企业制度查询 Agent」的测试提供隔离环境。

设计要点：
1. 不使用 get_settings()——它带 @lru_cache 且会强校验 APP_SECRET_KEY，
   密钥非法会直接 RuntimeError 打断整个测试会话。这里一律显式构造 Settings(...)，
   路径全部指向 tmp_path。
2. 一律不发真实 DeepSeek 请求：默认生成器关闭；需要"有模型"的场景注入 FakeGenerator。
3. 知识库使用项目根目录的真实《员工守则_美化版.docx》，索引写到 tmp_path，
   因此不会污染 data/index.json 与 data/enterprise_ai.db。
"""
from __future__ import annotations

import os

# 必须在导入 app.config 之前设置：pydantic-settings 的优先级是
# 显式入参 > 环境变量 > .env > 默认值，测试不该依赖使用者本地 .env 里恰好有合法密钥。
os.environ.setdefault("APP_SECRET_KEY", "pytest-only-app-secret-key-32chars-min")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from app.agent import EnterpriseAgent  # noqa: E402
from app.audit import AuditLogger  # noqa: E402
from app.config import PROJECT_ROOT, Settings  # noqa: E402
from app.database import Database  # noqa: E402
from app.models import Principal  # noqa: E402
from app.rag import KnowledgeBase  # noqa: E402
from app.tools import ToolRegistry, build_tool_registry  # noqa: E402

HANDBOOK = PROJECT_ROOT / "员工守则_美化版.docx"

# 四个演示账号由 Database.initialize() 自动 seed，密码见 app/database.py
DEMO_CREDENTIALS = {
    "employee": "Employee@123",
    "hr": "Hr@123456",
    "auditor": "Audit@123",
    "admin": "Admin@123",
}


class FakeGenerator:
    """替身生成器：可控返回、可控异常、可控 enabled，并记录调用次数。"""

    def __init__(
        self,
        response: str | None = "模拟 DeepSeek 回答 [1]",
        error: Exception | None = None,
        enabled: bool = True,
    ):
        self.enabled = enabled
        self.response = response
        self.error = error
        self.calls = 0

    def generate(self, question: str, documents) -> str | None:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.response


@pytest.fixture
def fake_generator():
    """以 fixture 形式暴露替身类。

    tests/ 不是 Python 包（没有 __init__.py），跨模块 `from tests.conftest import ...`
    只在特定调用方式下才成立，因此统一通过 fixture 获取。
    """
    return FakeGenerator


@pytest.fixture
def handbook_path() -> Path:
    assert HANDBOOK.exists(), f"知识库源文件缺失：{HANDBOOK}"
    return HANDBOOK


@pytest.fixture
def settings(tmp_path: Path, handbook_path: Path) -> Settings:
    """隔离的配置：数据库与索引都在 tmp_path，DeepSeek 固定关闭。"""
    return Settings(
        handbook_path=handbook_path,
        database_path=tmp_path / "test.db",
        rag_index_path=tmp_path / "index.json",
        deepseek_enabled=False,
        deepseek_api_key=None,
    )


@pytest.fixture
def database(settings: Settings) -> Database:
    db = Database(settings.database_path)
    db.initialize()
    return db


@pytest.fixture
def knowledge_base(settings: Settings) -> KnowledgeBase:
    kb = KnowledgeBase(
        settings.handbook_path,
        settings.rag_index_path,
        top_k=settings.rag_top_k,
        min_score=settings.rag_min_score,
        min_coverage=settings.rag_min_coverage,
        max_df_ratio=settings.rag_max_df_ratio,
        min_substantive_tokens=settings.rag_min_substantive_tokens,
        chunk_size=settings.rag_chunk_size,
        chunk_overlap=settings.rag_chunk_overlap,
    )
    kb.initialize()
    return kb


@pytest.fixture
def audit(database: Database) -> AuditLogger:
    return AuditLogger(database)


@pytest.fixture
def tool_registry() -> ToolRegistry:
    return build_tool_registry()


@pytest.fixture
def make_agent(settings, database, knowledge_base, audit, tool_registry):
    """构造 Agent 的工厂：默认用真实 GroundedAnswerGenerator 但保持关闭（不发请求）。"""

    def _make(generator=None, registry=None) -> EnterpriseAgent:
        return EnterpriseAgent(
            settings,
            database,
            knowledge_base,
            registry or tool_registry,
            audit,
            generator=generator,
        )

    return _make


@pytest.fixture
def agent(make_agent) -> EnterpriseAgent:
    return make_agent()


def _principal_of(database: Database, username: str) -> Principal:
    row = database.fetch_one(
        "SELECT id, username, display_name, role, department FROM users WHERE username=?",
        (username,),
    )
    assert row is not None, f"演示账号 {username} 未初始化"
    return Principal(row["id"], row["username"], row["display_name"], row["role"], row["department"])


@pytest.fixture
def employee(database: Database) -> Principal:
    return _principal_of(database, "employee")


@pytest.fixture
def hr(database: Database) -> Principal:
    return _principal_of(database, "hr")


@pytest.fixture
def auditor(database: Database) -> Principal:
    return _principal_of(database, "auditor")


@pytest.fixture
def admin(database: Database) -> Principal:
    return _principal_of(database, "admin")


def login(client, username: str):
    """以演示账号登录，返回响应。Cookie 由 TestClient 自身保存。"""
    return client.post(
        "/api/login",
        json={"username": username, "password": DEMO_CREDENTIALS[username]},
    )


@pytest.fixture
def login_as():
    """返回登录辅助函数（tests/ 非包，不能跨模块 import helper）。"""
    return login


@pytest.fixture
def api_client(monkeypatch, settings, database, knowledge_base, audit, tool_registry, agent):
    """把 app.api 的模块级单例替换为隔离实例，再启动 TestClient。

    app/api.py 直接引用模块级全局（settings / database / knowledge_base /
    audit / tool_registry / agent），不走 Depends，因此只能通过替换模块属性来隔离。

    注意 audit 必须一并替换：AuditLogger 在构造时就捕获了 Database 对象，
    只替换 api.database 的话，业务写操作会进 tmp 库、而审计写入仍会落到
    **真实** 的 data/enterprise_ai.db。这个坑踩过一次，这里加断言兜住。
    """
    from fastapi.testclient import TestClient

    from app import api as api_module

    monkeypatch.setattr(api_module, "settings", settings)
    monkeypatch.setattr(api_module, "database", database)
    monkeypatch.setattr(api_module, "knowledge_base", knowledge_base)
    monkeypatch.setattr(api_module, "audit", audit)
    monkeypatch.setattr(api_module, "tool_registry", tool_registry)
    monkeypatch.setattr(api_module, "agent", agent)

    # 防御断言：确认审计器与接口层确实指向隔离库，避免再次污染真实业务库
    assert api_module.audit.database.path == database.path
    assert api_module.database.path == database.path
    assert Path(database.path).parent == settings.database_path.parent

    with TestClient(api_module.app) as client:
        yield client
