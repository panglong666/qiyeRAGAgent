from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_community.document_loaders import Docx2txtLoader
from langchain_community.retrievers import BM25Retriever
from langchain_core.documents import Document
from langchain_core.runnables import RunnableLambda, RunnableSerializable
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.models import Citation


@dataclass(slots=True)
class KnowledgeChunk:
    """兼容旧工具和测试使用的知识块视图，底层数据实际是 LangChain Document。"""

    chunk_id: str
    section: str
    text: str
    source: str
    classification: str = "internal"


@dataclass(slots=True)
class SearchHit:
    document: Document
    score: float
    coverage: float

    @property
    def chunk(self) -> KnowledgeChunk:
        metadata = self.document.metadata
        return KnowledgeChunk(
            chunk_id=str(metadata.get("chunk_id", "unknown")),
            section=str(metadata.get("section", "员工守则")),
            text=self.document.page_content,
            source=str(metadata.get("source", "员工守则_美化版.docx")),
            classification=str(metadata.get("classification", "internal")),
        )

    def to_citation(self) -> Citation:
        chunk = self.chunk
        quote = chunk.text[:220] + ("…" if len(chunk.text) > 220 else "")
        return Citation(
            source=chunk.source,
            section=chunk.section,
            chunk_id=chunk.chunk_id,
            quote=quote,
            score=round(self.score, 4),
        )


def tokenize(text: str) -> list[str]:
    """LangChain BM25 使用的中文分词函数，无需下载额外嵌入模型。

    只产出 2-gram：3-gram 跨越词边界的比例更高（"公司的竞争对手"会切出
    "司的竞""的竞争"），这类碎片在语料里偶合出现时会变成"伪证据"，
    既稀释覆盖率，也会把域外问题误判成有依据。长词的信息由 2-gram 组合覆盖。
    """
    text = text.lower()
    tokens = re.findall(r"[a-z0-9_.-]+|[\u4e00-\u9fff]+", text)
    result: list[str] = []
    for token in tokens:
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            if len(token) == 1:
                result.append(token)
            else:
                result.extend(token[index:index + 2] for index in range(len(token) - 1))
        else:
            result.append(token)
    return result


class KnowledgeBase:
    """LangChain RAG：DOCX 加载、章节识别、递归分块、BM25 检索和 LCEL 检索链。"""

    INDEX_SCHEMA_VERSION = 2

    def __init__(
        self,
        source_path: Path,
        index_path: Path,
        *,
        top_k: int = 4,
        min_score: float = 0.08,
        min_coverage: float = 0.20,
        max_df_ratio: float = 0.5,
        min_substantive_tokens: int = 2,
        chunk_size: int = 800,
        chunk_overlap: int = 120,
    ):
        self.source_path = source_path
        self.index_path = index_path
        self.top_k = top_k
        self.min_score = min_score
        self.min_coverage = min_coverage
        self.max_df_ratio = max_df_ratio
        self.min_substantive_tokens = min_substantive_tokens
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.document_hash = ""
        self.documents: list[Document] = []
        self._corpus_vocab: set[str] = set()
        self._corpus_stopwords: set[str] = set()
        self.retriever: BM25Retriever | None = None
        self.retrieval_chain: RunnableSerializable[str, list[SearchHit]] | None = None

    @property
    def chunks(self) -> list[Document]:
        """保持健康检查和旧调用方使用 len(knowledge_base.chunks) 的兼容性。"""
        return self.documents

    def initialize(self) -> None:
        if not self.source_path.exists():
            raise FileNotFoundError(f"知识库源文件不存在：{self.source_path}")
        current_hash = hashlib.sha256(self.source_path.read_bytes()).hexdigest()
        if self.index_path.exists():
            try:
                payload = json.loads(self.index_path.read_text(encoding="utf-8"))
                if (
                    payload.get("schema_version") == self.INDEX_SCHEMA_VERSION
                    and payload.get("document_hash") == current_hash
                ):
                    self.document_hash = current_hash
                    self.documents = [
                        Document(page_content=item["page_content"], metadata=item["metadata"])
                        for item in payload["documents"]
                    ]
                    self._build_retrieval_chain()
                    return
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
        self.rebuild()

    def rebuild(self) -> int:
        self.document_hash = hashlib.sha256(self.source_path.read_bytes()).hexdigest()
        section_documents = self._load_section_documents()
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=self.chunk_size,
            chunk_overlap=self.chunk_overlap,
            separators=["\n\n", "\n", "。", "；", "，", " "],
            length_function=len,
        )
        self.documents = splitter.split_documents(section_documents)
        for index, document in enumerate(self.documents, start=1):
            document.metadata.update(
                {
                    "chunk_id": f"handbook-{index:03d}",
                    "classification": "internal",
                }
            )
        self._build_retrieval_chain()
        self._persist_index()
        return len(self.documents)

    def _load_section_documents(self) -> list[Document]:
        """先用 LangChain Docx2txtLoader 读取，再将正文整理为带章节元数据的 Document。"""
        loaded = Docx2txtLoader(self.source_path).load()
        if not loaded:
            raise ValueError("员工守则未加载到任何内容")
        paragraphs = [
            re.sub(r"\s+", " ", item).strip()
            for item in re.split(r"\n\s*\n", loaded[0].page_content)
            if item.strip()
        ]

        heading_pattern = re.compile(r"^(\d+)\.(\d+)?\s*(.+)$")
        started = False
        first_section_count = 0
        heading_1 = "员工守则"
        heading_2 = ""
        buffer: list[str] = []
        documents: list[Document] = []

        def flush() -> None:
            nonlocal buffer
            content = "\n".join(buffer).strip()
            if content:
                section = " / ".join(part for part in (heading_1, heading_2) if part)
                documents.append(
                    Document(
                        page_content=content,
                        metadata={
                            "source": self.source_path.name,
                            "section": section,
                            "classification": "internal",
                        },
                    )
                )
            buffer = []

        for paragraph in paragraphs:
            match = heading_pattern.match(paragraph)
            if match and match.group(1) == "1" and not match.group(2):
                first_section_count += 1
                if first_section_count == 2:
                    started = True
            if not started:
                continue
            if match:
                flush()
                if match.group(2):
                    heading_2 = paragraph
                else:
                    heading_1 = paragraph
                    heading_2 = ""
                continue
            buffer.append(paragraph)
        flush()
        if not documents:
            raise ValueError("未能从员工守则识别正文章节")
        return documents

    def _build_retrieval_chain(self) -> None:
        if not self.documents:
            raise ValueError("知识库没有可检索文档")
        # 语料词表 + 语料级通用词：
        #   词表用于把 2/3-gram 切出的"跨词碎片"排除在依据之外；
        #   通用词（如"公司""员工"，df/N 超过一半）几乎每块都有，不携带
        #   主题信息，不能作为"这个问题属于知识库范围"的证据。
        vocab: set[str] = set()
        document_tokens = [self._document_tokens(document) for document in self.documents]
        for tokens in document_tokens:
            vocab |= tokens
        frequency: dict[str, int] = {}
        for tokens in document_tokens:
            for token in tokens:
                frequency[token] = frequency.get(token, 0) + 1
        self._corpus_vocab = vocab
        self._corpus_stopwords = {
            token for token, count in frequency.items() if count / len(document_tokens) > self.max_df_ratio
        }
        self.retriever = BM25Retriever.from_documents(
            self.documents,
            preprocess_func=tokenize,
            k=max(1, min(self.top_k, len(self.documents))),
        )
        # LCEL 检索链把 LangChain Retriever 与可解释的评分步骤组合起来。
        self.retrieval_chain = RunnableLambda(self._retrieve_documents) | RunnableLambda(self._score_documents)

    @staticmethod
    def _document_tokens(document: Document) -> set[str]:
        """知识块的全部可检索词（正文 + 章节标题），标题也参与匹配以便定位章节。"""
        return set(tokenize(document.page_content + " " + str(document.metadata.get("section", ""))))

    def _substantive_tokens(self, query_tokens: set[str]) -> set[str]:
        """查询中的"实质词"：既要在语料里出现，又不能是语料级通用词。"""
        return (query_tokens & self._corpus_vocab) - self._corpus_stopwords

    def _retrieve_documents(self, query: str) -> dict[str, Any]:
        if self.retriever is None:
            return {"query": query, "documents": []}
        return {"query": query, "documents": self.retriever.invoke(query)}

    def _score_documents(self, payload: dict[str, Any]) -> list[SearchHit]:
        if self.retriever is None:
            return []
        query = str(payload["query"])
        query_tokens = tokenize(query)
        unique_query_tokens = set(query_tokens)
        if not unique_query_tokens:
            return []

        # 依据覆盖率只统计「实质词」，解决两个相反方向的问题：
        #   1) 2/3-gram 切出的跨词碎片（"什么规""班有什"）永远不可能命中，
        #      留在分母里会把手册原词直问（"加班有什么规定"）稀释成"依据不足"；
        #   2) 通用词（"公司"）几乎每块都命中，会凭一个不相关词就把
        #      域外问题（"公司股票代码是多少"）误判成"有依据"。
        substantive = self._substantive_tokens(unique_query_tokens)
        if len(substantive) < self.min_substantive_tokens:
            # 实质词不足以定位依据。保留候选是为了让阈值成为唯一的过滤环节
            # （便于观测与测试），但分数与覆盖率置 0，统一由双阈值过滤。
            return [
                SearchHit(document=document, score=0.0, coverage=0.0)
                for document in payload["documents"]
            ]

        raw_scores = self.retriever.vectorizer.get_scores(query_tokens)
        score_by_chunk = {
            str(document.metadata["chunk_id"]): float(score)
            for document, score in zip(self.retriever.docs, raw_scores, strict=True)
        }
        hits: list[SearchHit] = []
        for document in payload["documents"]:
            document_tokens = self._document_tokens(document)
            coverage = len(substantive & document_tokens) / len(substantive)
            raw_score = score_by_chunk.get(str(document.metadata.get("chunk_id")), 0.0)
            normalized = raw_score / max(math.sqrt(len(unique_query_tokens)), 1.0) * coverage
            hits.append(SearchHit(document=document, score=normalized, coverage=coverage))
        hits.sort(key=lambda item: item.score, reverse=True)
        return hits

    def retrieve(self, query: str, top_k: int | None = None) -> list[SearchHit]:
        if self.retrieval_chain is None:
            return []
        hits = self.retrieval_chain.invoke(query)
        return hits[: top_k or self.top_k]

    def search(
        self,
        query: str,
        top_k: int | None = None,
        min_score: float | None = None,
        min_coverage: float | None = None,
    ) -> list[SearchHit]:
        score_threshold = self.min_score if min_score is None else min_score
        coverage_threshold = self.min_coverage if min_coverage is None else min_coverage
        return [
            hit
            for hit in self.retrieve(query, top_k)
            if hit.score >= score_threshold and hit.coverage >= coverage_threshold
        ]

    def _persist_index(self) -> None:
        payload = {
            "schema_version": self.INDEX_SCHEMA_VERSION,
            "document_hash": self.document_hash,
            "source": self.source_path.name,
            "documents": [
                {"page_content": document.page_content, "metadata": document.metadata}
                for document in self.documents
            ],
        }
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.index_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(self.index_path)
