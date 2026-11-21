"""
课程助手 · 后端入口

启动（单机，和改造前一模一样）：
    python -m uvicorn app:app --port 8000

启动（多实例 / 独立 worker）：
    # 终端 1：只跑 API，不跑任务
    WORKER_INLINE=0 python -m uvicorn app:app --port 8000
    # 终端 2、3：任务进程，可以开任意多个
    python -m core.worker --concurrency 1

流水线（见 core/pipeline.py）：
    解析 → 抽知识点 → 合并 → 真题比对 → 排期 → 说明 → 视频
只有「抽知识点 / 说明 / 真题判定」调模型，「排期」是纯算法。

状态不放在这个进程里：任务表、会话结果、队列全部在 data/app.db（SQLite WAL）。
所以这个进程随时可以杀，重启后任务还在，worker 会继续跑。
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse

from core import access, obs, queue, store, worker
from core import generate, schedule
from core import video as video_mod
from core.llm import CONFIG_PATH, llm

BASE = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE, "data", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

MAX_FILE_MB = 40
# 内嵌 worker 的并发数。单机默认 2，多实例部署时设 WORKER_INLINE=0 关掉。
MAX_JOBS = int(os.environ.get("MAX_JOBS", "2"))
INLINE = os.environ.get("WORKER_INLINE", "1") != "0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.init()
    stop = __import__("asyncio").Event()
    tasks = []
    if INLINE:
        # 单机部署时行为不变：一条命令起来，任务自己就跑
        tasks = await worker.run_inline(MAX_JOBS, stop)
        obs.log("app.inline_workers", n=MAX_JOBS)
    else:
        obs.log("app.no_inline_worker")
    try:
        yield
    finally:
        stop.set()
        for t in tasks:
            t.cancel()


app = FastAPI(title="课程助手", lifespan=lifespan)


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
        "admin": access.is_local(request) or not access.enabled(),
        "note": "" if llm.available else "未配置模型，将使用规则模式（结果可用，但知识点粒度和说明质量会弱一些）",
    }


@app.post("/api/config")
async def set_config(payload: dict, _: str = Depends(access.require_admin)):
    """保存模型配置。只有本机能调——见 access.require_admin。"""
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

    new = type(llm)()
    llm.__dict__.update(new.__dict__)
    obs.log("config.updated", model=llm.model, available=llm.available)
    return {"ok": True, "llm_available": llm.available, "model": llm.model}


@app.post("/api/config/test")
async def test_config(payload: dict | None = None, _: str = Depends(access.require_admin)):
    """真发一次极小请求，确认 key 和地址都能用。"""
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

    # 存文件：每个任务一个子目录，保留原始文件名。
    # 不要用 taskid 给文件命名——解析层是拿 basename 当资料名的，
    # 加了前缀会让结果里显示「f79761308d62_1.txt」，用户认不出那是哪份资料。
    task_dir = os.path.join(UPLOAD_DIR, task_id)
    saved = _save_uploads(incoming, task_dir)
    exam_saved = _save_uploads(exam_incoming, os.path.join(task_dir, "exam")) if exam_incoming else []

    default_course = course.strip() or os.path.splitext(incoming[0].filename)[0]

    # 先把任务写进库，再投递。顺序反过来的话，worker 可能抢到任务
    # 却查不到对应的 task 行——这是「投递先于状态」的经典竞态。
    store.create_task(task_id, owner=owner)
    queue.enqueue(task_id, "analyze", {
        "paths": saved,
        "exam_paths": exam_saved,
        "course": default_course,
        "exam": exam.isoformat(),
        "minutes_per_day": max(30, min(minutes_per_day, 600)),
    })
    obs.log("api.analyze", task=task_id, files=len(saved), exams=len(exam_saved),
            owner=owner or "local", depth=queue.depth())
    return {"task_id": task_id, "file_count": len(saved), "exam_count": len(exam_saved)}


@app.get("/api/task/{task_id}")
async def task_status(task_id: str, owner: str = Depends(access.require)):
    t = _guard(store.get_task(task_id), owner)
    out = {k: v for k, v in t.items() if k != "owner"}
    # 排队位次从库里按入队顺序算，不是进程内计数器——多实例下计数器是错的
    if t["status"] == "queued":
        ahead = queue.ahead_of(task_id)
        if ahead > 0:
            out["step"] = f"排队中（前面还有 {ahead} 个任务）"
        else:
            out["step"] = "排队中"
    out["has_result"] = store.get_session(task_id) is not None
    return out


@app.get("/api/result/{task_id}")
async def task_result(task_id: str, owner: str = Depends(access.require)):
    t = _guard(store.get_task(task_id), owner)
    data = store.get_session(task_id)
    if t["status"] != "done" or not data:
        raise HTTPException(409, f"任务尚未完成（{t['step']}）")
    return data


@app.post("/api/rerun")
async def rerun(payload: dict, owner: str = Depends(access.require)):
    """只改考试时间，不重跑解析和抽取——换个时间预算，方案立刻变。

    这条保留同步执行：它是单个用户的一次主动操作，十几秒内返回比
    「排队 + 轮询」体验好。真要做成异步，改动的是前端，不是这里。
    """
    task_id = payload.get("task_id", "")
    exam_date = payload.get("exam_date", "")
    minutes_per_day = int(payload.get("minutes_per_day") or 120)

    base = store.get_session(task_id)
    sid_owner = store.get_session_owner(task_id)
    if not base or (owner and sid_owner != owner):
        raise HTTPException(404, "原始任务不存在")

    try:
        exam = date.fromisoformat(exam_date)
    except Exception:
        raise HTTPException(400, "考试日期格式应为 YYYY-MM-DD")
    if exam <= date.today():
        raise HTTPException(400, "考试日期必须晚于今天")

    points = base["knowledge"]["points"]
    async with obs.span("api.rerun", task=task_id, minutes=minutes_per_day):
        plan = schedule.make_plan(points, exam, max(30, min(minutes_per_day, 600)))
        chunks = base["chunks"]
        chunk_map = {c["id"]: c for c in chunks}
        await generate.add_guidance(plan, chunk_map, llm)
        plan["resources"] = generate.build_resources(plan, chunks, chunk_map, base["meta"]["course"])
        video_info = await video_mod.attach(plan, base["meta"]["course"])

    result = {**base, "plan": plan, "videos": video_info,
              "meta": {**base["meta"], "exam_date": exam.isoformat(),
                       "minutes_per_day": minutes_per_day,
                       "generated_at": datetime.now().isoformat(timespec="seconds")}}
    store.save_session(task_id, sid_owner or "", result)
    return result


@app.get("/api/history")
async def history(owner: str = Depends(access.require)):
    """历史记录按人隔离。

    不隔离的话，同学 A 打开页面就能看到同学 B 传的课件和考试日期——
    这些是他没打算给任何人看的东西，比 API 额度重要得多。
    """
    return store.list_sessions(owner=owner, limit=20)


@app.get("/api/result-by-id/{sid}")
async def result_by_id(sid: str, owner: str = Depends(access.require)):
    # 先挡掉路径穿越：以前 sid 是直接拼进文件路径的
    if not sid.isalnum():
        raise HTTPException(404, "找不到该记录")
    d = store.get_session(sid)
    if not d:
        raise HTTPException(404, "找不到该记录")
    if owner and store.get_session_owner(sid) != owner:
        raise HTTPException(404, "找不到该记录")
    return d


@app.get("/api/metrics")
async def metrics(_: str = Depends(access.require_admin)):
    """运维看这里：任务/队列/worker 状态 + 最近的结构化日志。

    指标从状态表算，不另存一份，所以永远不会和真实状态对不上。
    """
    import asyncio
    return {
        "stats": await asyncio.to_thread(store.stats),
        "queue_depth": queue.depth(),
        "workers": queue.active_workers(),
        "legal_queue": queue.depth() == 0 or len(queue.active_workers()) > 0,
        "recent_logs": obs.tail(40),
        "inline_worker": INLINE,
    }


# ---------------- 文件落盘 ----------------

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
