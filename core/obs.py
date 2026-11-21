"""结构化日志 + 最小可观测。

这个项目跑在一台 2C2G 上，引入 Prometheus + 时序库的运维成本远超收益。
所以只做两件真正有用的事：

1. 每条日志是**一行合法 JSON**：能 grep、能 jq、能直接喂给任何采集器
2. 指标**从状态表派生**，不另存一份：任务数、耗时、失败率都是 SQL 算的，
   不会出现「指标和真实状态对不上」这种最难查的问题

以后接 Prometheus / Loki，只需要在这里挂一个出口，业务代码一行不用改。
"""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(BASE, "data", "logs")

# 开发时把日志同时打到 stderr，省得每次去翻文件；部署时关掉免得占满 journal
_ECHO = os.environ.get("CA_LOG_ECHO", "1") != "0"
_ENABLED = os.environ.get("CA_LOG", "1") != "0"


def _path() -> str:
    os.makedirs(LOG_DIR, exist_ok=True)
    return os.path.join(LOG_DIR, f"app-{datetime.now():%Y%m%d}.jsonl")


def log(event: str, **fields) -> None:
    """记一条事件。

    用 os.write + O_APPEND 单次写：POSIX 规定小于 PIPE_BUF 的追加写是原子的，
    多进程同时写不会互相截断。用 open(...,"a") + write 在 Windows 上偶发
    交错，这是踩过的坑。
    """
    if not _ENABLED:
        return
    rec = {"ts": datetime.now().isoformat(timespec="milliseconds"), "event": event}
    rec.update(fields)
    line = json.dumps(rec, ensure_ascii=False, default=str) + "\n"
    try:
        fd = os.open(_path(), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:
        pass                       # 日志写不进去不能影响业务
    if _ECHO:
        sys.stderr.write(line)


@asynccontextmanager
async def span(event: str, **fields):
    """异步耗时埋点：无论成功失败都记一条，失败带上异常类型。"""
    t0 = time.perf_counter()
    try:
        yield
    except Exception as e:
        log(event, ok=False, ms=round((time.perf_counter() - t0) * 1000, 1),
            err=f"{type(e).__name__}: {str(e)[:180]}", **fields)
        raise
    else:
        log(event, ok=True, ms=round((time.perf_counter() - t0) * 1000, 1), **fields)


@contextmanager
def timer(event: str, **fields):
    """同步耗时埋点（解析文档、SQL 这类）。"""
    t0 = time.perf_counter()
    try:
        yield
    except Exception as e:
        log(event, ok=False, ms=round((time.perf_counter() - t0) * 1000, 1),
            err=f"{type(e).__name__}: {str(e)[:180]}", **fields)
        raise
    else:
        log(event, ok=True, ms=round((time.perf_counter() - t0) * 1000, 1), **fields)


def tail(n: int = 50) -> list[dict]:
    """读最近 n 条日志，给 /api/metrics 用。文件不存在就返回空。"""
    try:
        path = _path()
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()[-n:]
    except FileNotFoundError:
        return []
    except Exception:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out
