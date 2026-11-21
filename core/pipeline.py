"""分析流水线：解析 → 抽知识点 → 合并 → 真题 → 排期 → 说明 → 视频。

从 app.py 里搬出来的。原因不是「代码整洁」，是因为它现在要被
**独立的 worker 进程**调用——留在 app.py 里，worker 就得 import FastAPI 应用，
然后 app 又要 import worker 来跑 inline 模式，绕成环。

约束没变：
- 只有「抽知识点 / 生成说明 / 真题判定」调模型
- 「排期」是纯算法，不调模型
- 视频调网络，失败或超时不影响已经算好的计划
"""

from __future__ import annotations

from datetime import date, datetime

from core import exam as examlink
from core import extract as extract_mod
from core import generate, knowledge, obs, schedule, store
from core import video as video_mod
from core.llm import llm


async def run(task_id: str, payload: dict) -> dict:
    """跑完一次分析，返回完整结果。进度直接写进 store，调用方不用管。

    异常不吞：一路抛给 worker，由它决定重试还是标失败。
    """
    paths: list[str] = payload["paths"]
    exam_paths: list[str] = payload.get("exam_paths") or []
    course: str = payload["course"]
    exam = date.fromisoformat(payload["exam"])
    minutes_per_day: int = int(payload["minutes_per_day"])

    def step(name: str, p: int) -> None:
        store.update_task(task_id, step=name, progress=p, status="running")
        obs.log("pipeline.step", task=task_id, step=name, progress=p)

    with obs.timer("pipeline.parse", task=task_id, files=len(paths)):
        step("解析文档", 10)
        data = extract_mod.extract_many(paths)
    chunks = data["chunks"]
    if data["meta"]["skipped"]:
        store.update_task(task_id, note="以下文件没能解析：" +
                          "、".join(s["file"] for s in data["meta"]["skipped"]))

    async with obs.span("pipeline.knowledge", task=task_id, chunks=len(chunks)):
        step("抽取知识点", 30)
        kn = await knowledge.build_knowledge(chunks, course, llm)

    # 真题在知识点之后处理：它不产出新知识点，只给已有的加权
    exam_info: dict = {"enabled": False, "mode": "none", "hits": 0, "covered": 0, "note": ""}
    exam_chunks: list[dict] = []
    if exam_paths:
        step("比对真题", 50)
        try:
            async with obs.span("pipeline.exam", task=task_id, files=len(exam_paths)):
                edata = extract_mod.extract_many(exam_paths, max_chunks=60)
                exam_chunks = [{**c, "id": f"ex_{i:02d}"}
                               for i, c in enumerate(edata["chunks"], start=1)]
                exam_info = await examlink.link(kn["points"], exam_chunks, llm)
                exam_info["files"] = edata["meta"]["files"]
                exam_info["skipped"] = edata["meta"]["skipped"]
        except Exception as e:
            # 真题解析失败不该让整次分析失败——讲义本身的结果是完整的
            exam_info = {"enabled": False, "mode": "none", "hits": 0, "covered": 0,
                         "note": f"真题未能解析：{str(e)[:80]}"}
            obs.log("pipeline.exam_failed", task=task_id, err=str(e)[:200])

    with obs.timer("pipeline.schedule", task=task_id, points=len(kn["points"])):
        step("排定计划", 62)
        plan = schedule.make_plan(kn["points"], exam, minutes_per_day)

    async with obs.span("pipeline.guidance", task=task_id):
        step("生成学习说明", 78)
        chunk_map = {c["id"]: c for c in chunks}
        await generate.add_guidance(plan, chunk_map, llm)

    step("整理资源建议", 88)
    plan["resources"] = generate.build_resources(plan, chunks, chunk_map, course)

    # 视频放最后：依赖网络，失败或超时都不能影响前面已经算好的计划
    async with obs.span("pipeline.video", task=task_id):
        step("找配套视频", 94)
        video_info = await video_mod.attach(plan, course)

    return {
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
