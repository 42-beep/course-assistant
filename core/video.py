"""
配套视频检索。

为什么不直接给「去 B 站搜 XX」这种建议：
那不是建议，是把活推回给用户。他打开 B 站，看到 20 条标题，还是不知道点哪个——
这跟他自己搜没有任何区别，工具等于没做。

所以这里真的去搜，并且替他做一件事：**从 20 条里挑出能看的那一条**。

难点不在调接口，在下面三处，逐条说：

1. 搜什么词
   「数据结构 快速傅里叶变换(FFT)」这种拼接搜出来一半是无关的。
   课程名有用（能把同名不同课的内容分开），但必须放在前面，
   且知识点名里的括号要拆开（中文平台对全角括号很不友好）。

2. 怎么挑
   按播放量排是错的。实测「三分钟教会你遍历二叉树」71 万播放，
   但 3 分钟的视频教不会任何东西；真正有用的是 15 分钟那节课。
   所以打分是「播放量 × 时长合理性 × 标题命中 − 营销词」。

3. 它随时会挂
   B 站没有公开的搜索 API，这里用的是网页端接口，需要先访问首页拿 buvid3
   cookie，否则返回 -412。它不是契约，可能明天就加签名、加验证。
   所以：结果缓存 7 天、失败静默降级、整体超时，绝不能拖垮主流程。

换句话说：接口是最简单的一环，产品价值全在前面两条。
"""

from __future__ import annotations

import asyncio
import html
import json
import math
import os
import re
import time
from typing import Any

import httpx

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_PATH = os.path.join(BASE, "data", "video_cache.json")
CACHE_TTL = 7 * 24 * 3600

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

SEARCH_URL = "https://api.bilibili.com/x/web-interface/search/type"

# 这些词出现在标题里，基本可以判定是「应试技巧」而不是「讲解」。
# 不是不能用，是排在后面——学生缺的是听懂，不是再背一个口诀。
_MARKETING = ["速成", "秒杀", "秒解", "三分钟", "一学就会", "不挂科", "救命",
              "包过", "必过", "背下来", "口诀", "无脑"]

_MAX_POINTS = 8       # 最多给 8 个知识点搜，多了会触发风控
_PER_POINT = 2        # 每个知识点留几条
_TIMEOUT = 25         # 整段视频检索的总预算，超了就放弃
_CONCURRENCY = 3


async def attach(plan: dict, course: str, max_points: int = _MAX_POINTS) -> dict[str, Any]:
    """给计划里最值得看外部讲解的知识点挂上视频。原地改 plan。

    只给「精学 / 标准」级别的知识点搜——速览的内容不配视频，
    因为看 20 分钟视频就为了一个只花 5 分钟翻过去的点，是负收益。
    """
    targets = _pick_targets(plan, max_points)
    if not targets:
        return {"enabled": False, "found": 0, "note": "没有需要外部讲解的知识点"}

    queries = {it["name"]: _build_query(course, it["name"]) for it in targets}
    try:
        results = await asyncio.wait_for(_search_many(list(queries.values())), timeout=_TIMEOUT)
    except asyncio.TimeoutError:
        return {"enabled": False, "found": 0, "note": "视频检索超时，已跳过（不影响计划）"}
    except Exception as e:
        return {"enabled": False, "found": 0, "note": f"视频检索失败：{str(e)[:80]}"}

    used: set[str] = set()      # 跨知识点去重：同一条视频别在计划里铺满
    found = _assign(targets, queries, results, used)

    # 带课程名搜不到，就去掉课程名再试一次。
    # 这门课叫「数据结构」，但 FFT 的内容其实是数字信号处理的——
    # 课程名能提高精度，也会误杀本来正确的召回，所以失败时要退一步。
    missing = [it for it in targets if not it.get("videos")]
    if missing:
        q2 = {it["name"]: _build_query("", it["name"]) for it in missing}
        try:
            r2 = await asyncio.wait_for(_search_many(list(q2.values())), timeout=12)
            found += _assign(missing, q2, r2, used)
        except Exception:
            pass

    return {
        "enabled": True,
        "found": found,
        "searched": len(targets),
        "missing": len(targets) - found,
        "note": "" if found else "搜到了结果但没有一条真的在讲这个知识点，已全部过滤",
    }


def _assign(targets: list[dict], queries: dict[str, str],
            results: dict[str, list[dict]], used: set[str]) -> int:
    """把候选分配给知识点，返回这次挂上的知识点数。"""
    n = 0
    for it in targets:
        if it.get("videos"):
            continue
        cands = results.get(queries[it["name"]]) or []
        picked = _pick(cands, _core_terms(it["name"]), _PER_POINT, used)
        if picked:
            it["videos"] = picked
            used.update(v["bvid"] for v in picked)
            n += 1
    return n


# ---------------- 选题 ----------------

def _pick_targets(plan: dict, limit: int) -> list[dict]:
    items = [it for d in plan["days"] for it in d["items"]
             if it.get("level", "").startswith(("精学", "标准"))]
    # 难的优先（最需要有人讲）、其次是分值高的
    items.sort(key=lambda it: (it["difficulty"] >= 4, it["exam_freq"], it["importance"]),
               reverse=True)
    return items[:limit]


def _build_query(course: str, name: str) -> str:
    """课程名放前面能有效区分同名内容，括号要拆开。

    「快速傅里叶变换(FFT)」直接搜，中文平台的召回很差；
    拆成「快速傅里叶变换 FFT」召回明显变好。
    """
    flat = re.sub(r"[（(]([^）)]*)[）)]", r" \1 ", name)
    flat = re.sub(r"\s+", " ", flat).strip()
    base = f"{course} {flat}".strip() if course else flat
    return base[:50]


def _core_terms(name: str) -> dict:
    """从知识点名里取出判断相关性的词，分成「必须」和「任一」两类。

    这一步是整个功能的关键，也是踩过坑的地方。

    第一版用的是「任一核心词命中标题就算相关」，核心词取完整名 + 前 4 字 + 后 4 字。
    结果「FFT复杂度分析」搜出来两条「句法复杂度分析器」——
    因为后 4 字截出来是「杂度分析」，标题里正好有「句法复杂度分析器」。
    跨领域污染就是这么来的：截断会产生语义上不存在的词。

    所以改成两层：
    - required：名字里的英文缩写（FFT / TCP / APR）。**必须命中**。
      名字里带缩写的概念，在原文里就是靠这个缩写标识的，
      它也是唯一能把「FFT 的复杂度」和「句法复杂度」分开的东西。
    - any：中文部分（完整名 + 前后 4 字）。命中任一即可。

    没有英文缩写时 required 为空，退化回「任一命中」。
    """
    inner = re.findall(r"[（(]([^）)]*)[）)]", name)
    clean = re.sub(r"[（(][^）)]*[）)]", "", name)

    required = sorted({w.lower() for seg in inner for w in re.findall(r"[A-Za-z]{3,}", seg)})
    # 名字正文里的英文（不带括号的情况，比如「FFT复杂度分析」）
    required += [w.lower() for w in re.findall(r"[A-Za-z]{3,}", clean)]
    required = sorted(set(required))

    zh = re.sub(r"[A-Za-z0-9\s\-_]+", "", clean)
    any_terms: set[str] = set()
    if len(zh) >= 2:
        any_terms.add(zh)
    if len(zh) >= 4:
        any_terms.add(zh[:4])
        any_terms.add(zh[-4:])
    if not any_terms:
        any_terms.add(clean.strip())

    return {
        "required": required,
        "any": [t for t in any_terms if len(t) >= 2],
    }


def _relevant(title: str, spec: dict) -> bool:
    """标题里带不带「的」不算区别：「二叉树的遍历」和「二叉树遍历」是一回事。

    只去这一个助词，不删别的——删多了会像合并模块那样产生误匹配。
    """
    t = title.lower().replace("的", "")
    # 有缩写就必须命中缩写：这是唯一可靠的跨领域区分信号
    if spec["required"] and not all(x in t for x in spec["required"]):
        return False
    return any(x.lower().replace("的", "") in t for x in spec["any"]) if spec["any"] else True


def _pick(cands: list[dict], spec: dict, limit: int, used: set[str] | None = None) -> list[dict]:
    """先卡相关性，再按分数排序。顺序不能反——打分再高，不相关也是垃圾。

    used 是前面知识点已经用掉的视频。没有完全排除，只是降权：
    一个视频确实同时讲两个点时，重复推荐是对的；但优先给没推过的让路，
    否则十个知识点全推同一个 UP 的合集，等于没挑。
    """
    used = used or set()
    ok = [v for v in cands if _relevant(v["title"], spec)]
    for v in ok:
        v["_score"] = _score(v, spec) - (0.8 if v["bvid"] in used else 0)
    ok.sort(key=lambda v: v["_score"], reverse=True)
    # 拷贝再清理：cands 是缓存里的对象，直接 pop 会让下次读缓存少字段
    out = [dict(v) for v in ok[:limit]]
    for v in out:
        for f in ("_score", "_secs", "_danmaku"):
            v.pop(f, None)
    return out


# ---------------- 检索 ----------------

async def _search_many(queries: list[str]) -> dict[str, list[dict]]:
    cache = _load_cache()
    todo = [q for q in queries if not _cache_ok(cache, q)]
    out: dict[str, list[dict]] = {q: cache[q]["items"] for q in queries if _cache_ok(cache, q)}

    if todo:
        sem = asyncio.Semaphore(_CONCURRENCY)
        async with httpx.AsyncClient(
            headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"},
            timeout=12, follow_redirects=True,
        ) as client:
            # 必须先访问首页拿 buvid3，否则搜索接口直接 -412
            try:
                await client.get("https://www.bilibili.com/")
            except Exception:
                pass

            async def one(q: str) -> None:
                async with sem:
                    out[q] = await _search_one(client, q)
                    if out[q]:
                        cache[q] = {"ts": int(time.time()), "items": out[q]}

            await asyncio.gather(*(one(q) for q in todo), return_exceptions=True)
        _save_cache(cache)

    return out


async def _search_one(client: httpx.AsyncClient, query: str) -> list[dict]:
    """只拿候选，不在这里过滤排序。

    候选原样进缓存，过滤和打分在读取时做——这样调阈值、改权重都不用清缓存，
    而且旧缓存能立刻用上新规则。
    """
    try:
        r = await client.get(SEARCH_URL, params={
            "search_type": "video", "keyword": query, "page": 1,
        })
        data = r.json()
    except Exception:
        return []
    if data.get("code") != 0:
        return []

    raw = (data.get("data") or {}).get("result") or []
    return [v for v in (_normalize(x) for x in raw) if v]


def _normalize(v: dict) -> dict | None:
    """把接口返回整理成统一结构。"""
    if v.get("type") and v["type"] != "video":
        return None          # 课堂/番剧之类没有 bvid，点了跳不过去
    bvid = v.get("bvid")
    if not bvid:
        return None

    title = re.sub(r"<[^>]+>", "", html.unescape(v.get("title") or "")).strip()
    if not title:
        return None

    pic = v.get("pic") or ""
    return {
        "title": title,
        "author": (v.get("author") or "").strip(),
        "play": int(v.get("play") or 0),
        "duration": (v.get("duration") or "").strip(),
        "url": f"https://www.bilibili.com/video/{bvid}",
        "cover": ("https:" + pic) if pic.startswith("//") else pic,
        "bvid": bvid,
        "_secs": _secs(v.get("duration")),
        "_danmaku": int(v.get("danmaku") or 0),
    }


def _secs(duration: str | None) -> int:
    """「22:39」→ 1359；「253:27」→ 15207（分钟:秒，不是时:分）。"""
    if not duration or ":" not in duration:
        return 0
    try:
        parts = [int(p) for p in duration.split(":")]
    except ValueError:
        return 0
    sec = 0
    for p in parts:
        sec = sec * 60 + p
    return sec


def _score(v: dict, spec: dict) -> float:
    """相关性已经过了硬门槛，这里只决定「哪条更值得看」。

    播放量用 log：否则大 UP 的合集永远排第一，而合集往往 4 小时，
    一次看不完，对学生没用。

    时长是最强的信号：
      3 分钟以内  → 应试技巧，不是讲解，重罚
      3~8 分钟    → 偏短，多半只讲一个点，小罚
      8~45 分钟   → 一节课的长度，正合适
      90 分钟以上 → 合集，罚

    标题命中的是知识点的核心词，不含课程名——课程名每条结果都有，
    拿它算分等于没算。
    """
    score = math.log10(v["play"] + 10)

    sec = v["_secs"]
    if sec == 0:
        score -= 0.8
    elif sec < 180:
        score -= 1.6
    elif sec < 480:
        score -= 1.0
    elif sec <= 2700:
        score += 0.6
    elif sec <= 5400:
        score -= 0.2
    else:
        score -= 1.2

    terms = spec["required"] + spec["any"]
    title_key = v["title"].lower().replace("的", "")
    hits = sum(1 for t in terms if t.lower().replace("的", "") in title_key)
    score += hits * 0.7

    # 「秒解」「速成」这类不是讲解，是应试口诀。留着但不让它排前面
    if any(w in v["title"] for w in _MARKETING):
        score -= 1.5

    score += min(math.log10(v["_danmaku"] + 1), 1.0) * 0.3
    return round(score, 3)


# ---------------- 缓存 ----------------

def _cache_ok(cache: dict, key: str) -> bool:
    e = cache.get(key)
    return bool(e) and (time.time() - e.get("ts", 0)) < CACHE_TTL


def _load_cache() -> dict:
    if not os.path.exists(CACHE_PATH):
        return {}
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        # 只留最近 300 条，避免文件无限长
        if len(cache) > 300:
            keep = sorted(cache.items(), key=lambda kv: kv[1].get("ts", 0), reverse=True)[:300]
            cache = dict(keep)
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception:
        pass
