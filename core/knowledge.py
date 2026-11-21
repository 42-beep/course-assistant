"""
知识点抽取与建图。

产出统一结构（无论走 LLM 还是规则）：
{
  "id": "kp_01",
  "name": "傅里叶变换",
  "summary": "把时域信号分解为不同频率的正弦叠加",
  "chunks": [3, 7],          # 引用原文块，便于溯源
  "importance": 1-5,          # 在整门课里的地位
  "difficulty": 1-5,          # 理解难度
  "exam_freq": 1-5,           # 考察频率
  "deps": ["kp_00"],          # 前置知识
  "minutes": 45               # 预计掌握所需分钟数
}

为什么打分要和抽取分开做、并且给死标准：
不写标准，模型会给出一堆 3 分，权重就失去意义，排期算法也就失效了。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from core import merge

BATCH_CHARS = 7000

SYSTEM_PROMPT = """你是一位资深的大学课程助教，擅长把讲义拆解成可教学的知识点。

你的任务：从给定的课程片段中抽取知识点，判断它们的前置依赖，并对每个知识点打三个分。

【打分标准，必须严格遵守，不要都给中间分】
importance（在整门课中的地位）：
  5 = 贯穿全课的核心，不会它后面全听不懂
  4 = 主要章节的核心概念
  3 = 常规知识点
  2 = 补充说明性质
  1 = 边角料、举例、背景介绍

difficulty（理解难度，与重要性无关）：
  5 = 需要多步推导、抽象建模，或依赖较强的数学基础
  4 = 概念抽象，需要反复琢磨
  3 = 需要理解但路径清晰
  2 = 记住定义即可
  1 = 常识性或纯粹记忆

exam_freq（考察频率，按大学期末考试的普遍规律判断）：
  5 = 几乎每年必考，且占分较高
  4 = 高频考点
  3 = 常规考点
  2 = 偶尔出现
  1 = 几乎不考

【依赖规则】
deps 里只能出现本次输出中出现过的知识点 id，或者空数组。
不要把"同一章"当成依赖——依赖必须是真的"不懂 A 就学不会 B"。

【输出格式】只返回 JSON，不要解释：
{
  "points": [
    {
      "name": "知识点名称（不超过 20 字）",
      "summary": "一句话说明它是什么、解决什么问题",
      "chunks": [引用的片段编号],
      "importance": 1-5,
      "difficulty": 1-5,
      "exam_freq": 1-5,
      "deps": ["前置知识点的 name"],
      "minutes": 预计掌握分钟数
    }
  ]
}
注意：deps 用 name 而不是 id，方便跨批次匹配；我会在合并时统一转换。"""


async def build_knowledge(chunks: list[dict], course: str, llm) -> dict[str, Any]:
    if llm.available:
        try:
            points = await _llm_extract(chunks, course, llm)
            mode, note = "llm", ""
        except Exception as e:
            points = _rule_extract(chunks, course)
            mode, note = "rule", f"LLM 抽取失败，已降级为规则模式：{e}"
    else:
        points = _rule_extract(chunks, course)
        mode = "rule"
        note = "未配置模型，使用规则模式（依标题结构与关键词启发式抽取）"

    # 抽取是多批次并发的，各批次之间互不知情，必然产生重复与别名。
    # 所以去重分两级：先字面完全相同的（_dedupe），再同一概念不同叫法的（merge）。
    points = _dedupe(points)
    points, merge_report = await merge.merge_points(points, chunks, llm)
    return _finalize(points, chunks, mode=mode, note=note, merge_report=merge_report)


# ---------------- LLM 抽取 ----------------

async def _llm_extract(chunks: list[dict], course: str, llm) -> list[dict]:
    batches = _make_batches(chunks)
    tasks = [_extract_batch(b, course, llm) for b in batches]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    merged: list[dict] = []
    for r in results:
        if isinstance(r, Exception):
            continue
        merged.extend(r)
    if not merged:
        raise ValueError("模型没有返回任何知识点")
    return merged


async def _extract_batch(batch: list[dict], course: str, llm) -> list[dict]:
    body = "\n\n".join(
        f"[片段 {c['id']}] （{c['heading']} · {c['source']}）\n{c['text']}"
        for c in batch
    )
    user = f"课程名称：{course}\n\n以下是讲义片段：\n\n{body}"
    obj = await llm.json_call(SYSTEM_PROMPT, user)
    pts = obj.get("points") or []
    return [p for p in pts if isinstance(p, dict) and p.get("name")]


def _make_batches(chunks: list[dict]) -> list[list[dict]]:
    batches, cur, size = [], [], 0
    for c in chunks:
        n = len(c["text"])
        if cur and size + n > BATCH_CHARS:
            batches.append(cur)
            cur, size = [], 0
        cur.append(c)
        size += n
    if cur:
        batches.append(cur)
    return batches


# ---------------- 规则抽取 ----------------

_IMP_STRONG = ["核心", "重点掌握", "必须掌握", "骨架", "基础"]
_IMP_MID = ["重点", "掌握", "重要", "关键"]
_IMP_WEAK = ["了解", "补充", "阅读材料", "选学", "扩展", "背景"]

_FREQ_STRONG = ["必考", "每年必", "必出", "必考内容"]
_FREQ_MID = ["高频", "常考", "考点", "考试中常", "考到"]
_FREQ_WEAK = ["几乎不考", "不考"]

_HARD_WORDS = ["推导", "证明", "积分", "微分", "变换", "定理", "抽象", "矩阵", "方程"]

_NUM_HEADING = re.compile(r"^\s*(第[一二三四五六七八九十百\d]+[章节讲部分]|[一二三四五六七八九十]+[、.]|\d+(\.\d+)*[、.\s])")


def _rule_extract(chunks: list[dict], course: str) -> list[dict]:
    """没有模型时的兜底：从章节标题和带编号的段落里提取候选知识点。

    依赖用启发式建立：同一章内，后一个知识点依赖前一个。
    这符合老师讲课的实际顺序，也让拓扑排序有意义（否则排期是散的）。
    """
    points: list[dict] = []
    seen: set[str] = set()
    prev_in_chapter: dict[str, str] = {}

    for c in chunks:
        head = c.get("heading", "")
        parts = [p.strip() for p in head.split(" / ") if p.strip()]
        cand = parts[-1] if parts else ""
        chapter = parts[-2] if len(parts) >= 2 else (parts[0] if parts else "")

        if cand and len(cand) >= 3 and cand not in seen and not _NUM_HEADING.match(cand):
            seen.add(cand)
            points.append(_make_rule_point(cand, c, head, len(parts),
                                           prev_in_chapter.get(chapter)))
            prev_in_chapter[chapter] = cand

        # 正文里带编号的短句，可能是子主题
        for line in c["text"].splitlines():
            line = line.strip()
            if 4 <= len(line) <= 28 and _NUM_HEADING.match(line):
                name = _NUM_HEADING.sub("", line).strip(" :：-—")
                if name and len(name) >= 3 and name not in seen:
                    seen.add(name)
                    points.append(_make_rule_point(name, c, head, 3,
                                                   prev_in_chapter.get(chapter)))
                    prev_in_chapter[chapter] = name

    if not points:
        points.append({
            "name": course or "课程内容",
            "summary": "未能从资料中识别出结构化标题，建议改用 PDF 或手动指定章节",
            "chunks": [chunks[0]["id"]] if chunks else [],
            "importance": 3, "difficulty": 3, "exam_freq": 3,
            "deps": [], "minutes": 60,
        })

    return points


def _make_rule_point(name: str, chunk: dict, heading: str, level: int,
                     prev_name: str | None = None) -> dict:
    text = chunk["text"]
    blob = text + " " + heading + " " + name

    # 重要性：标题层级给基础分，再用关键词升降
    importance = 4 if level <= 2 else 3
    if any(w in blob for w in _IMP_WEAK):
        importance = min(importance, 2)
    if any(w in blob for w in _IMP_MID):
        importance = max(importance, 4)
    if any(w in blob for w in _IMP_STRONG):
        importance = 5

    # 考频：默认常规考点，只有明确出现信号词才动
    exam = 3
    if any(w in blob for w in _FREQ_WEAK):
        exam = 2
    if any(w in blob for w in _FREQ_MID):
        exam = 4
    if any(w in blob for w in _FREQ_STRONG):
        exam = 5

    # 难度：篇幅、公式密度、是否涉及推导
    difficulty = 2
    if len(text) > 250:
        difficulty = 3
    if len(text) > 600:
        difficulty = 4
    if any(w in blob for w in _HARD_WORDS):
        difficulty = max(difficulty, 4)
    formula_ratio = len(re.findall(r"[=∫∑√±≤≥∂]", text)) / max(len(text), 1)
    if formula_ratio > 0.015:
        difficulty = max(difficulty, 4)
    if formula_ratio > 0.035:
        difficulty = 5

    return {
        "name": name[:20],
        "summary": _first_sentence(text),
        "chunks": [chunk["id"]],
        "importance": importance,
        "difficulty": difficulty,
        "exam_freq": exam,
        "deps": [prev_name] if prev_name else [],
        "minutes": 20 + difficulty * 12 + (10 if importance >= 4 else 0),
    }


def _first_sentence(text: str) -> str:
    t = re.sub(r"\s+", " ", text).strip()
    for sep in ("。", "；", ". ", "\n"):
        i = t.find(sep)
        if 10 < i < 80:
            return t[:i + 1]
    return t[:60]


# ---------------- 合并与规范化 ----------------

def _finalize(points: list[dict], chunks: list[dict], mode: str, note: str,
              merge_report: dict | None = None) -> dict[str, Any]:
    points = _dedupe(points)
    _validate_refs(points, chunks)
    name2id = {p["name"]: p["id"] for p in points}

    for p in points:
        deps = []
        for d in p.get("deps") or []:
            d = str(d).strip()
            if d in name2id and name2id[d] != p["id"]:
                deps.append(name2id[d])
        p["deps"] = sorted(set(deps))

    _break_cycles(points)

    return {
        "points": points,
        "mode": mode,
        "note": note,
        "merge": merge_report or {},
        "stats": {
            "count": len(points),
            "total_minutes": sum(p["minutes"] for p in points),
            "hard_count": sum(1 for p in points if p["difficulty"] >= 4),
            "core_count": sum(1 for p in points if p["importance"] >= 4),
            "multi_source": sum(1 for p in points if p.get("coverage", 0) >= 2),
        },
    }


def _validate_refs(points: list[dict], chunks: list[dict]) -> None:
    """引用校验：chunk id 必须真的存在。

    模型给 chunk 编号时会编——它看到的是「片段 3」，很容易顺手写出
    「片段 8」而不管 8 存不存在。这种错误如果放过去，用户点开溯源会
    看到完全无关的一页，然后对整个工具失去信任。

    宁可让引用为空（不显示），也不显示错的。
    """
    valid = {c["id"] for c in chunks}
    for p in points:
        refs = [c for c in (p.get("chunks") or []) if c in valid]
        p["chunks"] = sorted(set(refs))
        p["refs_ok"] = bool(refs)


def _dedupe(points: list[dict]) -> list[dict]:
    by_name: dict[str, dict] = {}
    for p in points:
        name = str(p.get("name", "")).strip()
        if not name:
            continue
        key = re.sub(r"\s+", "", name)
        if key in by_name:
            old = by_name[key]
            # 同名的取更高的分，合并引用块
            for f in ("importance", "difficulty", "exam_freq"):
                old[f] = max(int(old.get(f, 3)), _clamp(p.get(f, 3)))
            old["chunks"] = sorted(set(old.get("chunks") or []) | set(p.get("chunks") or []))
            old["deps"] = list(set(old.get("deps") or []) | set(p.get("deps") or []))
            continue
        p["importance"] = _clamp(p.get("importance", 3))
        p["difficulty"] = _clamp(p.get("difficulty", 3))
        p["exam_freq"] = _clamp(p.get("exam_freq", 3))
        p["minutes"] = max(10, min(int(p.get("minutes") or 45), 240))
        p["chunks"] = sorted({int(c) for c in (p.get("chunks") or []) if str(c).isdigit()})
        p["summary"] = str(p.get("summary") or "").strip()[:200]
        by_name[key] = p

    out = list(by_name.values())
    for i, p in enumerate(out, start=1):
        p["id"] = f"kp_{i:02d}"
    return out


def _clamp(v: Any) -> int:
    try:
        return max(1, min(5, int(round(float(v)))))
    except Exception:
        return 3


def _break_cycles(points: list[dict]) -> None:
    """依赖成环会让拓扑排序死锁，这里直接把回边砍掉。"""
    id2p = {p["id"]: p for p in points}
    state: dict[str, int] = {}   # 0=未访问 1=访问中 2=完成

    def dfs(pid: str) -> None:
        state[pid] = 1
        for d in list(id2p[pid]["deps"]):
            if d not in id2p:
                continue
            if state.get(d) == 1:
                id2p[pid]["deps"].remove(d)   # 回边，砍掉
            elif state.get(d) is None:
                dfs(d)
        state[pid] = 2

    for p in points:
        if state.get(p["id"]) is None:
            dfs(p["id"])
