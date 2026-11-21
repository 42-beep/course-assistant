"""
时间倒排：把「知识点 + 剩余天数」变成一张逐日计划表。

这是整个产品的核心算法，也是唯一不调用模型的部分。
好处是可复现、可解释：同样的输入永远得到同样的计划，
而且每个知识点为什么排在第几天，都能说清楚。

核心三步：
  1. 算容量：剩余天数 × 每天可用分钟 ×（1 - 复习预留）
  2. 选内容：容量不够就按「性价比」裁剪，但保证依赖闭包完整
  3. 排顺序：拓扑排序保证前置先学，再贪心填进每一天
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

REVIEW_RATIO = 0.2        # 每天留 20% 时间回顾前一天
SPRINT_THRESHOLD = 7      # 少于 7 天进入冲刺模式


def make_plan(points: list[dict], exam_date: date, minutes_per_day: int,
              today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    days_left = (exam_date - today).days
    if days_left <= 0:
        raise ValueError("考试日期必须晚于今天")

    # ---------- 1. 容量 ----------
    usable = days_left * minutes_per_day * (1 - REVIEW_RATIO)
    demand = sum(p["minutes"] for p in points)
    ratio = usable / demand if demand else 99

    if ratio >= 1.0:
        mode, mode_label = "full", "时间充裕 · 完整覆盖"
    elif ratio >= 0.65:
        mode, mode_label = "trim", "时间偏紧 · 按权重取舍"
    elif ratio >= 0.35:
        mode, mode_label = "core", "时间紧张 · 只保核心与高频"
    else:
        mode, mode_label = "sprint", "极限冲刺 · 只保必考"

    # ---------- 2. 选内容 ----------
    kept, dropped = _select(points, usable, mode)

    # ---------- 3. 排顺序 + 分天 ----------
    ordered = _topo_sort(kept, by_value=(mode in ("core", "sprint")))
    daily_budget = int(minutes_per_day * (1 - REVIEW_RATIO))
    days = _distribute(ordered, days_left, daily_budget, today, mode)

    total_min = sum(it["minutes"] for d in days for it in d["items"])
    return {
        "mode": mode,
        "mode_label": mode_label,
        "exam_date": exam_date.isoformat(),
        "days_left": days_left,
        "minutes_per_day": minutes_per_day,
        "days": days,
        "dropped": dropped,
        "summary": {
            "kept": len(kept),
            "dropped": len(dropped),
            "total_minutes": total_min,
            "capacity_minutes": int(usable),
            "load_ratio": round(demand / max(usable, 1), 2),
            "strategy": _strategy_text(mode, ratio),
        },
    }


# ---------------- 选择 ----------------

def _value(p: dict) -> float:
    """性价比：越重要、越常考、越不难，单位时间收益越高。

    两个额外加权项，都是「证据」而非「预测」：
    - exam_hits：真题里出现过几次。考过就是考过，比模型猜的考频硬得多。
    - coverage：几份资料都讲到它。三个老师都讲同一个东西，它就该排前面。

    这两个加起来上限 1.95，不会盖过重要性本身，但足以在同分时决定先后。
    单份资料时 coverage 全是 1，这一项恒为 0，行为与之前完全一致。
    """
    exam_bonus = min(int(p.get("exam_hits") or 0), 3) * 0.35
    cover_bonus = min(max(int(p.get("coverage") or 1) - 1, 0), 3) * 0.3
    return (p["importance"] * 0.35
            + p["exam_freq"] * 0.45
            - p["difficulty"] * 0.18
            + 0.9
            + exam_bonus
            + cover_bonus)


def _select(points: list[dict], usable: float, mode: str) -> tuple[list[dict], list[dict]]:
    # 真题命中过的先排：证据优先于预测。
    # 否则会出现「考过的被丢掉、没考过的留下」——那是因为 exam_freq 是猜的，
    # 而 exam_hits 是查到的，两者的可信度不在一个量级。
    ranked = sorted(
        points,
        key=lambda p: (int(p.get("exam_hits") or 0) > 0, _value(p)),
        reverse=True,
    )
    id2p = {p["id"]: p for p in points}

    kept_map: dict[str, dict] = {}
    dropped: list[dict] = []
    budget = usable

    for p in ranked:
        if mode == "full":
            cost = p["minutes"]
        elif mode == "trim":
            cost = p["minutes"]
        elif mode == "core":
            # 核心、高频、真题命中过的一律精学，其余速览
            if p["importance"] >= 4 or p["exam_freq"] >= 4 or int(p.get("exam_hits") or 0):
                cost = p["minutes"]
            else:
                cost = int(p["minutes"] * 0.45)
        else:  # sprint
            # 真题命中过的绝不放弃——那不是预测，是已经发生过的事
            proven = int(p.get("exam_hits") or 0) > 0
            if proven:
                cost = int(p["minutes"] * 0.7)
            elif p["exam_freq"] >= 4 and p["importance"] >= 3:
                cost = int(p["minutes"] * 0.6)
            elif p["exam_freq"] >= 3:
                cost = int(p["minutes"] * 0.4)
            else:
                dropped.append({**_brief(p), "reason": "冲刺阶段放弃，考频低且真题未出现"})
                continue

        cost = max(cost, 10)

        # 依赖闭包：要留下它，就必须留下它的前置
        missing = [d for d in p["deps"] if d not in kept_map and d in id2p]
        closure_cost = sum(max(int(id2p[d]["minutes"] * 0.4), 10) for d in missing)

        if cost + closure_cost > budget and kept_map:
            dropped.append({**_brief(p), "reason": _drop_reason(mode, p)})
            continue

        for d in missing:
            dp = id2p[d]
            kept_map[d] = {**dp, "level": "速览（前置）", "plan_minutes": max(int(dp["minutes"] * 0.4), 10)}
            budget -= kept_map[d]["plan_minutes"]

        level = "精学" if cost >= p["minutes"] * 0.9 else ("标准" if cost >= p["minutes"] * 0.5 else "速览")
        kept_map[p["id"]] = {**p, "level": level, "plan_minutes": cost}
        budget -= cost

    kept = [kept_map[p["id"]] for p in points if p["id"] in kept_map]

    # 同一个知识点可能先被判为放弃、后来又作为别人的前置被拉回来。
    # 以「已保留」为准，从放弃列表里剔除，否则统计会对不上。
    kept_names = {p["name"] for p in kept}
    dropped = [d for d in dropped if d["name"] not in kept_names]
    return kept, dropped


def _drop_reason(mode: str, p: dict | None = None) -> str:
    """放弃原因要说人话：用户看到「考频一般」但那个点是 5 分，会觉得工具在乱来。"""
    p = p or {}
    if int(p.get("exam_hits") or 0):
        return "真题命中，但时间装不下——建议单独抽 20 分钟看它"
    if int(p.get("exam_freq") or 3) >= 4:
        return "考频高但时间不够，且真题未出现"
    return {
        "trim": "时间不足，按权重取舍",
        "core": "非核心且考频一般",
        "sprint": "冲刺阶段放弃，考频低",
        "full": "容量外",
    }.get(mode, "容量外")


def _brief(p: dict) -> dict:
    return {"name": p["name"], "importance": p["importance"],
            "exam_freq": p["exam_freq"], "difficulty": p["difficulty"],
            "coverage": p.get("coverage", 1)}


# ---------------- 拓扑排序 ----------------

def _topo_sort(points: list[dict], by_value: bool = False) -> list[dict]:
    """拓扑排序。

    by_value=False：同层按原始顺序（即讲义的章节顺序）——
        时间充裕时应该按老师讲课的顺序走，章节之间的铺垫是有意义的。
    by_value=True：同层按性价比——
        时间紧张时才值得打乱顺序先啃高频，否则学生跟不上。
    """
    id2p = {p["id"]: p for p in points}
    order_idx = {p["id"]: i for i, p in enumerate(points)}
    indeg = {p["id"]: 0 for p in points}
    children: dict[str, list[str]] = {p["id"]: [] for p in points}

    for p in points:
        for d in p["deps"]:
            if d in id2p:
                indeg[p["id"]] += 1
                children[d].append(p["id"])

    def sort_key(i: str) -> float:
        return _value(id2p[i]) if by_value else -order_idx[i]

    ready = sorted([i for i, v in indeg.items() if v == 0], key=sort_key, reverse=True)
    out: list[str] = []
    while ready:
        cur = ready.pop(0)
        out.append(cur)
        for c in children[cur]:
            indeg[c] -= 1
            if indeg[c] == 0:
                ready.append(c)
        ready.sort(key=sort_key, reverse=True)

    # 理论上不会有环（已在抽取阶段砍掉），兜底
    for p in points:
        if p["id"] not in out:
            out.append(p["id"])
    return [id2p[i] for i in out]


# ---------------- 分天 ----------------

def _distribute(ordered: list[dict], days_left: int, daily_budget: int,
                today: date, mode: str) -> list[dict]:
    """把知识点铺进日历。

    关键决策：学习日是连续排还是均匀铺开。
    如果 30 天的内容 8 天就学完了，连续排会让后面 22 天变成空白。
    按间隔重复的规律，正确做法是把学习日均匀铺开，中间夹复习日——
    这比「一口气学完再复习」的记忆效果更好，也更符合真实备考节奏。
    """
    total = sum(p["plan_minutes"] for p in ordered)
    # 按装箱模拟算需要几天，不能简单用 total/budget——
    # 每个知识点 78 分钟、每天 96 分钟时，一天只能装一个，
    # 8 个点需要 8 天，而不是 6 天。
    need, acc = 1, 0
    for p in ordered:
        if acc + p["plan_minutes"] > daily_budget and acc > 0:
            need += 1
            acc = p["plan_minutes"]
        else:
            acc += p["plan_minutes"]
    need = max(1, min(need, days_left))

    if need >= days_left * 0.7:
        study_pos = list(range(min(need, days_left)))
    else:
        step = days_left / need
        study_pos = sorted({min(days_left - 1, int(i * step)) for i in range(need)})

    study_set = set(study_pos)
    last_study = max(study_pos)

    queue = list(ordered)
    idx = 0
    days: list[dict] = []
    learned: list[str] = []

    for d in range(days_left):
        cur = today + timedelta(days=d + 1)
        is_last = (d == days_left - 1)
        label = f"第 {d + 1} 天" + ("（考试）" if is_last else "")

        # 只要队列还有内容，这一天就排学习；不再因为"是最后一天"而跳过
        if d in study_set and idx < len(queue):
            items: list[dict] = []
            used = 0
            while idx < len(queue):
                p = queue[idx]
                cost = p["plan_minutes"]
                if used + cost > daily_budget and items and d != last_study:
                    break
                items.append({
                    "id": p["id"], "name": p["name"], "summary": p["summary"],
                    "level": p["level"], "minutes": cost,
                    "difficulty": p["difficulty"], "importance": p["importance"],
                    "exam_freq": p["exam_freq"], "chunks": p["chunks"],
                    "exam_hits": p.get("exam_hits", 0),
                    "exam_evidence": p.get("exam_evidence") or [],
                    "coverage": p.get("coverage", 1),
                    "files": p.get("files") or [],
                })
                used += cost
                idx += 1

            if items:
                days.append({
                    "date": cur.isoformat(), "day": d + 1, "label": label,
                    "theme": _theme(items), "type": "study",
                    "items": items,
                    "review": [{"name": n, "minutes": 5} for n in learned[-3:]],
                    "total_minutes": used,
                })
                learned.extend(i["name"] for i in items)
                continue

        # 非学习日：复习 / 自测
        if is_last:
            theme = "考前总览 · 只看错题和公式"
        elif d == 0:
            theme = "热身 · 翻一遍目录建立框架"
        else:
            theme = "间隔回顾 · 巩固已学内容"
        days.append({
            "date": cur.isoformat(), "day": d + 1, "label": label,
            "theme": theme,
            "type": "mock" if is_last else "review",
            "items": [],
            "review": [{"name": n, "minutes": 8} for n in learned[-5:]],
            "total_minutes": int(daily_budget * 0.6),
        })

    # 兜底：容量估算再准也可能有剩余，宁可压进最后一个学习日，也不能丢内容。
    # 计划表少一个知识点，用户是发现不了的——这种静默丢失最伤信任。
    if idx < len(queue):
        target = next((d for d in reversed(days) if d["type"] == "study"), None)
        for p in queue[idx:]:
            extra = {
                "id": p["id"], "name": p["name"], "summary": p["summary"],
                "level": "速览", "minutes": max(int(p["plan_minutes"] * 0.4), 5),
                "difficulty": p["difficulty"], "importance": p["importance"],
                "exam_freq": p["exam_freq"], "chunks": p["chunks"],
                "exam_hits": p.get("exam_hits", 0),
                "exam_evidence": p.get("exam_evidence") or [],
                "coverage": p.get("coverage", 1),
                "files": p.get("files") or [],
            }
            if target is not None:
                target["items"].append(extra)
                target["total_minutes"] += extra["minutes"]
            else:
                days[0]["items"].append(extra)
                days[0]["type"] = "study"

    return days


def _theme(items: list[dict]) -> str:
    if not items:
        return "复盘"
    hard = [i for i in items if i["difficulty"] >= 4]
    if len(hard) >= 2:
        return "硬骨头日 · " + "、".join(i["name"] for i in hard[:2])
    names = [i["name"] for i in items]
    if len(names) == 1:
        return names[0]
    return names[0] + " 等 " + str(len(names)) + " 项"


def _strategy_text(mode: str, ratio: float) -> str:
    if mode == "full":
        return "时间足够完整覆盖。按依赖顺序系统学，配合间隔复习；不要提前押题，覆盖本身就是优势。"
    if mode == "trim":
        return "内容略多于时间。优先保证核心与高频，次要章节只求看懂，不做深推。"
    if mode == "core":
        return "时间明显不足。放弃低权重章节，把精力集中在会直接影响及格线的内容上。"
    return "极限冲刺。只保必考与高频，其余一律放弃。目标是拿分，不是学懂。"
