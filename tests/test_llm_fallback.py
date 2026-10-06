"""C. 降级：DeepSeek 不可用 / 未启用 / 返回空时，只返回已命中的制度原文。"""
from __future__ import annotations

import pytest

from app.database import Database
from app.models import Principal

# 实测可通过双阈值、能稳定带出制度依据的问题
GROUNDED_QUESTION = "公司原则上每月几号发放上月工资？"
FALLBACK_HEADER = "DeepSeek 暂时不可用，以下为知识库中的相关原文："


def test_generator_exception_degrades_to_source_text(
    make_agent: object, employee: Principal, database: Database, fake_generator
) -> None:
    generator = fake_generator(error=TimeoutError("mock timeout"))
    agent = make_agent(generator=generator)

    result = agent.run(employee, GROUNDED_QUESTION, "127.0.0.1")

    assert generator.calls == 1
    assert result["answer"].startswith(FALLBACK_HEADER)
    assert result["citations"]
    assert "15 日" in result["answer"]
    # 降级动作必须留痕，便于事后排查模型故障
    failure = database.fetch_one(
        "SELECT outcome FROM audit_logs WHERE action='llm.generate' ORDER BY id DESC LIMIT 1"
    )
    assert failure is not None
    assert failure["outcome"] == "failed"


@pytest.mark.parametrize(
    "generator_kwargs",
    [{"enabled": False, "response": None}, {"enabled": True, "response": None}],
    ids=["generator-disabled", "generator-returns-none"],
)
def test_generator_unavailable_degrades_safely(
    make_agent: object,
    employee: Principal,
    database: Database,
    fake_generator,
    generator_kwargs: dict,
) -> None:
    generator = fake_generator(**generator_kwargs)
    agent = make_agent(generator=generator)

    result = agent.run(employee, GROUNDED_QUESTION, "127.0.0.1")

    assert generator.calls == 1
    assert result["answer"].startswith(FALLBACK_HEADER)
    assert result["citations"]
    if not generator_kwargs["enabled"]:
        # 未启用大模型时不应产生任何 llm.generate 审计事件
        assert database.fetch_all("SELECT id FROM audit_logs WHERE action='llm.generate'") == []


def test_fallback_contains_only_retrieved_source_text(
    make_agent: object, employee: Principal, fake_generator
) -> None:
    """降级输出必须是"固定表头 + 命中原文"的精确拼接，不得掺入模型自身知识。"""
    generator = fake_generator(error=RuntimeError("mock failure"))
    agent = make_agent(generator=generator)

    result = agent.run(employee, GROUNDED_QUESTION, "127.0.0.1")

    citations = result["citations"]
    assert citations

    lines = result["answer"].splitlines()
    assert lines[0] == FALLBACK_HEADER

    # 制度原文自身含换行，因此按"整块拼接结果"逐字比对，而不是按行比对
    body = "\n".join(lines[1:])
    expected = "\n".join(
        f"{index}. {citation['quote']} [{index}]"
        for index, citation in enumerate(citations[:3], start=1)
    )
    # 多一个字都说明掺入了模型自身知识
    assert body == expected
