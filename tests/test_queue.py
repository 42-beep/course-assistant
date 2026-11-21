"""队列与存储层测试。

这一组要钉住的是「进程挂了会怎样」，而不是「功能对不对」：

  1. 多个 worker 同时抢，同一条任务只能被抢一次（不重复执行）
  2. worker 抢完就崩，租约到期后任务能被别人捡回来（不丢任务）
  3. 失败重试有上限，超了标 failed 而不是无限烧钱
  4. 任务状态真的在磁盘上：换一个**独立进程**也能读到

第 4 条是这次改造的核心主张。测它必须用子进程——同进程读同一个 SQLite
永远成功，证明不了任何事。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from core import queue, store      # noqa: E402

# 独立测试库：不碰你自己跑着的 data/app.db。
# 放在项目目录而不是系统临时目录——Windows 上临时目录的 SQLite WAL
# 共享内存文件可能拿不到写权限，会报 "attempt to write a readonly database"。
store.DB_PATH = os.path.join(BASE, "data", f"_test_queue_{os.getpid()}.db")

# 上次异常退出可能留下 -wal/-shm，带着它们初始化会让子进程写不进去。
# 每次从干净的库开始，测试结果才不会受上一次运行的脏状态影响。
for _suffix in ("", "-wal", "-shm"):
    try:
        os.remove(store.DB_PATH + _suffix)
    except OSError:
        pass

store.init()


def tearDownModule():
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(store.DB_PATH + suffix)
        except OSError:
            pass


def _clean() -> None:
    with store.tx() as conn:
        for t in ("jobs", "tasks", "workers"):
            conn.execute(f"DELETE FROM {t}")


class QueueTest(unittest.TestCase):
    def setUp(self):
        _clean()

    def _task(self, tid: str) -> None:
        store.create_task(tid)

    def test_claim_is_exclusive(self):
        """8 个任务、6 个线程同时抢，不能有任何一个被抢两次。

        线程各自开独立连接，等于模拟多个 worker——SQLite 的写锁是真的
        跨连接生效的，所以这能证明租约语句的有效性。
        """
        for i in range(8):
            self._task(f"t{i}")
            queue.enqueue(f"t{i}", "analyze", {"n": i})

        got: list[int] = []
        lock = threading.Lock()

        def grab(wid: str) -> None:
            while True:
                job = queue.claim(wid)
                if not job:
                    return
                with lock:
                    got.append(job["id"])

        threads = [threading.Thread(target=grab, args=(f"w{i}",)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(got), 8, "有任务没被抢到")
        self.assertEqual(len(set(got)), 8, "同一个任务被两个 worker 抢到了")

    def test_expired_lease_is_reclaimed(self):
        """worker 抢完就崩（不续租），任务要能被别人捡回来。

        这是 at-least-once 的来源：宁可重跑一次，也不能永远卡在 running。
        """
        self._task("t1")
        queue.enqueue("t1", "analyze", {})

        first = queue.claim("w-dead", lease=0.05)
        self.assertIsNotNone(first)
        self.assertIsNone(queue.claim("w-other", lease=5), "租约没过期不该被抢")

        time.sleep(0.08)
        second = queue.claim("w-alive", lease=30)
        self.assertIsNotNone(second, "租约过期后应该能捡回来")
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["attempts"], 2)

    def test_retry_has_ceiling(self):
        """失败重试到上限就放弃，不能无限重投。"""
        self._task("t2")
        queue.enqueue("t2", "analyze", {}, max_attempts=2)

        job = queue.claim("w1")
        self.assertEqual(queue.fail(job["id"], "timeout"), "retry")

        job = queue.claim("w1")
        self.assertEqual(job["attempts"], 2)
        self.assertEqual(queue.fail(job["id"], "timeout"), "failed")

        self.assertIsNone(queue.claim("w1"), "已经 failed 的任务不该再被抢")

    def test_finish_removes_from_depth(self):
        self._task("t3")
        queue.enqueue("t3", "analyze", {})
        self.assertEqual(queue.depth(), 1)
        job = queue.claim("w1")
        queue.finish(job["id"])
        self.assertEqual(queue.depth(), 0)

    def test_depth_counts_queued_plus_abandoned(self):
        for i in range(3):
            self._task(f"d{i}")
            queue.enqueue(f"d{i}", "analyze", {})
        self.assertEqual(queue.depth(), 3)
        job = queue.claim("w1", lease=0.05)
        self.assertEqual(queue.depth(), 2, "正在跑的不算排队")
        time.sleep(0.08)
        self.assertEqual(queue.depth(), 3, "租约过期的要算回排队")

    def test_payload_roundtrip(self):
        """payload 里有路径、中文课程名，走一遍 JSON 不能变形。"""
        self._task("t4")
        payload = {"paths": ["D:/教学/课件 1.pptx"], "course": "数据结构", "exam": "2027-01-10"}
        queue.enqueue("t4", "analyze", payload)
        job = queue.claim("w1")
        self.assertEqual(job["payload"], payload)

    def test_enqueue_after_create_task(self):
        """任务行必须先于 job 存在，否则 worker 抢到任务却查不到归属。"""
        store.create_task("t5", owner="abc")
        queue.enqueue("t5", "analyze", {})
        job = queue.claim("w1")
        self.assertEqual(store.get_task(job["task_id"])["owner"], "abc")


class StoreTest(unittest.TestCase):
    def setUp(self):
        _clean()

    def test_session_upsert_not_duplicate(self):
        """rerun 会重写同一份结果，应该是覆盖不是新增（否则历史里出现两条）。"""
        store.save_session("s1", "", {"meta": {"course": "数据结构"}, "knowledge": {"stats": {"count": 5}}})
        store.save_session("s1", "", {"meta": {"course": "数据结构", "exam_date": "2027-01-02"},
                                      "knowledge": {"stats": {"count": 7}}})
        items = [x for x in store.list_sessions("", 50) if x["id"] == "s1"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["points"], 7)
        self.assertEqual(items[0]["exam_date"], "2027-01-02")

    def test_session_owner_isolation(self):
        store.save_session("a1", "user-a", {"meta": {}, "knowledge": {}})
        store.save_session("b1", "user-b", {"meta": {}, "knowledge": {}})
        self.assertEqual([x["id"] for x in store.list_sessions("user-a", 50)], ["a1"])
        self.assertEqual(store.get_session_owner("b1"), "user-b")

    def test_update_task_ignores_unknown_fields(self):
        """只允许改白名单字段。防止某次手滑把 created_at 覆盖掉。"""
        store.create_task("t9", owner="x")
        store.update_task("t9", status="running", created_at="1970-01-01", hacker="1")
        t = store.get_task("t9")
        self.assertEqual(t["status"], "running")
        self.assertNotEqual(t["created_at"][:4], "1970")

    def test_stats_derives_from_state(self):
        store.create_task("s-a")
        store.create_task("s-b")
        store.update_task("s-a", status="done", progress=100)
        store.update_task("s-b", status="error", error="boom")
        s = store.stats()
        self.assertEqual(s["tasks"].get("done"), 1)
        self.assertEqual(s["tasks"].get("error"), 1)
        self.assertEqual(len(s["recent_errors"]), 1)

    def test_state_survives_new_process(self):
        """核心主张：状态在磁盘上，不在进程里。

        用真正的子进程去读——同进程读同一个连接永远成功，证明不了什么。
        """
        store.create_task("survive", owner="owner-1")
        store.update_task("survive", status="running", step="抽取知识点", progress=30)

        code = (
            "import json,os;"
            "from core import store;"
            "t=store.get_task('survive');"
            "print(json.dumps(t, ensure_ascii=False))"
        )
        env = {**os.environ, "CA_DB": store.DB_PATH, "CA_LOG": "0"}
        out = subprocess.run([sys.executable, "-c", code], cwd=BASE, env=env,
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr[-500:])
        t = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(t["status"], "running")
        self.assertEqual(t["step"], "抽取知识点")
        self.assertEqual(t["owner"], "owner-1")

    def test_two_processes_never_claim_same_job(self):
        """两个独立进程同时抢 6 个任务，合起来必须正好 6 个且不重复。

        这是「多 worker」这个说法的唯一硬证据——线程共享一个解释器，
        子进程才是真的两套东西。
        """
        for i in range(6):
            store.create_task(f"m{i}")
            queue.enqueue(f"m{i}", "analyze", {})

        code = (
            "import json,sys;"
            "from core import queue;"
            "ids=[];"
            "j=queue.claim('w'+sys.argv[1]);\n"
            "while j:\n"
            "    ids.append(j['id']); j=queue.claim('w'+sys.argv[1])\n"
            "print(json.dumps(ids))"
        )
        env = {**os.environ, "CA_DB": store.DB_PATH, "CA_LOG": "0"}
        procs = [subprocess.Popen([sys.executable, "-c", code, str(i)], cwd=BASE, env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for i in range(2)]

        claimed: list[int] = []
        for p in procs:
            out, err = p.communicate(timeout=90)
            self.assertEqual(p.returncode, 0, err[-500:])
            claimed.extend(json.loads(out.strip().splitlines()[-1]))

        self.assertEqual(len(claimed), 6, f"合起来只抢到 {len(claimed)} 个：{claimed}")
        self.assertEqual(len(set(claimed)), 6, f"有重复：{sorted(claimed)}")


if __name__ == "__main__":
    unittest.main()
