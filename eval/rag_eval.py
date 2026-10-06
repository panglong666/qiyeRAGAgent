"""RAG 质量评测脚本（无需 DeepSeek Key，不产生任何写库副作用）。

用途：量化知识库检索质量，替代 README「后续方向」里"缺评测集"这一项。
用法：
    .venv\\Scripts\\python.exe -m eval.rag_eval
    .venv\\Scripts\\python.exe -m eval.rag_eval --min-recall 0.6   # 作为回归门禁

指标口径：
    命中@1 / 命中@4  期望章节是否出现在 Top1 / Top4 引用中
    误召            域外问题被错误召回到制度片段（越低越好）
设计原则：域内召回与域外拒答必须同时看。一个"什么都拒答"的系统
拒答准确率天然是 100%，单看拒答率会得出相反结论。
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from app.config import PROJECT_ROOT
from app.rag import KnowledgeBase

SOURCE = PROJECT_ROOT / "员工守则_美化版.docx"

# 三层难度，刻意为难检索：L1 用手册原词，L2 同义改写，L3 口语化跨词表
IN_DOMAIN: dict[str, list[tuple[str, str]]] = {
    "L1 原词直问": [
        ("迟到超过30分钟如何处理？", "考勤方式"),
        ("带薪年假有多少天？", "带薪年假"),
        ("工资什么时候发放？", "薪酬发放"),
        ("试用期辞职要提前几天？", "辞职"),
        ("社会保险和住房公积金怎么缴纳？", "社会保险"),
        ("加班需要申请吗？", "加班管理"),
        ("违纪处分有哪些情形？", "违纪处分"),
        ("保密义务的期限是多久？", "保密义务"),
    ],
    "L2 同义改写": [
        ("上班迟到半小时会怎么处理？", "考勤方式"),
        ("年假标准是多少天？", "带薪年假"),
        ("每月薪资几号到账？", "薪酬发放"),
        ("离职通知期是多久？", "辞职"),
        ("五险一金怎么交？", "社会保险"),
        ("加班要不要审批？", "加班管理"),
        ("哪些行为会被处分？", "违纪处分"),
        ("离职以后还要保密吗？", "保密义务"),
    ],
    "L3 口语化": [
        ("迟到半小时以上怎么算？", "考勤方式"),
        ("每个月几号发钱？", "薪酬发放"),
        ("离职要提前多久打招呼？", "辞职"),
        ("公司给交五险一金吗？", "社会保险"),
        ("被处分了不服气怎么办？", "申诉机制"),
        ("接私活会不会被开除？", "违纪处分"),
        ("我在公司写的代码归谁？", "知识产权归属"),
        ("离职时电脑要还吗？", "办公资源发放"),
        ("出差打不了卡怎么办？", "考勤方式"),
        ("上班能穿拖鞋吗？", "基本行为规范"),
    ],
}

OUT_OF_DOMAIN = [
    "今天食堂的菜单是什么？",
    "公司股票代码是多少？",
    "附近哪家火锅好吃？",
    "帮我写一首诗",
    "今天深圳天气怎么样？",
    "帮我算一下 128 乘以 47",
    "推荐几本 Python 入门书",
    "公司的竞争对手都有谁？",
]

# 疑问句不得触发写操作的回归用例（P0：确定性路由优先于 RAG 的缺陷）
READ_ONLY_PROBES = [
    "请假申请需要提前几天？",
    "请假申请流程是什么",
    "怎么提交请假申请？",
    "创建工单要走什么审批？",
    "工单怎么创建？",
    "怎么申请工单？",
]


def build_knowledge_base() -> KnowledgeBase:
    tmp = Path(tempfile.mkdtemp())
    kb = KnowledgeBase(SOURCE, tmp / "index.json", top_k=4)
    kb.initialize()
    return kb


def evaluate_retrieval(kb: KnowledgeBase, top_k: int) -> tuple[list[str], dict[str, float]]:
    rows: list[str] = []
    summary: dict[str, float] = {}
    rows.append("| 难度层 | 题量 | 命中@1 | 命中@4 | 被拒答 |")
    rows.append("|---|---|---|---|---|")
    total_n = total_h1 = total_h4 = 0
    for tier, items in IN_DOMAIN.items():
        h1 = h4 = rejected = 0
        for question, expected in items:
            hits = kb.search(question, top_k=top_k)
            if not hits:
                rejected += 1
                continue
            sections = [hit.chunk.section for hit in hits]
            if expected in sections[0]:
                h1 += 1
            if any(expected in s for s in sections):
                h4 += 1
        n = len(items)
        total_n += n
        total_h1 += h1
        total_h4 += h4
        rows.append(f"| {tier} | {n} | {h1}/{n} | {h4}/{n} | {rejected}/{n} |")
    rows.append(f"| **合计** | {total_n} | {total_h1}/{total_n} | {total_h4}/{total_n} | - |")
    summary["recall@1"] = total_h1 / total_n
    summary["recall@4"] = total_h4 / total_n
    return rows, summary


def evaluate_rejection(kb: KnowledgeBase, top_k: int) -> tuple[list[str], dict[str, float]]:
    leaked = [q for q in OUT_OF_DOMAIN if kb.search(q, top_k=top_k)]
    n = len(OUT_OF_DOMAIN)
    rows = [
        f"- 域外问题 {n} 题，误召回 {len(leaked)} 题（拒答准确率 {1 - len(leaked) / n:.0%}）",
    ]
    if leaked:
        rows.append(f"- 误召回明细：{'、'.join(leaked)}")
    return rows, {"rejection_accuracy": 1 - len(leaked) / n}


def evaluate_read_only() -> list[str]:
    """跑一遍 Agent，确认疑问句没有产生写操作。"""
    from app.agent import EnterpriseAgent
    from app.audit import AuditLogger
    from app.config import Settings
    from app.database import Database
    from app.models import Principal
    from app.tools import build_tool_registry

    tmp = Path(tempfile.mkdtemp())
    settings = Settings(
        handbook_path=SOURCE,
        database_path=tmp / "probe.db",
        rag_index_path=tmp / "probe-index.json",
        deepseek_enabled=False,
        deepseek_api_key=None,
    )
    database = Database(settings.database_path)
    database.initialize()
    kb = KnowledgeBase(settings.handbook_path, settings.rag_index_path, top_k=4)
    kb.initialize()
    agent = EnterpriseAgent(settings, database, kb, build_tool_registry(), AuditLogger(database))
    principal = Principal(1, "employee", "普通员工", "employee", "产品部")

    rows = ["| 提问 | 命中工具 | 落库写操作 |", "|---|---|---|"]
    violations = 0
    for question in READ_ONLY_PROBES:
        before_leave = database.fetch_one("SELECT COUNT(*) c FROM leave_requests")["c"]
        before_ticket = database.fetch_one("SELECT COUNT(*) c FROM hr_tickets")["c"]
        result = agent.run(principal, question, "127.0.0.1")
        wrote = []
        if database.fetch_one("SELECT COUNT(*) c FROM leave_requests")["c"] > before_leave:
            wrote.append("请假申请")
        if database.fetch_one("SELECT COUNT(*) c FROM hr_tickets")["c"] > before_ticket:
            wrote.append("HR工单")
        if wrote:
            violations += 1
        rows.append(
            f"| {question} | {result['tool']} | {'、'.join(wrote) if wrote else '无'} |"
        )
    rows.append(f"\n- 违反「疑问句只读」的用例：**{violations}/{len(READ_ONLY_PROBES)}**")
    return rows


def evaluate_thresholds(kb: KnowledgeBase, top_k: int) -> list[str]:
    rows = ["| min_score | min_coverage | 域内可回答 | 域外拒答 |", "|---|---|---|---|"]
    in_all = [q for items in IN_DOMAIN.values() for q, _ in items]
    for ms, mc in [(0.05, 0.15), (0.08, 0.20), (0.10, 0.25), (0.15, 0.35)]:
        ok_in = sum(bool(kb.search(q, top_k=top_k, min_score=ms, min_coverage=mc)) for q in in_all)
        ok_out = sum(not kb.search(q, top_k=top_k, min_score=ms, min_coverage=mc) for q in OUT_OF_DOMAIN)
        mark = " ← 当前默认" if (ms, mc) == (0.08, 0.20) else ""
        rows.append(f"| {ms:.2f} | {mc:.2f} | {ok_in}/{len(in_all)} | {ok_out}/{len(OUT_OF_DOMAIN)}{mark} |")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="员工守则 RAG 质量评测")
    parser.add_argument("--top-k", type=int, default=4)
    parser.add_argument("--min-recall", type=float, default=None,
                        help="设置后，Recall@4 低于该值则以非零码退出，可用作回归门禁")
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "eval" / "last_report.md")
    args = parser.parse_args()

    kb = build_knowledge_base()
    lines = ["# RAG 质量评测报告", "", f"- 知识库：`{SOURCE.name}`（{len(kb.documents)} 个知识块）",
             f"- 检索：BM25（LangChain）+ 双阈值 score/coverage", ""]

    lines.append("## 1. 域内召回")
    lines.append("")
    rows, summary = evaluate_retrieval(kb, args.top_k)
    lines.extend(rows)
    lines.append("")
    lines.append(f"Recall@1 = **{summary['recall@1']:.0%}**，Recall@4 = **{summary['recall@4']:.0%}**")
    lines.append("")

    lines.append("## 2. 域外拒答")
    lines.append("")
    rows, _ = evaluate_rejection(kb, args.top_k)
    lines.extend(rows)
    lines.append("")

    lines.append("## 3. 阈值敏感性")
    lines.append("")
    lines.extend(evaluate_thresholds(kb, args.top_k))
    lines.append("")

    lines.append("## 4. 疑问句只读性（P0 回归）")
    lines.append("")
    lines.extend(evaluate_read_only())
    lines.append("")

    report = "\n".join(lines)
    print(report)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report, encoding="utf-8")

    if args.min_recall is not None and summary["recall@4"] < args.min_recall:
        print(f"\n[FAIL] Recall@4 {summary['recall@4']:.0%} < 门槛 {args.min_recall:.0%}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
