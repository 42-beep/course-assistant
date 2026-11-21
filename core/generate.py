"""
每日内容生成：给排好的计划补上「怎么学」的部分。

排期算法只回答"哪天学什么"，这里回答"怎么学、容易错在哪、去哪找补充材料"。

成本控制：
按天逐条调用会把 token 打爆（30 天 = 30 次）。所以按 4 天一批，
且只对精学/标准级别的知识点生成详细说明，速览的用模板带过。
"""

from __future__ import annotations

import asyncio
from typing import Any

BATCH_DAYS = 4
MAX_BATCHES = 8

SYSTEM_PROMPT = """你是大学课程的辅导老师，正在给学生写每日复习的执行说明。

对每个知识点，你要给出三条：
1. why：为什么它值得花这个时间（结合重要性/考频，一句话）
2. how：具体怎么学（动作要具体，比如"先自己推导一遍，再对照讲义第几条结论"，
   禁止写"认真理解""多加练习"这种空话）
3. pitfall：最容易错或最容易混淆的地方（要具体，指向某一个常见的误解）

另外为每天给一个 goal：这一天结束时，学生应该能做到什么（可检验，不要写"掌握 XX"）。

【输出格式】只返回 JSON：
{
  "days": [
    {
      "day": 1,
      "goal": "能独立写出……的推导过程",
      "points": [
        {"name": "知识点名称", "why": "...", "how": "...", "pitfall": "..."}
      ]
    }
  ]
}
注意：points 里的 name 必须和输入里的知识点名称完全一致，不要改写。"""


async def add_guidance(plan: dict, chunk_map: dict[int, dict], llm) -> dict[str, Any]:
    """原地补充 guidance 字段，返回同一个 plan。"""
    if llm.available:
        try:
            await _llm_guidance(plan, llm)
            plan["guidance_mode"] = "llm"
            return plan
        except Exception as e:
            plan["guidance_note"] = f"模型生成失败，已用模板兜底：{e}"

    _template_guidance(plan, chunk_map)
    plan["guidance_mode"] = "template"
    return plan


# ---------------- LLM ----------------

async def _llm_guidance(plan: dict, llm) -> None:
    study_days = [d for d in plan["days"] if d["type"] == "study" and d["items"]]
    if not study_days:
        return

    batches = [study_days[i:i + BATCH_DAYS] for i in range(0, len(study_days), BATCH_DAYS)][:MAX_BATCHES]
    results = await asyncio.gather(*[_guidance_batch(b, llm) for b in batches],
                                   return_exceptions=True)

    by_day: dict[int, dict] = {}
    for r in results:
        if isinstance(r, Exception):
            continue
        for d in r.get("days", []):
            try:
                by_day[int(d.get("day"))] = d
            except Exception:
                continue

    for day in plan["days"]:
        detail = by_day.get(day["day"])
        if not detail:
            continue
        day["goal"] = str(detail.get("goal", "")).strip()[:120]
        pmap = {str(p.get("name", "")).strip(): p for p in detail.get("points", []) if isinstance(p, dict)}
        for it in day["items"]:
            p = pmap.get(it["name"])
            if p:
                it["why"] = str(p.get("why", ""))[:200]
                it["how"] = str(p.get("how", ""))[:300]
                it["pitfall"] = str(p.get("pitfall", ""))[:200]


async def _guidance_batch(days: list[dict], llm) -> dict:
    lines = []
    for d in days:
        lines.append(f"第 {d['day']} 天（{d['date']}，约 {d['total_minutes']} 分钟）：")
        for it in d["items"]:
            lines.append(
                f"  - {it['name']}｜{it['level']}｜重要度{it['importance']} "
                f"考频{it['exam_freq']} 难度{it['difficulty']}｜{it['summary'][:80]}"
            )
    user = "下面是一份复习计划中的几天，请为每天写执行说明：\n\n" + "\n".join(lines)
    return await llm.json_call(SYSTEM_PROMPT, user)


# ---------------- 模板兜底 ----------------

_LEVEL_ACTION = {
    "精学": "把这一节从头推一遍，合上讲义自己复述一次结论和推导路径，再回原文核对差异",
    "标准": "先看结论和例题，再回头补中间步骤；重点是知道什么时候用得上它",
    "速览": "只读定义和一句话结论，遇到推导直接跳过，记住它大概在讲什么即可",
    "速览（前置）": "只需知道它是什么、什么时候会用到，不必展开",
}


def _template_guidance(plan: dict, chunk_map: dict[int, dict]) -> None:
    for day in plan["days"]:
        if day["type"] == "mock":
            day["goal"] = "只看错题和公式，不做新题；把最容易混的两三个点再确认一遍"
            continue
        if day["type"] == "review":
            day["goal"] = "合上讲义回忆一遍已学内容，再翻回去核对记错的地方"
            continue

        names = [it["name"] for it in day["items"]]
        day["goal"] = ("结束后能用自己的话讲清 " + "、".join(names[:3])) if names else "复盘前一天内容"

        for it in day["items"]:
            it["why"] = _why(it)
            it["how"] = _LEVEL_ACTION.get(it["level"], _LEVEL_ACTION["标准"])
            it["pitfall"] = _pitfall(it)
            ref = chunk_map.get(it["chunks"][0]) if it.get("chunks") else None
            if ref:
                it["how"] = f"{it['how']}。对应原文：{ref['source']} · {ref['heading']}"


def _why(it: dict) -> str:
    imp, freq = it["importance"], it["exam_freq"]
    if imp >= 5:
        return "整门课的骨架，不会它后面的章节都接不上"
    if freq >= 5:
        return "每年必考，且占分较高，投入产出比最高"
    if imp >= 4 and freq >= 4:
        return "核心章节的主考点，值得优先拿下"
    if it["difficulty"] >= 4:
        return "难点内容，早一点啃比留到最后更省时间"
    return "常规知识点，按标准节奏过一遍即可"


def _pitfall(it: dict) -> str:
    if it["difficulty"] >= 4:
        return "容易停留在「看得懂」但没有独立推过一遍；务必合上讲义自己写一次"
    if it["exam_freq"] >= 4:
        return "容易只记结论不记适用条件，题目一变形就用错"
    return "容易看过就忘，建议当天晚上用三句话复述一次"


# ---------------- 资源建议 ----------------

def build_resources(plan: dict, chunks: list[dict], chunk_map: dict[int, dict], course: str) -> list[dict]:
    """资源分两类：资料原文（可溯源）和外部检索建议（给关键词，不给链接）。"""
    res: list[dict] = []

    used: dict[int, list[str]] = {}
    for day in plan["days"]:
        for it in day["items"]:
            if it["level"] in ("精学", "标准"):
                for cid in it.get("chunks", []):
                    used.setdefault(cid, []).append(it["name"])

    for cid, names in sorted(used.items(), key=lambda kv: -len(kv[1]))[:8]:
        c = chunk_map.get(cid)
        if not c:
            continue
        res.append({
            "type": "internal",
            "title": f"{c['source']} · {c['heading']}",
            "detail": "支撑 " + "、".join(names[:3]) + (" 等" if len(names) > 3 else ""),
            "excerpt": c["text"][:160].replace("\n", " "),
        })

    hard = [it for d in plan["days"] for it in d["items"] if it["difficulty"] >= 4][:5]
    for it in hard:
        res.append({
            "type": "external",
            "title": f"补充讲解：{it['name']}",
            "detail": "建议按这些关键词检索视频或图文讲解，优先找带例题的",
            "excerpt": f"{course} {it['name']} 例题 推导",
        })

    freq = [it for d in plan["days"] for it in d["items"] if it["exam_freq"] >= 4][:4]
    if freq:
        res.append({
            "type": "external",
            "title": "历年真题定位",
            "detail": "优先找本校近三年期末卷，只做高频知识点对应的题",
            "excerpt": "、".join(it["name"] for it in freq),
        })

    return res
