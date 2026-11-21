"""
多份资料的合并（从「拼接」升级到「融合」）。

拼接只保证不冲突：id 连续、来源带文件名、单文件失败不中断。
但拼接解决不了一个真问题——同一个概念在不同资料里叫法不同：

    教材写「傅里叶变换」，PPT 写「FFT」，习题册写「傅立叶变换」

抽出来就是三个知识点，排期表里排三遍，用户会以为这个工具在凑数。

所以合并要做三件事，按重要性排序：
  1. 同义归并：不同叫法的同一个概念合成一个，chunks 求并，溯源一条不少
  2. 覆盖度：合并后能说清「这个概念在哪几份资料里出现过」
  3. 覆盖度入权重：三份资料都讲的东西，比模型打的 importance 更硬

第 3 条是合并真正的增值——不是把文件堆在一起，而是用多份资料的
一致性去交叉验证重要性。一个概念被三份资料同时覆盖，它一定是重点，
这不需要任何模型来判断。
"""

from __future__ import annotations

import re
from typing import Any

# 归一化时直接删掉的标点与空白。中英文括号必须删：
# 「快速傅里叶变换（FFT）」和「快速傅里叶变换」是同一个东西。
_PUNCT = re.compile(
    r"[\s\u3000（）()【】\[\]{}<>《》〈〉\"'“”‘’·・\-—_/\\、，,。.:：;；!！?？~～|]+"
)

# 中文教材最常见的写法是「中文全称（英文缩写）」。
# 括号里通常是 FFT / TCP / PCB 这类简称，正文里往往又会单独出现。
_BRACKET = re.compile(r"[（(【\[][^）)】\]]{1,40}[）)】\]]")

# 高频字形异写。只收确定无疑的，宁缺毋滥——
# 误合并会让用户少学一个知识点，比漏合并严重得多。
_EQUIV = {
    "傅立叶": "傅里叶",
    "稀而伯特": "希尔伯特",
}


def _norm(name: str) -> str:
    s = str(name or "").strip().lower()
    s = _BRACKET.sub("", s)      # 括号内容不参与主名比较
    s = _PUNCT.sub("", s)
    for a, b in _EQUIV.items():
        s = s.replace(a, b)
    # 「二叉树的遍历」「二叉树遍历」是同一个说法，助词不承载语义
    s = s.replace("的", "")
    return s


SYSTEM_PROMPT = """你是课程知识点的去重裁判。下面这些知识点名称，是从同一门课的多份资料里分别抽出来的，
所以同一个概念可能有多种写法。

请找出「指的是同一个概念、只是叫法不同」的组。

【必须合并的情况】
- 中文名与英文缩写：傅里叶变换 / FFT
- 中文名与英文全称：快速傅里叶变换 / Fast Fourier Transform
- 多字/少字、带不带助词：二叉树的遍历 / 二叉树遍历
- 错别字或异体字：傅立叶变换 / 傅里叶变换

【绝对不能合并的情况】
- 上下位关系：二叉树 与 二叉搜索树，是两个概念
- 相关但不同：进程 与 线程，是两个概念
- 只是同一章：排序算法 与 快速排序，是两个概念

【判断原则】
宁缺毋滥。拿不准就不合并——漏合并只是多排一次，误合并会让一个知识点从计划里消失。

【输出格式】只返回 JSON：
{
  "groups": [
    {"keep": "保留的那个写法（必须出现在输入列表里，选最完整通用的）",
     "drop": ["被合并掉的写法（也必须出现在输入列表里）"]}
  ]
}
没有可合并的就返回 {"groups": []}"""


async def merge_points(points: list[dict], chunks: list[dict], llm) -> tuple[list[dict], dict]:
    """同义归并 + 覆盖度统计。返回 (points, report)。

    points 的 chunks 是 chunk id 列表；chunks 里每项带 file 字段（资料名）。
    """
    chunk_file = {c["id"]: (c.get("file") or "资料") for c in chunks}
    file_count = len({f for f in chunk_file.values()})

    for p in points:
        p["files"] = sorted({chunk_file[c] for c in p.get("chunks") or [] if c in chunk_file})

    if len(points) < 2:
        return points, _report(points, file_count, [], "skip", "", [])

    # ---- 1. 规则归并：归一化后相同的，直接合 ----
    groups = _rule_groups(points)
    had_rule = bool(groups)

    # ---- 2. 模型归并：规则覆盖不到的别名（缩写、中英对照） ----
    llm_merged = False
    rejected: list[dict] = []
    note = ""
    if llm is not None and getattr(llm, "available", False):
        try:
            more, rejected = await _llm_groups(points, groups, llm)
            if more:
                groups.extend(more)
                llm_merged = True
        except Exception as e:
            note = f"模型归并失败，仅用规则：{str(e)[:80]}"
    else:
        note = "未配置模型：只做字面归并，缩写与中英对照可能漏合并"

    mode = ("llm+rule" if (had_rule and llm_merged)
            else "llm" if llm_merged
            else "rule" if had_rule
            else "none")

    if not groups:
        return points, _report(points, file_count, [], mode, note, rejected)

    merged = _apply(points, groups)
    return merged, _report(merged, file_count, groups, mode, note, rejected)


# ---------------- 别名可信度 ----------------


def _lcs(a: str, b: str) -> int:
    """最长公共子序列长度。名字都短（<20 字），朴素 DP 足够。"""
    m, n = len(a), len(b)
    prev = [0] * (n + 1)
    for i in range(1, m + 1):
        cur = [0] * (n + 1)
        for j in range(1, n + 1):
            cur[j] = prev[j - 1] + 1 if a[i - 1] == b[j - 1] else max(prev[j], cur[j - 1])
        prev = cur
    return prev[n]


_ASCII_ABBR = re.compile(r"^[a-z0-9+#\-]{2,5}$", re.I)


def _plausible_alias(a: str, b: str) -> bool:
    """两个名字是否可能是「同一个概念的不同叫法」。

    模型在这一步不可靠。实测它会给出这样的合并：

        二叉搜索树的中序遍历  →  二叉树遍历

    这是上下位关系，不是同义。合并之后学生的计划里就少了一个该学的东西，
    而且他永远发现不了——这比漏合并严重得多。

    所以模型给的结果必须再过一道字面检查。放行三种情况：
    1. 缩写：FFT / TCP / DFT，纯英文短串
    2. 简称：一个是另一个的子串，长度差不夸张（三次握手 ⊂ TCP三次握手）
    3. 异写：字面高度重合（傅立叶变换 / 傅里叶变换，带不带"的"）
    """
    x, y = _norm(a), _norm(b)
    if not x or not y:
        return False
    if x == y:
        return True

    short, long = (x, y) if len(x) <= len(y) else (y, x)

    # 1. 英文缩写。中英对照无从比较字符，只能整体放行
    if _ASCII_ABBR.match(short):
        return True

    # 2. 子串包含。但多出来的部分必须是外文或数字——
    #    「三次握手 ⊂ TCP三次握手」是简称，
    #    「傅里叶变换 ⊂ 快速傅里叶变换」却是两个概念（多出来的"快速"是修饰语）。
    #    光看长度比区分不了这两者，得看多出来的是什么字。
    if short in long and not re.search(r"[\u4e00-\u9fff]", long.replace(short, "", 1)):
        return True

    # 3. 字形高度重合。阈值 0.75 是量出来的：
    #    「二叉搜索树的中序遍历 / 二叉树遍历」≈ 0.50，必须拦下
    #    「傅里叶变换 / 快速傅里叶变换」= 0.71，也该拦下（DFT 与 FFT 是两个概念）
    #    「傅立叶变换 / 傅里叶变换」= 0.80，应该合并
    return _lcs(x, y) / len(long) >= 0.75


# ---------------- 规则归并 ----------------

def _rule_groups(points: list[dict]) -> list[dict]:
    """字面层面的归并，用并查集，避免两个组抢同一个名字时互相打脸。

    两条归并边：
    1. 归一化 key 相同（去标点、去括号、去助词）
    2. 某个名字正好等于另一个名字括号里的内容
       ——「快速傅里叶变换（FFT）」与单独出现的「FFT」是同一概念
    """
    names = [str(p["name"]) for p in points]
    n = len(names)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # 边 1：归一化后相同
    seen: dict[str, int] = {}
    for i, name in enumerate(names):
        key = _norm(name)
        if key in seen:
            union(seen[key], i)
        else:
            seen[key] = i

    # 边 2：名字是别人括号里的缩写
    in_bracket: dict[str, list[int]] = {}
    for i, name in enumerate(names):
        for inner in _BRACKET.findall(name):
            # 这里不能用 _norm：它会把「（FFT）」整体当括号删掉，得到空串
            key = _PUNCT.sub("", inner.lower())
            if key:
                in_bracket.setdefault(key, []).append(i)
    for i, name in enumerate(names):
        for j in in_bracket.get(_norm(name), []):
            if i != j:
                union(j, i)

    clusters: dict[int, list[str]] = {}
    for i, name in enumerate(names):
        clusters.setdefault(find(i), []).append(name)

    groups = []
    for members in clusters.values():
        if len(members) < 2:
            continue
        # 保留信息最全的写法：「快速傅里叶变换（FFT）」优于「快速傅里叶变换」
        keep = max(members, key=lambda x: (len(x), x))
        groups.append({"keep": keep, "drop": [m for m in members if m != keep]})
    return groups


# ---------------- 模型归并 ----------------

async def _llm_groups(points: list[dict], existing: list[dict], llm) -> tuple[list[dict], list[dict]]:
    """返回 (可执行的合并组, 被字面校验拦下的组)。"""
    already = {n for g in existing for n in [g["keep"], *g["drop"]]}
    names = [p["name"] for p in points if p["name"] not in already]
    if len(names) < 2:
        return [], []

    known = set(names)
    user = "知识点名称列表：\n" + "\n".join(f"- {n}" for n in names)
    obj = await llm.json_call(SYSTEM_PROMPT, user)

    out: list[dict] = []
    rejected: list[dict] = []
    for g in obj.get("groups") or []:
        if not isinstance(g, dict):
            continue
        keep = str(g.get("keep") or "").strip()
        drop = [str(d).strip() for d in (g.get("drop") or [])]
        # 模型可能编出列表里没有的名字，必须校验，否则会凭空合并出空组
        if keep not in known:
            continue
        real = [d for d in drop if d in known and d != keep]
        if not real:
            continue

        ok = [d for d in real if _plausible_alias(keep, d)]
        bad = [d for d in real if d not in ok]
        if ok:
            out.append({"keep": keep, "drop": ok})
        if bad:
            rejected.append({"keep": keep, "drop": bad})
    return out, rejected


# ---------------- 执行合并 ----------------

def _apply(points: list[dict], groups: list[dict]) -> list[dict]:
    """按组执行合并。被合并掉的点，其 chunks / deps / files 全部并入主点。

    合并规则里有两条容易搞错：
    - minutes 取 max 而不是求和：学一次就够，不是学三遍
    - chunks 求并而不是取主：溯源要能看到所有出处，这是多资料的价值
    """
    by_name = {p["name"]: p for p in points}
    alias: dict[str, str] = {}      # 被合并名 -> 主名

    for g in groups:
        keep = g["keep"]
        if keep not in by_name:
            continue
        target = by_name[keep]
        for d in g["drop"]:
            src = by_name.get(d)
            if src is None or d == keep:
                continue
            # 依赖闭环保护：A 依赖 B 又 B 依赖 A 的两个点，
            # 合并后就是自己依赖自己，拓扑排序会死。
            if keep in (src.get("deps") or []) and d in (target.get("deps") or []):
                continue
            target["chunks"] = sorted(set(target.get("chunks") or []) | set(src.get("chunks") or []))
            target["deps"] = list({x for x in (target.get("deps") or []) + (src.get("deps") or [])
                                   if x not in (keep, d)})
            target["files"] = sorted(set(target.get("files") or []) | set(src.get("files") or []))
            for f in ("importance", "difficulty", "exam_freq"):
                target[f] = max(int(target.get(f, 3)), int(src.get(f, 3)))
            target["minutes"] = max(int(target.get("minutes") or 45), int(src.get("minutes") or 45))
            # 主点的 summary 太短就用被合并点的补上
            if len(str(target.get("summary") or "")) < len(str(src.get("summary") or "")):
                target["summary"] = src.get("summary") or target.get("summary")
            alias[d] = keep
            by_name.pop(d, None)

    # 依赖名已变，把所有点里指向被合并名的依赖改写为主名
    if alias:
        for p in by_name.values():
            p["deps"] = sorted({alias.get(x, x) for x in p.get("deps") or [] if alias.get(x, x) != p["name"]})

    return list(by_name.values())


# ---------------- 报告 ----------------

def _report(points: list[dict], file_count: int, groups: list[dict],
            mode: str, note: str, rejected: list[dict] | None = None) -> dict[str, Any]:
    """覆盖度：一个知识点被几份资料讲到。

    只统计「不同资料」的数量，同一份资料里的多个 chunk 算一次——
    否则一份 PDF 讲得越啰嗦，它的知识点就越显得重要。
    """
    for p in points:
        p["coverage"] = len(p.get("files") or [])

    multi = sorted(
        [p for p in points if p.get("coverage", 0) >= 2],
        key=lambda p: (-p["coverage"], -p.get("importance", 3), p["name"]),
    )

    return {
        "mode": mode,
        "note": note or (f"已比对 {len(points)} 个知识点的叫法，未发现同一概念的不同写法"
                         if mode == "none" else ""),
        "scanned": len(points),
        "file_count": file_count,
        "merged_groups": [
            {"keep": g["keep"], "drop": g["drop"]}
            for g in groups
        ],
        "merged_count": sum(len(g["drop"]) for g in groups),
        "rejected": rejected or [],
        "multi_source_count": len(multi),
        "multi_source": [
            {"name": p["name"], "coverage": p["coverage"], "files": p.get("files") or []}
            for p in multi[:12]
        ],
        "enabled": file_count >= 2,
    }
