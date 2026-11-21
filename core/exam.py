"""
接真题：把「考频」从猜测变成证据。

原来的 exam_freq 是模型看着讲义猜的——它不知道你们老师爱考什么。
真题是唯一能证明「这个知识点真的考过」的东西，所以这一层只做一件事：
拿真题原文去对知识点，命中就把 exam_freq 顶到 4 或 5，并留下证据。

两种模式：
- 有模型：让模型读真题，判断哪些知识点被考过（准，但慢、要钱）
- 没模型：字符匹配（糙，但零成本，且完全可解释）

匹配结果不改变知识点本身，只改 exam_freq / exam_hits / exam_evidence，
所以排期那边一行代码都不用动——_value() 本来就在用 exam_freq。
"""

from __future__ import annotations

import re
from typing import Any

# 出现这些词的知识点名字太泛，拿去做字符串匹配必然误伤
_GENERIC = {
    "定义", "概念", "性质", "方法", "应用", "分析", "计算", "总结",
    "概述", "简介", "基础", "原理", "例题", "习题", "复习", "重点",
}


async def link(points: list[dict], exam_chunks: list[dict], llm=None) -> dict[str, Any]:
    """把真题命中情况写回 points（原地修改），并返回统计。

    llm 可用就走模型，否则走字符匹配。两条路的输出结构完全一样，
    上层不需要知道自己走的是哪条。
    """
    if not exam_chunks or not points:
        return {"enabled": False, "mode": "none", "hits": 0, "covered": 0, "note": ""}

    if llm is not None and getattr(llm, "available", False):
        matched, note = await _llm_match(points, exam_chunks, llm)
        mode = "llm"
    else:
        matched, note = _rule_match(points, exam_chunks)
        mode = "rule"

    hits = 0
    for p in points:
        ev = matched.get(p["id"]) or []
        p["exam_hits"] = len(ev)
        p["exam_evidence"] = ev[:2]
        if ev:
            hits += 1
            # 命中一次顶到 4，多次命中直接 5。
            # 不无条件给 5：真题里提一句和出一道大题，分量不一样。
            p["exam_freq"] = 5 if len(ev) >= 2 else max(int(p.get("exam_freq", 3)), 4)

    return {
        "enabled": True,
        "mode": mode,
        "hits": hits,
        "covered": len(points),
        "note": note,
    }


# ---------------- 规则匹配 ----------------

def _terms(name: str) -> list[str]:
    """从知识点名字里抽可用于匹配的词。

    去掉括号里的补充说明，再按标点切成短语；太短的不要，
    否则「系统」「分析」这种词会在任何一份卷子里命中。
    """
    raw = re.sub(r"[（(][^)）]*[)）]", "", name).strip()
    out: list[str] = []
    for part in re.split(r"[、，,·/\s]+", raw):
        part = part.strip(" ：:—-_·")
        if len(part) >= 3 and part not in _GENERIC:
            out.append(part)
    # 名字本身较短但完整（如「线性表」）时，整名也算一个词
    if not out and 3 <= len(raw) <= 12 and raw not in _GENERIC:
        out.append(raw)
    return out[:4]


def _snippet(text: str, term: str, width: int = 40) -> str:
    i = text.find(term)
    if i < 0:
        return text[:width].replace("\n", " ")
    start = max(0, i - 12)
    return text[start:start + width].replace("\n", " ").strip()


def _rule_match(points: list[dict], exam_chunks: list[dict]) -> tuple[dict[str, list], str]:
    matched: dict[str, list] = {}

    for p in points:
        terms = _terms(p["name"])
        if not terms:
            continue
        ev: list[dict] = []
        for c in exam_chunks:
            text = c["text"]
            hit_term = next((t for t in terms if t in text), None)
            if hit_term:
                ev.append({
                    "source": c["source"],
                    "term": hit_term,
                    "snippet": _snippet(text, hit_term),
                })
            if len(ev) >= 3:      # 够证据了，不用扫完全部
                break
        if ev:
            matched[p["id"]] = ev

    return matched, "按知识点名称与真题原文做字符匹配，只认字面命中"


# ---------------- 模型匹配 ----------------

_SYS = """你是阅卷老师。给你一份课程的知识点清单，和一份历年真题的原文。
判断每个知识点是否在这份真题里真的被考到（出过题，或作为某道题的求解步骤必须用到）。

标准要严：
- 只在题干背景里顺带提到，不算考到
- 必须能从原文找到具体题目，才算
- 拿不准就不算

只输出 JSON：{"matches": [{"id": "kp_01", "evidence": "第 3 题：求 f(t) 的傅里叶变换"}]}
没有命中就返回 {"matches": []}"""


async def _llm_match(points: list[dict], exam_chunks: list[dict], llm) -> tuple[dict[str, list], str]:
    exam_text = _compact(exam_chunks, limit=7000)
    matched: dict[str, list] = {}
    batch = 25

    for i in range(0, len(points), batch):
        group = points[i:i + batch]
        listing = "\n".join(
            f'{p["id"]}｜{p["name"]}｜{p.get("summary", "")[:40]}' for p in group
        )
        user = f"【知识点清单】\n{listing}\n\n【真题原文】\n{exam_text}"
        try:
            data = await llm.json_call(_SYS, user)
        except Exception:
            # 模型这条路失败不要紧，退回字符匹配，别让整次分析挂掉
            return _rule_match(points, exam_chunks)

        valid = {p["id"] for p in group}
        for m in (data.get("matches") or []):
            pid = str(m.get("id", "")).strip()
            if pid not in valid:
                continue
            ev = str(m.get("evidence", "")).strip()[:120] or "真题命中"
            matched.setdefault(pid, []).append({"source": "真题", "term": "", "snippet": ev})

    return matched, "由模型逐条比对真题题干，只认能找到具体题目的命中"


def _compact(chunks: list[dict], limit: int) -> str:
    """真题可能几十页，截断到可控长度；优先保留前面的（大题通常在前）。"""
    out: list[str] = []
    used = 0
    for c in chunks:
        t = c["text"].strip()
        if used + len(t) > limit:
            t = t[: max(0, limit - used)]
        if not t:
            break
        out.append(f"[{c['source']}] {t}")
        used += len(t)
        if used >= limit:
            break
    return "\n\n".join(out)
