from __future__ import annotations

from collections.abc import Sequence

from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableSerializable
from langchain_openai import ChatOpenAI

from app.config import Settings


class GroundedAnswerGenerator:
    """LangChain + DeepSeek 回答链；模型只能使用 RAG 检索到的制度片段。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    """你是星河智能企业内部制度助手。
你只能依据“制度上下文”回答，不得使用上下文之外的知识补充或猜测。
回答必须准确、简洁，并使用 [1]、[2] 标注对应制度片段。
不得修改、批准或执行任何业务操作；业务操作只能由系统工具完成。
如果上下文不能直接支持答案，只回复：我不知道，知识库中没有相关内容。""",
                ),
                (
                    "human",
                    "员工问题：{question}\n\n制度上下文：\n{context}",
                ),
            ]
        )
        self.llm: ChatOpenAI | None = None
        self.chain: RunnableSerializable[dict[str, str], str] | None = None
        if self.enabled:
            self.llm = ChatOpenAI(
                model=settings.deepseek_model,
                api_key=settings.deepseek_api_key,
                base_url=settings.deepseek_base_url,
                temperature=0,
                timeout=settings.deepseek_timeout_seconds,
                max_retries=settings.deepseek_max_retries,
            )
            self.chain = self.prompt | self.llm | StrOutputParser()

    @property
    def enabled(self) -> bool:
        key = self.settings.deepseek_api_key
        if not self.settings.deepseek_enabled or key is None:
            return False
        return bool(key.get_secret_value().strip())

    def generate(self, question: str, documents: Sequence[Document]) -> str | None:
        if self.chain is None:
            return None
        context = "\n\n".join(
            f"[{index}] {document.metadata.get('section', '员工守则')}\n{document.page_content}"
            for index, document in enumerate(documents, start=1)
        )
        return self.chain.invoke({"question": question, "context": context}).strip()
