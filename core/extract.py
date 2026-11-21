"""
文档解析：把 PDF / PPTX / TXT 变成带结构的文本块。

为什么要保留 heading：
知识点抽取的准确率，八成取决于"这段文字属于哪个章节"这个信息。
光有正文，模型只能瞎猜；带上标题路径，它就能判断层级关系。
"""

from __future__ import annotations

import os
import re
from typing import Any

SUPPORTED = {".pdf", ".pptx", ".ppt", ".txt", ".md"}


def extract(path: str) -> dict[str, Any]:
    """返回 {chunks, meta}。chunks 是 [{id, text, source, heading}]。"""
    ext = os.path.splitext(path)[1].lower()

    if ext not in SUPPORTED:
        raise ValueError(
            f"暂不支持 {ext} 格式。目前支持 PDF / PPTX / TXT / Markdown。"
            f"如果是图片或扫描件，需要先做 OCR。"
        )

    if ext == ".pdf":
        raw = _pdf_to_markdown(path)
        chunks, title = _split_markdown(raw, unit="页")
    elif ext in (".pptx", ".ppt"):
        chunks, title = _pptx_to_chunks(path)
    else:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            raw = f.read()
        chunks, title = _split_markdown(raw, unit="节")

    if not chunks:
        raise ValueError("没有提取到任何文字内容。如果是扫描版 PDF，需要先做 OCR。")

    return {
        "chunks": chunks,
        "meta": {
            "filename": os.path.basename(path),
            "title": title or os.path.splitext(os.path.basename(path))[0],
            "chunk_count": len(chunks),
            "char_count": sum(len(c["text"]) for c in chunks),
        },
    }


def extract_many(paths: list[str], max_chunks: int = 400) -> dict[str, Any]:
    """多份资料合并成一个 chunk 序列。

    三件事必须做对，否则后面的排期会乱：
    1. chunk id 全局连续，单文件 id 冲突会让知识点引用串台
    2. 每个 chunk 记住自己来自哪个文件，source 里也带上，
       否则「第 3 页」在五个文件里指五处，溯源就失效了
    3. 单个文件解析失败不能拖垮整批，记进 skipped 继续走
    """
    if not paths:
        raise ValueError("没有收到文件")

    all_chunks: list[dict] = []
    files_meta: list[dict] = []
    skipped: list[dict] = []

    for path in paths:
        name = os.path.basename(path)
        try:
            data = extract(path)
        except Exception as e:
            skipped.append({"file": name, "reason": str(e)[:120]})
            continue

        stem = os.path.splitext(name)[0]
        for c in data["chunks"]:
            if len(all_chunks) >= max_chunks:
                break
            all_chunks.append({
                **c,
                "id": len(all_chunks) + 1,
                "file": stem,
                # 多份资料时「第 3 页」有歧义，前面挂上文件名
                "source": c["source"] if len(paths) == 1 else f"{stem} · {c['source']}",
            })

        files_meta.append({
            "name": name,
            "title": data["meta"]["title"],
            "chunks": len(data["chunks"]),
            "chars": data["meta"]["char_count"],
        })

    if not all_chunks:
        reason = skipped[0]["reason"] if skipped else "没有提取到任何文字内容"
        raise ValueError(f"所有文件都没解析成功：{reason}。如果是扫描版 PDF，需要先做 OCR。")

    titles = [f["title"] for f in files_meta]
    return {
        "chunks": all_chunks,
        "meta": {
            "filename": "、".join(f["name"] for f in files_meta),
            "title": titles[0] if len(titles) == 1 else f"{titles[0]} 等 {len(titles)} 份资料",
            "files": files_meta,
            "skipped": skipped,
            "chunk_count": len(all_chunks),
            "char_count": sum(len(c["text"]) for c in all_chunks),
        },
    }


# ---------------- PDF ----------------

_CN_NUM = "一二三四五六七八九十百零两"


def _pdf_to_markdown(path: str) -> str:
    """按页提取，并自己识别中文标题。

    不用 pymupdf4llm 的自动标题识别：它会把「1.1 顺序存储结构」这类
    中文小标题转成列表项，导致整章挤成一个块，知识点粒度粗到没法排期。
    自己按「第X章 / 1.1 / 1、」这类编号判断层级，可控得多。
    """
    import pymupdf

    doc = pymupdf.open(path)
    parts: list[str] = []
    for i, page in enumerate(doc, start=1):
        parts.append(f"\n\n## 第 {i} 页\n")
        for raw in page.get_text().splitlines():
            line = raw.strip()
            if not line:
                continue
            lvl = _guess_heading_level(line)
            parts.append(("#" * lvl + " " + line) if lvl else line)
    doc.close()
    return "\n".join(parts)


def _guess_heading_level(line: str) -> int:
    if len(line) > 42:
        return 0
    if re.match(rf"^第[{_CN_NUM}\d]+[章篇]", line):
        return 3
    if re.match(rf"^第[{_CN_NUM}\d]+节", line):
        return 4
    if re.match(r"^\d+\.\d+(\.\d+)?[\s、.]+\S", line):
        return 4
    if re.match(r"^\d+[\.、]\s*\S", line) and not re.match(r"^\d+\.\d", line):
        return 4
    return 0


# ---------------- PPTX ----------------

def _pptx_to_chunks(path: str) -> tuple[list[dict], str]:
    from pptx import Presentation

    prs = Presentation(path)
    chunks: list[dict] = []
    title = ""

    for idx, slide in enumerate(prs.slides, start=1):
        texts: list[str] = []
        slide_title = ""

        for shape in slide.shapes:
            if not hasattr(shape, "text_frame"):
                continue
            t = shape.text_frame.text.strip()
            if not t:
                continue
            # 占位符里的标题当作这一页的 heading
            if not slide_title and shape.is_placeholder:
                try:
                    if shape.placeholder_format.type in (1, 13):  # TITLE / CENTER_TITLE
                        slide_title = t.split("\n")[0].strip()
                        continue
                except Exception:
                    pass
            texts.append(t)

        # 演讲者备注往往比幻灯片正文信息量大，一并收进来
        try:
            if slide.has_notes_slide:
                note = slide.notes_slide.notes_text_frame.text.strip()
                if note:
                    texts.append("【备注】" + note)
        except Exception:
            pass

        body = "\n".join(texts).strip()
        if body:
            chunks.append({
                "id": len(chunks) + 1,
                "text": body,
                "source": f"第 {idx} 页",
                "heading": slide_title or (title or f"第 {idx} 页"),
            })
            if not title and slide_title:
                title = slide_title

    return chunks, title


# ---------------- Markdown / 纯文本 ----------------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def _split_markdown(raw: str, unit: str = "节") -> tuple[list[dict], str]:
    """按标题切块；没有标题就按空行聚合成固定大小的块。"""
    lines = raw.splitlines()
    chunks: list[dict] = []
    doc_title = ""
    heading_stack: list[tuple[int, str]] = []
    buf: list[str] = []
    page_no = 1

    def flush() -> None:
        nonlocal buf
        text = "\n".join(buf).strip()
        buf = []
        if len(text) < 30:          # 太短的碎片（页眉、编号）丢掉
            return
        path = " / ".join(h[1] for h in heading_stack) if heading_stack else ""
        # PDF 有真实页码；纯文本没有分页，用段序号更诚实
        src = f"第 {page_no} 页" if unit == "页" else f"第 {len(chunks) + 1} 段"
        chunks.append({
            "id": len(chunks) + 1,
            "text": text,
            "source": src,
            "heading": path or doc_title or src,
        })

    for line in lines:
        # 页码标记必须先判断：否则会被当成普通标题塞进 heading 路径，
        # 每个片段的 heading 都会带上「第 N 页」，抽取质量会变差。
        pm = re.match(r"^#+\s*第\s*(\d+)\s*页\s*$", line)
        if pm:
            flush()
            page_no = int(pm.group(1))
            continue

        m = _HEADING_RE.match(line)
        if m:
            flush()
            level, text = len(m.group(1)), m.group(2).strip()
            if level == 1 and not doc_title:
                doc_title = text
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, text))
            buf.append(text)
            continue

        buf.append(line)

    flush()
    return chunks, doc_title
