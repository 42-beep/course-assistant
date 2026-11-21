"""
课程助手 · 后端入口

启动：
    cd D:/career_plan/course-assistant
    python -m uvicorn app:app --reload --port 8000
然后打开 http://127.0.0.1:8000

流水线（见 core/）：
    解析 → 抽知识点 → 合并 → 真题比对 → 排期 → 说明 → 视频
只有「抽知识点 / 说明」调模型，「排期」是纯算法，「视频」调网络。
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import traceback
import uuid
from datetime import date, datetime

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from core import access
from core import extract as extract_mod
from core import exam as examlink
from core import generate, knowledge, schedule, video as video_mod
from core.llm import CONFIG_PATH, llm

BASE = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE, "data", "uploads")
SESSION_DIR = os.path.join(BASE, "data", "sessions")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(SESSION_DIR, exist_ok=True)

app = FastAPI(title="课程助手")

# 任务表：id -> {status, step, progress, result, error, owner}
TASKS: dict[str, dict] = {}

# 一次最多 40MB/份。局域网里有人传个 300MB 的扫描版教材，
# 解析要跑十几分钟，还把别人的任务全堵在后面。
MAX_FILE_MB = 40
# 同时跑几个分析。每个任务要调几十次模型，开太多会互相拖慢，
# 所以排队而不是并发堆上去。
MAX_JOBS = int(os.environ.get("MAX_JOBS", "2"))
_SLOTS = asyncio.Semaphore(MAX_JOBS)
_running = {"n": 0}


def _guard(t: dict | None, owner: str) -> dict:
    """别人的任务一律当作不存在。

    用 404 而不是 403：403 等于告诉对方「这个 id 是真的，你只是没权限」，
    而 task_id 本身就该是猜不到的。
    """
    if not t or (owner and t.get("owner", "") != owner):
        raise HTTPException(404, "任务不存在")
    return t


@app.get("/")
async def index(request: Request):
    if not access.valid(request):
        return FileResponse(os.path.join(BASE, "web", "login.html"), status_code=401)
    return FileResponse(os.path.join(BASE, "web", "index.html"))


@app.post("/login")
async def login(request: Request, k: str = Form("")):
    """口令只走 POST：放 URL 里会进浏览器历史、也可能被代理日志记下。"""
    got = (k or "").strip()
    hit = next((c for c in access.codes() if got and secrets.compare_digest(got, c)), "")
    if hit:
        resp = RedirectResponse("/", status_code=303)
        access.set_cookie(resp, hit)
        return resp
    return RedirectResponse("/?e=1", status_code=303)


@app.get("/api/config")
async def get_config(request: Request):
    return {
        "llm_available": llm.available,
        "model": llm.model if llm.available else "",
        # 非本机不给配置入口：接口本来就会 403，前端别再显示一个点了没用的链接
        "admin": access.is_local(request) or not access.enabled(),
        "note": "" if llm.available else "未配置模型，将使用规则模式（结果可用，但知识点粒度和说明质量会弱一些）",
    }


@app.post("/api/config")
async def set_config(payload: dict, _: str = Depends(access.require_admin)):
    """保存模型配置，免去手改 config.json。只有本机能调——见 access.require_admin。"""
    api_key = (payload.get("api_key") or "").strip()
    base_url = (payload.get("base_url") or "").strip()
    model = (payload.get("model") or "").strip()
    if not api_key:
        raise HTTPException(400, "api_key 不能为空")

    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    cfg.update({"api_key": api_key, "base_url": base_url, "model": model})
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    # 重新载入全局实例（就地更新，其他模块持有的引用也会同步）
    new = type(llm)()
    llm.__dict__.update(new.__dict__)
    return {"ok": True, "llm_available": llm.available, "model": llm.model}


@app.post("/api/config/test")
async def test_config(payload: dict | None = None, _: str = Depends(access.require_admin)):
    """真发一次极小请求，确认 key 和地址都能用。

    带上 payload 时用临时配置测试，不需要先保存——这样填完就能验证。
    同样只有本机能调：否则别人可以用它探测你的网络和额度。
    """
    target = llm
    if payload and (payload.get("api_key") or "").strip():
        target = type(llm)(payload)
    if not target.available:
        return {"ok": False, "error": "api_key 或 model 没填全"}
    try:
        reply = await target.chat("你是连通性测试。", "只回复两个字：可用", temperature=0, timeout=40)
        return {"ok": True, "model": target.model, "reply": (reply or "").strip()[:40]}
    except Exception as e:
        msg = f"{type(e).__name__}: {str(e)[:220]}"
        # 把最常见的两种错误翻译成人话
        if "401" in msg or "Unauthorized" in msg:
            msg = "401 认证失败：api_key 不对，或者复制时带了空格/引号"
        elif "404" in msg:
            msg = "404 地址不对：base_url 应该到 /v1 为止，不要带 /chat/completions"
        elif "Connect" in msg or "timeout" in msg.lower():
            msg = "连不上：检查网络，或 base_url 末尾多了斜杠"
        return {"ok": False, "error": msg}


@app.post("/api/analyze")
async def analyze(
    files: list[UploadFile] | None = File(None),
    exam_files: list[UploadFile] | None = File(None),
    file: UploadFile | None = File(None),
    course: str = Form(""),
    exam_date: str = Form(...),
    minutes_per_day: int = Form(120),
    owner: str = Depends(access.require),
):
    # 新旧两种字段名都收：老前端发 file，新前端发 files
    incoming = [f for f in (files or []) if f and f.filename]
    if file and file.filename:
        incoming.append(file)
    if not incoming:
        raise HTTPException(400, "没有收到文件")
    if len(incoming) > 10:
        raise HTTPException(400, "一次最多 10 份资料。分批上传，或者先合并成一个 PDF")

    exam_incoming = [f for f in (exam_files or []) if f and f.filename]
    if len(exam_incoming) > 5:
        raise HTTPException(400, "真题最多 5 份")

    too_big = [f.filename for f in incoming + exam_incoming
               if (getattr(f, "size", 0) or 0) > MAX_FILE_MB * 1024 * 1024]
    if too_big:
        raise HTTPException(413, f"单个文件不能超过 {MAX_FILE_MB}MB：{'、'.join(too_big[:3])}")

    try:
        exam = date.fromisoformat(exam_date)
    except Exception:
        raise HTTPException(400, "考试日期格式应为 YYYY-MM-DD")
    if exam <= date.today():
        raise HTTPException(400, "考试日期必须晚于今天")

    task_id = uuid.uuid4().hex[:12]
    TASKS[task_id] = {"status": "running", "step": "准备中", "progress": 0,
                      "result": None, "error": "", "owner": owner}

    # 存文件：每个任务一个子目录，保留原始文件名。
    # 不要用 taskid 给文件命名——解析层是拿 basename 当资料名的，
    # 加了前缀会让结果里显示「f79761308d62_1.txt」，用户认不出那是哪份资料。
    task_dir = os.path.join(UPLOAD_DIR, task_id)
    saved = _save_uploads(incoming, task_dir)
    # 真题放子目录，basename 仍是原名，来源标签不会带上 exam/ 前缀
    exam_saved = _save_uploads(exam_incoming, os.path.join(task_dir, "exam")) if exam_incoming else []

    default_course = course.strip() or os.path.splitext(incoming[0].filename)[0]
    asyncio.create_task(_run_queued(task_id, saved, exam_saved, default_course,
                                    exam, max(30, min(minutes_per_day, 600))))
    return {"task_id": task_id, "file_count": len(saved), "exam_count": len(exam_saved)}


@app.get("/api/task/{task_id}")
async def task_status(task_id: str, owner: str = Depends(access.require)):
    t = _guard(TASKS.get(task_id), owner)
    return {k: v for k, v in t.items() if k not in ("result", "owner")} | {
        "has_result": t["result"] is not None
    }


@app.get("/api/result/{task_id}")
async def task_result(task_id: str, owner: str = Depends(access.require)):
    t = _guard(TASKS.get(task_id), owner)
    if t["status"] != "done" or not t["result"]:
        raise HTTPException(409, f"任务尚未完成（{t['step']}）")
    return t["result"]


@app.post("/api/rerun")
async def rerun(payload: dict, owner: str = Depends(access.require)):
    """只改考试时间，不重跑解析和抽取——换个时间预算，方案立刻变。"""
    task_id = payload.get("task_id", "")
    exam_date = payload.get("exam_date", "")
    minutes_per_day = int(payload.get("minutes_per_day") or 120)

    t = _guard(TASKS.get(task_id), owner)
    if not t.get("result"):
        raise HTTPException(404, "原始任务不存在")

    try:
        exam = date.fromisoformat(exam_date)
    except Exception:
        raise HTTPException(400, "考试日期格式应为 YYYY-MM-DD")
    if exam <= date.today():
        raise HTTPException(400, "考试日期必须晚于今天")

    base = t["result"]
    points = base["knowledge"]["points"]

    plan = schedule.make_plan(points, exam, max(30, min(minutes_per_day, 600)))
    chunks = base["chunks"]
    chunk_map = {c["id"]: c for c in chunks}
    await generate.add_guidance(plan, chunk_map, llm)
    plan["resources"] = generate.build_resources(plan, chunks, chunk_map, base["meta"]["course"])
    # 重算换了日期，每日内容会变，视频要重新挂（有缓存，很快）
    video_info = await video_mod.attach(plan, base["meta"]["course"])

    result = {**base, "plan": plan, "videos": video_info,
              "meta": {**base["meta"], "exam_date": exam.isoformat(),
                       "minutes_per_day": minutes_per_day,
                       "generated_at": datetime.now().isoformat(timespec="seconds")}}
    t["result"] = result
    _save(task_id, result)
    return result


@app.get("/api/history")
async def history(owner: str = Depends(access.require)):
    """历史记录按人隔离。

    不隔离的话，同学 A 打开页面就能看到同学 B 传的课件和考试日期——
    这些是他没打算给任何人看的东西，比 API 额度重要得多。
    """
    items = []
    for fn in sorted(os.listdir(SESSION_DIR), reverse=True):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(SESSION_DIR, fn), "r", encoding="utf-8") as f:
                d = json.load(f)
            meta = d.get("meta", {})
            if owner and meta.get("owner", "") != owner:
                continue
            items.append({
                "id": fn[:-5],
                "course": meta.get("course", ""),
                "filename": meta.get("filename", ""),
                "exam_date": meta.get("exam_date", ""),
                "generated_at": meta.get("generated_at", ""),
                "points": d.get("knowledge", {}).get("stats", {}).get("count", 0),
            })
        except Exception:
            continue
        if len(items) >= 20:
            break
    return items


@app.get("/api/result-by-id/{sid}")
async def result_by_id(sid: str, owner: str = Depends(access.require)):
    # 先挡掉路径穿越：sid 直接拼进路径了
    if not sid.isalnum():
        raise HTTPException(404, "找不到该记录")
    path = os.path.join(SESSION_DIR, f"{sid}.json")
    if not os.path.exists(path):
        raise HTTPException(404, "找不到该记录")
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    if owner and d.get("meta", {}).get("owner", "") != owner:
        raise HTTPException(404, "找不到该记录")
    return d


# ---------------- 流水线 ----------------

async def _run_queued(task_id: str, *args) -> None:
    """排队 + 限流。

    每个任务要串几十次模型调用，五个人同时点「生成」不会更快，只会互相
    拖慢，而且容易撞上游的限流。宁可明确告诉他们「在排队」，也别让所有人
    一起卡住。
    """
    t = TASKS[task_id]
    waiting = _running["n"] - MAX_JOBS + 1
    if waiting > 0:
        t["step"] = f"排队中（前面还有 {waiting} 个任务在跑）"
    async with _SLOTS:
        _running["n"] += 1
        try:
            await _pipeline(task_id, *args)
        finally:
            _running["n"] -= 1


async def _pipeline(task_id: str, paths: list[str], exam_paths: list[str],
                    course: str, exam: date, minutes_per_day: int) -> None:
    t = TASKS[task_id]

    def step(name: str, p: int) -> None:
        t["step"], t["progress"] = name, p

    try:
        step("解析文档", 10)
        data = extract_mod.extract_many(paths)
        chunks = data["chunks"]
        if data["meta"]["skipped"]:
            t["note"] = "以下文件没能解析：" + "、".join(s["file"] for s in data["meta"]["skipped"])

        step("抽取知识点", 30)
        kn = await knowledge.build_knowledge(chunks, course, llm)

        # 真题在知识点之后处理：它不产出新知识点，只给已有的加权
        exam_info: dict = {"enabled": False, "mode": "none", "hits": 0, "covered": 0, "note": ""}
        exam_chunks: list[dict] = []
        if exam_paths:
            step("比对真题", 50)
            try:
                edata = extract_mod.extract_many(exam_paths, max_chunks=60)
                exam_chunks = [{**c, "id": f"ex_{i:02d}"} for i, c in enumerate(edata["chunks"], start=1)]
                exam_info = await examlink.link(kn["points"], exam_chunks, llm)
                exam_info["files"] = edata["meta"]["files"]
                exam_info["skipped"] = edata["meta"]["skipped"]
            except Exception as e:
                # 真题解析失败不该让整次分析失败——讲义的结果本身是完整的
                exam_info = {"enabled": False, "mode": "none", "hits": 0, "covered": 0,
                             "note": f"真题未能解析：{str(e)[:80]}"}

        step("排定计划", 62)
        plan = schedule.make_plan(kn["points"], exam, minutes_per_day)

        step("生成学习说明", 78)
        chunk_map = {c["id"]: c for c in chunks}
        await generate.add_guidance(plan, chunk_map, llm)

        step("整理资源建议", 88)
        plan["resources"] = generate.build_resources(plan, chunks, chunk_map, course)

        # 视频放最后：它依赖网络，失败或超时都不能影响前面已经算好的计划
        step("找配套视频", 94)
        video_info = await video_mod.attach(plan, course)

        result = {
            "meta": {
                "course": course,
                "filename": data["meta"]["filename"],
                "files": data["meta"]["files"],
                "skipped": data["meta"]["skipped"],
                "file_count": len(data["meta"]["files"]),
                "exam_date": exam.isoformat(),
                "minutes_per_day": minutes_per_day,
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "llm_mode": kn["mode"],
                "llm_note": kn["note"],
            },
            "chunks": chunks,
            "knowledge": kn,
            "exam": {**exam_info, "chunks": exam_chunks},
            "plan": plan,
            "videos": video_info,
        }

        t["result"] = result
        t["status"] = "done"
        step("完成", 100)
        _save(task_id, result)

    except Exception as e:
        t["status"] = "error"
        t["error"] = f"{e}"
        t["step"] = "出错"
        traceback.print_exc()


def _save_uploads(items: list[UploadFile], directory: str) -> list[str]:
    """按原始文件名落盘，同名加序号，返回绝对路径。"""
    os.makedirs(directory, exist_ok=True)
    saved: list[str] = []
    used: set[str] = set()
    for up in items:
        name = _safe_filename(up.filename)
        stem, ext = os.path.splitext(name)
        n = 2
        while name in used:
            name = f"{stem}_{n}{ext}"
            n += 1
        used.add(name)
        dst = os.path.join(directory, name)
        with open(dst, "wb") as f:
            shutil.copyfileobj(up.file, f)
        saved.append(dst)
    return saved


def _safe_filename(name: str) -> str:
    """去掉路径分隔符和控制字符，防止 ../ 之类的名字写到目录外面。"""
    name = os.path.basename(name or "").replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '<>:"|?*')
    return name.strip(". ") or "未命名"


def _save(task_id: str, result: dict) -> None:
    # 归属写进会话文件：否则重启后内存里的任务表没了，历史就没法再按人隔离
    owner = (TASKS.get(task_id) or {}).get("owner", "")
    if owner:
        result.setdefault("meta", {})["owner"] = owner
    with open(os.path.join(SESSION_DIR, f"{task_id}.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)
