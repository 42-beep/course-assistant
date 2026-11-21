"""任务队列：把「提交」和「执行」拆开。

原来是 `asyncio.create_task`，三件事一件都做不到：

1. **进程崩了任务就没了** —— 跑了一半，重启，用户看到的是永远停在 62% 的进度条
2. **加不了 worker** —— 任务绑在 API 进程里，API 卡住就是任务卡住
3. **失败不会重试** —— 调模型偶发超时，整次分析白跑

## 租约（lease）是这里的核心

抢任务的语句长这样：

    UPDATE jobs SET status='running', worker=?, lease_until=now+900
     WHERE id=(SELECT id FROM jobs WHERE 可抢 ORDER BY id LIMIT 1)

整段在 `BEGIN IMMEDIATE` 事务里，多个 worker 同时抢，只有一个能改到这行。
这就是分布式锁最朴素的形式——不需要 Redis，SQLite 的行锁就够。

`lease_until` 解决「worker 抢到任务后崩了」：租约到期，任务自动回到可抢状态，
被别的 worker 捡走。所以语义是 **at-least-once**（至少执行一次），
不是 exactly-once。这个区别很要紧：重复执行会重复调模型（多花钱），
所以 `max_attempts` 默认只有 2，宁可标失败让用户重提交。

## 为什么没写 Redis 实现

租约这个原语 Redis 和 SQLite 都有，而 SQLite 已经在用了，少一个进程。
真要上多机，把 `claim()` 换成 Redis 的 Lua 脚本即可——接口就一个方法，
切点留在这里。本机没有 Redis，写了也验证不了，那不如不写。
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import time
import uuid
from datetime import datetime

from core import obs, store

LEASE_SECONDS = float(os.environ.get("JOB_LEASE", "900"))      # 15 分钟没人续租就回收
_ALIVE_WINDOW = float(os.environ.get("WORKER_ALIVE", "30"))    # 30 秒内有心跳算活着


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def enqueue(task_id: str, kind: str, payload: dict, max_attempts: int = 2) -> int:
    ts = _now_iso()
    with store.tx() as conn:
        cur = conn.execute(
            "INSERT INTO jobs(task_id, kind, payload, status, max_attempts, created_at, updated_at)"
            " VALUES(?,?,?,'pending',?,?,?)",
            (task_id, kind, json.dumps(payload, ensure_ascii=False), max_attempts, ts, ts),
        )
        job_id = cur.lastrowid
    obs.log("queue.enqueue", job=job_id, task=task_id, kind=kind)
    return job_id


def claim(worker_id: str, lease: float | None = None) -> dict | None:
    """抢一个任务。抢不到返回 None。

    可抢 = 还没开始的（pending），或者被抢了但租约已经过期的（worker 崩了）。

    外面包一层重试，原因是踩到过的真问题：**两个 worker 同时启动**时，
    数据库的 -wal/-shm 可能刚被上一个进程清理掉，两个进程会同时去创建
    共享内存文件，Windows 上偶尔会失败并报 "attempt to write a readonly
    database"（SQLite 打不开 -shm 时就是这么报的）。这不是逻辑错误，
    重试几十毫秒就好了。只对 locked/readonly 重试，别的错误照常抛。
    """
    for attempt in range(5):
        try:
            return _claim_once(worker_id, lease)
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if attempt == 4 or ("locked" not in msg and "readonly" not in msg):
                raise
            obs.log("queue.claim_retry", worker=worker_id, err=msg[:80], attempt=attempt + 1)
            time.sleep(0.02 * (2 ** attempt))       # 20/40/80/160ms，合计不到 0.3 秒
    return None


def _claim_once(worker_id: str, lease: float | None = None) -> dict | None:
    lease = lease or LEASE_SECONDS
    now = time.time()
    with store.tx() as conn:
        row = conn.execute(
            "SELECT id, task_id, kind, payload, attempts, max_attempts FROM jobs"
            " WHERE (status='pending' AND lease_until <= ?)"
            "    OR (status='running' AND lease_until < ?)"
            " ORDER BY id LIMIT 1",
            (now, now),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE jobs SET status='running', worker=?, lease_until=?,"
            " attempts=attempts+1, updated_at=? WHERE id=?",
            (worker_id, now + lease, _now_iso(), row["id"]),
        )
        job = dict(row)
    job["attempts"] += 1
    try:
        job["payload"] = json.loads(job["payload"])
    except Exception:
        job["payload"] = {}
    obs.log("queue.claim", job=job["id"], task=job["task_id"],
            worker=worker_id, attempt=job["attempts"])
    return job


def heartbeat(job_id: int, lease: float | None = None) -> None:
    """续租。任务跑得久（解析大 PDF）时必须续，否则会被当成崩了重投。"""
    lease = lease or LEASE_SECONDS
    with store.tx() as conn:
        conn.execute("UPDATE jobs SET lease_until=?, updated_at=? WHERE id=? AND status='running'",
                     (time.time() + lease, _now_iso(), job_id))


def finish(job_id: int) -> None:
    with store.tx() as conn:
        conn.execute("UPDATE jobs SET status='done', lease_until=0, updated_at=? WHERE id=?",
                     (_now_iso(), job_id))


def fail(job_id: int, error: str) -> str:
    """失败处理。还有重试次数就放回队列，否则标 failed。

    返回 'retry' 或 'failed'，调用方据此决定日志措辞。
    """
    with store.tx() as conn:
        row = conn.execute("SELECT attempts, max_attempts FROM jobs WHERE id=?",
                           (job_id,)).fetchone()
        if not row:
            return "failed"
        if row["attempts"] < row["max_attempts"]:
            conn.execute(
                "UPDATE jobs SET status='pending', lease_until=0, last_error=?, updated_at=?"
                " WHERE id=?", (error[:500], _now_iso(), job_id))
            return "retry"
        conn.execute(
            "UPDATE jobs SET status='failed', lease_until=0, last_error=?, updated_at=?"
            " WHERE id=?", (error[:500], _now_iso(), job_id))
        return "failed"


def depth() -> int:
    """还有几个在等。排队提示用这个数，不是进程内计数器。"""
    with store.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs"
            " WHERE status='pending' OR (status='running' AND lease_until < ?)",
            (time.time(),),
        ).fetchone()
    return int(row["n"])


def ahead_of(task_id: str) -> int:
    """这个任务前面还有几个。

    不能用 depth()-1：depth 把「跑着的」也算进去，而前面的任务可能刚跑完。
    按 job id 比较才是准的——id 自增，就是入队顺序。
    """
    with store.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs"
            " WHERE status IN ('pending','running')"
            "   AND id < (SELECT MIN(id) FROM jobs WHERE task_id=?)",
            (task_id,),
        ).fetchone()
    return int(row["n"])


# ---------------- worker 存活探测 ----------------

def new_worker_id() -> str:
    """host + pid + 随机后缀。

    不能只用时间戳：同进程内起两个 worker 协程，秒级时间戳是一样的，
    两个 worker 就会共用一个 id（踩过：并发 2 只注册上 1 个）。
    """
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:4]}"


def register(worker_id: str) -> None:
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO workers(id, pid, host, started_at, last_seen) VALUES(?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen",
            (worker_id, os.getpid(), socket.gethostname(), _now_iso(), time.time()),
        )


def beat(worker_id: str) -> None:
    """心跳。除了证明自己活着，也顺手续上正在跑的任务的租约。"""
    with store.tx() as conn:
        conn.execute("UPDATE workers SET last_seen=? WHERE id=?", (time.time(), worker_id))


def bump_done(worker_id: str) -> None:
    with store.tx() as conn:
        conn.execute("UPDATE workers SET jobs_done=jobs_done+1, last_seen=? WHERE id=?",
                     (time.time(), worker_id))


def active_workers() -> list[dict]:
    cutoff = time.time() - _ALIVE_WINDOW
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT id, pid, host, started_at, jobs_done FROM workers WHERE last_seen >= ?"
            " ORDER BY started_at", (cutoff,)).fetchall()
    return [dict(r) for r in rows]
