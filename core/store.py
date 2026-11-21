"""统一存储层：任务状态、会话结果、队列都落在这里。

## 为什么必须有这一层

改造之前，状态散在三个地方，而且三处都隐含「只有一个进程」的假设：

| 原来 | 问题 |
|---|---|
| `TASKS: dict`（app.py 内存） | 第二个进程看不见；服务一重启，跑了一半的任务凭空消失 |
| `data/sessions/*.json` | 两个请求同时写同一目录，`os.listdir` 可能读到写了一半的文件 |
| `_SLOTS` 信号量 | 进程内的锁，多实例各算各的，限流等于没做 |

收进一个 SQLite（WAL 模式）之后：进程无状态、任何实例读到同一份状态、
并发写由数据库保证一致。

## 为什么是 SQLite 不是 Postgres

一台 2C2G 上多跑一个 PG 进程不值得。WAL 模式支持多进程并发读写，
几十个任务完全够用。真到多机部署那天要换 PG，**只改这个文件**——
因为所有 SQL 都收在这里，业务代码只调 `store.xxx()`。
这不是空话：`connect()` 和每条 SQL 都在下面，一眼能看完。
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from core import obs

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE, "data")
SESSION_DIR = os.path.join(DATA_DIR, "sessions")      # 老版本留下的，只读一次
DB_PATH = os.environ.get("CA_DB", os.path.join(DATA_DIR, "app.db"))

_TASK_FIELDS = {"status", "step", "progress", "note", "error"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    owner       TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL,                 -- queued / running / done / error
    step        TEXT NOT NULL DEFAULT '',
    progress    INTEGER NOT NULL DEFAULT 0,
    note        TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,
    owner        TEXT NOT NULL DEFAULT '',
    course       TEXT NOT NULL DEFAULT '',
    exam_date    TEXT NOT NULL DEFAULT '',
    generated_at TEXT NOT NULL DEFAULT '',
    points       INTEGER NOT NULL DEFAULT 0,
    data         TEXT NOT NULL                 -- 完整结果 JSON
);

CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'analyze',
    payload      TEXT NOT NULL DEFAULT '{}',
    status       TEXT NOT NULL DEFAULT 'pending',   -- pending/running/done/failed
    attempts     INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 2,
    worker       TEXT NOT NULL DEFAULT '',
    lease_until  REAL NOT NULL DEFAULT 0,           -- 租约到期的 epoch 秒
    last_error   TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workers (
    id         TEXT PRIMARY KEY,
    pid        INTEGER NOT NULL DEFAULT 0,
    host       TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    last_seen  REAL NOT NULL DEFAULT 0,
    jobs_done  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_tasks_owner   ON tasks(owner, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_owner ON sessions(owner, generated_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_claim    ON jobs(status, lease_until, id);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def connect():
    """一次操作一个连接。不要跨线程/跨 await 复用——SQLite 连接不是线程安全的。"""
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        # journal_mode 是**持久化**在数据库文件里的，只在 init() 里设一次。
        # 每条连接都设一遍是写操作，会在并发时多抢一次写锁——没必要。
        conn.execute("PRAGMA synchronous=NORMAL")    # WAL 下够安全，比 FULL 快很多
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
    finally:
        conn.close()


@contextmanager
def tx():
    """写事务。用 IMMEDIATE 立刻拿写锁，避免升级锁时死锁。"""
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")


def init() -> None:
    with connect() as conn:
        # WAL 支持多进程并发读写，是这套「状态放库里」的地基。
        # 它是持久化设置，一次生效，后续连接不用再设。
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
    _import_legacy_sessions()
    obs.log("store.ready", db=DB_PATH)


# ---------------- 任务状态 ----------------

def create_task(task_id: str, owner: str = "", note: str = "") -> None:
    ts = _now()
    with tx() as conn:
        conn.execute(
            "INSERT INTO tasks(id, owner, status, step, progress, note, created_at, updated_at)"
            " VALUES(?,?,'queued','排队中',0,?,?,?)",
            (task_id, owner, note, ts, ts),
        )


def update_task(task_id: str, **fields) -> None:
    cols = {k: v for k, v in fields.items() if k in _TASK_FIELDS}
    if not cols:
        return
    sets = ", ".join(f"{k}=?" for k in cols)
    with tx() as conn:
        conn.execute(f"UPDATE tasks SET {sets}, updated_at=? WHERE id=?",
                     (*cols.values(), _now(), task_id))


def get_task(task_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return dict(row) if row else None


# ---------------- 会话结果 ----------------

def save_session(session_id: str, owner: str, data: dict) -> None:
    meta = data.get("meta") or {}
    with tx() as conn:
        conn.execute(
            "INSERT INTO sessions(id, owner, course, exam_date, generated_at, points, data)"
            " VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET owner=excluded.owner, course=excluded.course,"
            " exam_date=excluded.exam_date, generated_at=excluded.generated_at,"
            " points=excluded.points, data=excluded.data",
            (session_id, owner, meta.get("course", ""), meta.get("exam_date", ""),
             meta.get("generated_at", ""),
             int((data.get("knowledge") or {}).get("stats", {}).get("count", 0)),
             json.dumps(data, ensure_ascii=False)),
        )


def get_session(session_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT data FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["data"])
    except Exception:
        return None


def get_session_owner(session_id: str) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT owner FROM sessions WHERE id=?", (session_id,)).fetchone()
    return row["owner"] if row else None


def list_sessions(owner: str = "", limit: int = 20) -> list[dict]:
    sql = ("SELECT id, course, exam_date, generated_at, points FROM sessions"
           + (" WHERE owner=?" if owner else "")
           + " ORDER BY generated_at DESC LIMIT ?")
    args = (owner, limit) if owner else (limit,)
    with connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [{"id": r["id"], "course": r["course"], "exam_date": r["exam_date"],
             "generated_at": r["generated_at"], "points": r["points"]} for r in rows]


# ---------------- 指标（从状态表派生，不另存一份） ----------------

def stats() -> dict:
    with connect() as conn:
        by_status = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")}
        jobs = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")}
        dur = conn.execute(
            "SELECT ROUND(AVG((julianday(updated_at)-julianday(created_at))*86400),1) AS avg_s"
            " FROM tasks WHERE status='done'").fetchone()
        recent = [dict(r) for r in conn.execute(
            "SELECT id, status, step, error, updated_at FROM tasks"
            " WHERE status='error' ORDER BY updated_at DESC LIMIT 5")]
    return {
        "tasks": by_status,
        "jobs": jobs,
        "avg_task_seconds": (dur["avg_s"] if dur else None),
        "task_total": sum(by_status.values()),
        "recent_errors": recent,
    }


# ---------------- 老数据迁移 ----------------

def _import_legacy_sessions() -> None:
    """把 data/sessions/*.json 导进来，只导一次。

    文件不删——那是用户传过的资料算出来的结果，删了没法恢复。
    用 meta 表记一个标记，避免每次启动重复导入。
    """
    with connect() as conn:
        done = conn.execute("SELECT value FROM meta WHERE key='legacy_imported'").fetchone()
    if done or not os.path.isdir(SESSION_DIR):
        return

    files = [f for f in os.listdir(SESSION_DIR) if f.endswith(".json")]
    imported = 0
    for fn in files:
        path = os.path.join(SESSION_DIR, fn)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        sid = fn[:-5]
        owner = (data.get("meta") or {}).get("owner", "")
        try:
            save_session(sid, owner, data)
            imported += 1
        except Exception:
            continue

    with tx() as conn:
        conn.execute("INSERT INTO meta(key, value) VALUES('legacy_imported', ?)"
                     " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (_now(),))
    if imported:
        obs.log("store.legacy_imported", files=imported)
