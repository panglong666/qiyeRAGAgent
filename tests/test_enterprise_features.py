from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.agent import UNKNOWN_ANSWER, EnterpriseAgent
from app.audit import AuditLogger
from app.config import PROJECT_ROOT, Settings
from app.database import Database
from app.llm import GroundedAnswerGenerator
from app.models import Principal
from app.rag import KnowledgeBase
from app.security import ensure_permission
from app.tools import build_tool_registry


HANDBOOK = PROJECT_ROOT / "员工守则_美化版.docx"
EMPLOYEE = Principal(1, "employee", "普通员工", "employee", "产品部")


class FakeGenerator:
    def __init__(self, response: str = "模拟 DeepSeek 回答 [1]", error: Exception | None = None):
        self.enabled = True
        self.response = response
        self.error = error
        self.calls = 0

    def generate(self, question, documents):
        self.calls += 1
        if self.error:
            raise self.error
        assert question
        assert documents
        return self.response


def build_settings(tmp_path: Path, *, deepseek_enabled: bool = False) -> Settings:
    return Settings(
        handbook_path=HANDBOOK,
        database_path=tmp_path / "enterprise.db",
        rag_index_path=tmp_path / "index.json",
        deepseek_enabled=deepseek_enabled,
        deepseek_api_key=None,
    )


def build_knowledge_base(settings: Settings) -> KnowledgeBase:
    knowledge_base = KnowledgeBase(
        settings.handbook_path,
        settings.rag_index_path,
        top_k=settings.rag_top_k,
        min_score=settings.rag_min_score,
        min_coverage=settings.rag_min_coverage,
        chunk_size=settings.rag_chunk_size,
        chunk_overlap=settings.rag_chunk_overlap,
    )
    knowledge_base.initialize()
    return knowledge_base


def build_agent(tmp_path: Path, generator=None):
    settings = build_settings(tmp_path)
    database = Database(settings.database_path)
    database.initialize()
    knowledge_base = build_knowledge_base(settings)
    audit = AuditLogger(database)
    agent = EnterpriseAgent(
        settings,
        database,
        knowledge_base,
        build_tool_registry(),
        audit,
        generator=generator,
    )
    return agent, database, knowledge_base, audit


def test_langchain_docx_loader_splitter_and_bm25_metadata(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    knowledge_base = build_knowledge_base(settings)
    assert knowledge_base.retriever is not None
    assert knowledge_base.retrieval_chain is not None
    assert len(knowledge_base.documents) >= 30
    assert all("source" in document.metadata for document in knowledge_base.documents)
    assert all("section" in document.metadata for document in knowledge_base.documents)
    assert all("chunk_id" in document.metadata for document in knowledge_base.documents)

    hits = knowledge_base.search("迟到超过30分钟如何处理", top_k=3)
    assert hits
    assert "考勤方式" in hits[0].chunk.section
    assert "旷工半日" in hits[0].chunk.text


def test_employee_cannot_read_audit_logs() -> None:
    with pytest.raises(HTTPException) as error:
        ensure_permission(EMPLOYEE, "audit.read")
    assert error.value.status_code == 403


def test_audit_hash_chain_detects_tampering(tmp_path: Path) -> None:
    database = Database(tmp_path / "audit.db")
    database.initialize()
    logger = AuditLogger(database)
    logger.log(EMPLOYEE, "test.action", "unit-test", "success", {"phone": "13812345678"})
    assert logger.verify_chain() is True
    row = database.fetch_one("SELECT id FROM audit_logs ORDER BY id DESC LIMIT 1")
    database.execute("UPDATE audit_logs SET outcome='tampered' WHERE id=?", (row["id"],))
    assert logger.verify_chain() is False


def test_known_policy_question_uses_deepseek_and_returns_citations(tmp_path: Path) -> None:
    generator = FakeGenerator("迟到超过30分钟可按旷工半日处理。[1]")
    agent, database, _, audit = build_agent(tmp_path, generator)
    result = agent.run(EMPLOYEE, "迟到超过30分钟如何处理？", "127.0.0.1")
    assert result["answer"].endswith("[1]")
    assert result["citations"]
    assert result["tool"] == "policy_search"
    assert generator.calls == 1
    assert audit.verify_chain() is True
    graph_nodes = database.fetch_one("SELECT COUNT(*) count FROM audit_logs WHERE action='graph.node'")
    assert graph_nodes["count"] >= 3


def test_unknown_question_says_unknown_without_deepseek_or_human_case(tmp_path: Path) -> None:
    generator = FakeGenerator()
    agent, database, _, _ = build_agent(tmp_path, generator)
    result = agent.run(EMPLOYEE, "食堂今天菜单是什么？", "127.0.0.1")
    assert result["answer"] == UNKNOWN_ANSWER
    assert result["citations"] == []
    assert result["escalated"] is False
    assert generator.calls == 0
    assert database.fetch_one("SELECT id FROM human_cases LIMIT 1") is None


def test_deepseek_failure_falls_back_to_retrieved_source(tmp_path: Path) -> None:
    generator = FakeGenerator(error=TimeoutError("mock timeout"))
    agent, database, _, _ = build_agent(tmp_path, generator)
    result = agent.run(EMPLOYEE, "公司原则上每月几号发放上月工资？", "127.0.0.1")
    assert "DeepSeek 暂时不可用" in result["answer"]
    assert "15 日" in result["answer"]
    assert result["citations"]
    failure = database.fetch_one(
        "SELECT id FROM audit_logs WHERE action='llm.generate' AND outcome='failed' LIMIT 1"
    )
    assert failure is not None


def test_langchain_tools_and_langgraph_human_approval(tmp_path: Path) -> None:
    agent, database, _, _ = build_agent(tmp_path)

    leave = agent.run(EMPLOYEE, "工作满8年有几天年假？", "127.0.0.1")
    assert leave["tool"] == "calculate_annual_leave"
    assert "5 天" in leave["answer"]
    assert any("StructuredTool" in step["detail"] for step in leave["steps"])

    request = agent.run(
        EMPLOYEE,
        "我要提交请假申请 2026-08-10 到 2026-08-12",
        "127.0.0.1",
    )
    assert request["tool"] == "submit_leave_request"
    assert "等待人工审批" in request["answer"]
    row = database.fetch_one("SELECT status FROM leave_requests ORDER BY id DESC LIMIT 1")
    assert row["status"] == "pending_human_approval"

    human = agent.run(EMPLOYEE, "请帮我转人工", "127.0.0.1")
    assert human["tool"] == "escalate_to_human"
    assert human["escalated"] is True
    assert database.fetch_one("SELECT id FROM human_cases ORDER BY id DESC LIMIT 1") is not None


@pytest.mark.skipif(
    os.getenv("RUN_DEEPSEEK_LIVE_TEST") != "1",
    reason="设置 RUN_DEEPSEEK_LIVE_TEST=1 后才调用真实 DeepSeek API",
)
def test_deepseek_live_connection(tmp_path: Path) -> None:
    settings = Settings(
        handbook_path=HANDBOOK,
        database_path=tmp_path / "live.db",
        rag_index_path=tmp_path / "live-index.json",
    )
    assert settings.deepseek_api_key is not None
    generator = GroundedAnswerGenerator(settings)
    knowledge_base = build_knowledge_base(settings)
    documents = [hit.document for hit in knowledge_base.search("每月几号发工资？", top_k=2)]
    answer = generator.generate("每月几号发工资？", documents)
    assert answer
    assert "15" in answer
