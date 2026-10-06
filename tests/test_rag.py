"""A. 检索层：DOCX 加载 / 分块元数据 / 双阈值命中与拒答 / 索引缓存与重建。"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import docx as docx_lib
import pytest

from app.config import PROJECT_ROOT
from app.rag import KnowledgeBase

HANDBOOK = PROJECT_ROOT / "员工守则_美化版.docx"

# 查询用词均在《员工守则》原文出现，且实测可通过 min_score / min_coverage 双阈值
HIT_CASES = [
    ("带薪年假有多少天", "带薪年假"),
    ("考勤怎么打卡", "考勤方式"),
    ("病假怎么申请", "病假"),
]

# 守则中不存在相关内容的查询。
# 后两条是"偶合词"陷阱：能命中语料级通用词（公司）或跨词碎片（司的），
# 但整句主题并不在知识库范围内，不得凭这类词作答。
MISS_CASES = [
    "今天天气怎么样",
    "帮我订一张机票",
    "公司股票代码是多少？",
    "公司的竞争对手都有谁？",
]

REBUILD_MARKER = "补充条款：本条用于验证文档哈希变化后的索引重建。"


def test_docx_loaded_and_sections_recognized(knowledge_base: KnowledgeBase) -> None:
    documents = knowledge_base.documents
    assert len(documents) > 0

    sections = [document.metadata["section"] for document in documents]
    assert any("带薪年假" in section for section in sections)
    assert any("考勤方式" in section for section in sections)
    assert any("离职管理" in section for section in sections)

    # 文档开头有目录，章节标题会重复出现一次；KnowledgeBase 已有跳过目录的逻辑，
    # 因此同一章节只应产生一个正文知识块（这不是"重复 bug"）。
    assert sum(1 for section in sections if "前言与欢迎辞" in section) == 1

    # 来源文件名被写入元数据，便于引用溯源
    assert {document.metadata["source"] for document in documents} == {HANDBOOK.name}


def test_chunks_have_complete_metadata(knowledge_base: KnowledgeBase) -> None:
    documents = knowledge_base.documents
    assert len(documents) > 0
    for document in documents:
        assert document.metadata.get("chunk_id")
        assert document.metadata.get("section")
        assert document.metadata.get("classification") == "internal"
        assert document.page_content.strip()

    # chunk_id 必须唯一，否则引用无法定位
    chunk_ids = [document.metadata["chunk_id"] for document in documents]
    assert len(set(chunk_ids)) == len(chunk_ids)


@pytest.mark.parametrize(("query", "expected_section"), HIT_CASES)
def test_hit_query_returns_results_meeting_both_thresholds(
    knowledge_base: KnowledgeBase, query: str, expected_section: str
) -> None:
    hits = knowledge_base.search(query, top_k=4)

    assert hits, f"{query!r} 未命中任何制度片段"
    top = hits[0]
    assert expected_section in top.chunk.section
    assert top.score >= knowledge_base.min_score
    assert top.coverage >= knowledge_base.min_coverage
    assert top.to_citation().quote


@pytest.mark.parametrize("query", MISS_CASES)
def test_irrelevant_query_is_rejected_by_thresholds(
    knowledge_base: KnowledgeBase, query: str
) -> None:
    # BM25 检索器始终会返回 Top-K 候选（哪怕相关度为 0），
    # 因此"返回空"必须由双阈值过滤产生，而不是"检索不到候选"。
    candidates = knowledge_base.retrieve(query, top_k=4)
    assert candidates, "预期检索器仍会返回候选，以验证阈值确实是过滤环节"

    assert knowledge_base.search(query, top_k=4) == []
    assert all(
        candidate.score < knowledge_base.min_score
        or candidate.coverage < knowledge_base.min_coverage
        for candidate in candidates
    )


def test_index_rebuilt_when_document_hash_changes(tmp_path: Path) -> None:
    source = tmp_path / "员工守则_美化版.docx"
    shutil.copy(HANDBOOK, source)
    index_path = tmp_path / "index.json"

    first = KnowledgeBase(source, index_path, top_k=4)
    first.initialize()
    original_hash = first.document_hash
    assert original_hash
    assert json.loads(index_path.read_text(encoding="utf-8"))["document_hash"] == original_hash

    # 追加一段正文，使文档哈希发生变化（保持章节结构不变）
    document = docx_lib.Document(str(source))
    document.add_paragraph(REBUILD_MARKER)
    document.save(str(source))

    second = KnowledgeBase(source, index_path, top_k=4)
    second.initialize()

    assert second.document_hash != original_hash
    # 索引文件已按新哈希重写，而不是继续沿用旧缓存
    assert json.loads(index_path.read_text(encoding="utf-8"))["document_hash"] == second.document_hash
    # 新内容确实进入了索引
    assert any(REBUILD_MARKER in item.page_content for item in second.documents)


def test_index_reused_when_document_hash_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index_path = tmp_path / "index.json"
    calls: list[int] = []
    original_rebuild = KnowledgeBase.rebuild

    def counting_rebuild(self: KnowledgeBase) -> int:
        calls.append(1)
        return original_rebuild(self)

    monkeypatch.setattr(KnowledgeBase, "rebuild", counting_rebuild)

    first = KnowledgeBase(HANDBOOK, index_path, top_k=4)
    first.initialize()
    assert len(calls) == 1, "首次初始化应当构建索引"

    second = KnowledgeBase(HANDBOOK, index_path, top_k=4)
    second.initialize()

    assert len(calls) == 1, "文档未变化时不应重建索引"
    assert len(second.documents) == len(first.documents)


def test_verbatim_query_is_not_rejected_by_coverage_dilution(
    knowledge_base: KnowledgeBase,
) -> None:
    """回归：tokenize 的 2/3-gram 会切出跨词碎片，曾稀释覆盖率分母导致原词直问也被拒答。

    "加班有什么规定" 切出 11 个词，其中只有「加班」「规定」真实存在于语料；
    覆盖率改为只统计这部分实质词后，应命中 5.3 加班管理。
    """
    hits = knowledge_base.search("加班有什么规定", top_k=4)

    assert hits, "手册 5.3 明确写了加班规定，用原词提问不应被拒答"
    assert "加班管理" in hits[0].chunk.section
    assert hits[0].coverage >= knowledge_base.min_coverage
    assert hits[0].score >= knowledge_base.min_score


def test_query_outside_knowledge_scope_is_rejected_despite_incidental_keyword(
    knowledge_base: KnowledgeBase,
) -> None:
    """回归：整句落在知识库范围外时，不能因为偶合命中一个词就作答。

    "创建工单要走什么审批" 只有「审批」一词在语料中出现（df=1），
    若只看覆盖率会得到 1.0 分并返回"加班管理"片段，属于答非所问；
    现按"查询可解释度"判为范围外，由双阈值统一过滤。
    """
    candidates = knowledge_base.retrieve("创建工单要走什么审批？", top_k=4)
    assert candidates, "预期检索器仍会返回候选，以验证阈值确实是过滤环节"

    assert knowledge_base.search("创建工单要走什么审批？", top_k=4) == []
