"""独立的 worker 进程。

    python -m core.worker --concurrency 2

它只做三件事：抢任务 → 跑流水线 → 写回状态。不提供 HTTP，不处理上传，
所以可以按需要随便开几个：

    # 一台机器上开两个 worker，各跑 1 个任务
    python -m core.worker --concurrency 1 &
    python -m core.worker --concurrency 1 &

    # 或者只跑 API、worker 单独开进程（同一台机器）
    WORKER_INLINE=0 python -m uvicorn app:app --port 8000

**不能跨机器。** SQLite 的并发靠本地文件锁和 WAL 的 -shm 共享内存，
放在 NFS / SMB 这类网络文件系统上会静默损坏数据。要放到第二台机器上跑
worker，先换 PostgreSQL（只改 core/store.py）或加 Redis 做队列——
这条是边界，不是待办。

## 三个必须处理的细节

1. **心跳续租**：解析大 PDF 可能几分钟，租约 15 分钟不够就续。不续会被
   别的 worker 当成「崩了」重投，同一个任务跑两遍，模型调两遍。
2. **失败要区分重试和放弃**：偶发超时值得重试一次；参数错误重试一万次也没用。
   所以 attempts 上限 2，超了标 failed 让用户重提交。
3. **优雅退出**：收到 SIGTERM 时不立刻死，先把手上的任务跑完或者放回队列。
   `kill -9` 也死不了整个系统——租约到期后任务会被别人捡走，这就是租约的价值。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import time
import traceback

from core import obs, pipeline, queue, store

POLL = float(os.environ.get("WORKER_POLL", "1.0"))


async def _renew(job_id: int, every: float) -> None:
    """定时续租。任务跑多久就续多久。"""
    try:
        while True:
            await asyncio.sleep(every)
            queue.heartbeat(job_id)
    except asyncio.CancelledError:
        raise


async def process_one(worker_id: str) -> bool:
    """抢一个任务并跑完。返回 False 表示队列空。"""
    job = queue.claim(worker_id)
    if not job:
        return False

    task_id = job["task_id"]
    task = store.get_task(task_id) or {}
    owner = task.get("owner", "")

    # 租约的三分之一续一次。续太频繁是白写库，续太慢有被重投的风险
    renewer = asyncio.create_task(_renew(job["id"], max(queue.LEASE_SECONDS / 3, 5)))

    try:
        result = await pipeline.run(task_id, job["payload"])
        store.save_session(task_id, owner, result)
        store.update_task(task_id, status="done", step="完成", progress=100)
        queue.finish(job["id"])
        queue.bump_done(worker_id)
        obs.log("worker.done", task=task_id, job=job["id"], worker=worker_id,
                attempt=job["attempts"])

    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        traceback.print_exc()
        verdict = queue.fail(job["id"], err)
        if verdict == "retry":
            # 放回队列。状态写 "重试中" 而不是直接报错：用户看到的应该是
            # 「还在跑」而不是「失败了」，因为下一次可能就成了
            store.update_task(task_id, status="queued", step=f"重试中（{job['attempts']}/2）",
                              error=err[:300])
        else:
            store.update_task(task_id, status="error", step="出错", error=err[:300])
        obs.log("worker.fail", task=task_id, job=job["id"], worker=worker_id,
                verdict=verdict, err=err[:200], attempt=job["attempts"])

    finally:
        renewer.cancel()
    return True


async def loop(worker_id: str, stop: asyncio.Event) -> None:
    queue.register(worker_id)
    obs.log("worker.start", worker=worker_id, pid=os.getpid())
    beat_at = 0.0
    try:
        while not stop.is_set():
            did = await process_one(worker_id)
            if time.time() - beat_at >= 5:
                queue.beat(worker_id)
                beat_at = time.time()
            if not did:
                # 空转时也保持心跳，否则 /api/metrics 会显示「没有 worker」
                await asyncio.sleep(POLL)
    finally:
        obs.log("worker.stop", worker=worker_id)


async def run_inline(concurrency: int, stop: asyncio.Event) -> list[asyncio.Task]:
    """给 app.py 用的内嵌模式：和 API 同进程跑几个 worker 协程。

    单机部署时不需要额外开进程，行为和改造前一模一样。
    """
    tasks = [asyncio.create_task(loop(queue.new_worker_id(), stop))
             for _ in range(max(1, concurrency))]
    return tasks


def main() -> None:
    ap = argparse.ArgumentParser(description="课程助手 worker")
    ap.add_argument("--concurrency", type=int, default=1, help="同时跑几个任务")
    args = ap.parse_args()

    store.init()

    async def _main() -> None:
        stop = asyncio.Event()

        def _bye(*_):
            obs.log("worker.signal", concurrency=args.concurrency)
            stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                asyncio.get_running_loop().add_signal_handler(sig, _bye)
            except NotImplementedError:
                # Windows 上 loop.add_signal_handler 不支持，退回 signal 模块
                signal.signal(sig, _bye)

        await asyncio.gather(*[loop(queue.new_worker_id(), stop)
                               for _ in range(max(1, args.concurrency))])

    asyncio.run(_main())


if __name__ == "__main__":
    main()
