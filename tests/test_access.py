"""分享给别人用时的访问控制测试。

核心要钉住的是三件事：
  1. 没开口令时，行为和单机版完全一致（不能因为加功能把老用法搞坏）
  2. 开了口令后，外人看不到任何数据，猜不到别人的任务
  3. 改模型配置这条路，外人怎么都进不来

测试用独立的临时数据库：绝不能把测试数据写进你自己的 data/app.db。
"""

from __future__ import annotations

import os
import tempfile
import unittest

# 必须在 import app 之前设：store 的 DB_PATH 是导入时就算好的
os.environ["CA_DB"] = os.path.join(tempfile.gettempdir(), f"ca_test_{os.getpid()}.db")
os.environ["WORKER_INLINE"] = "0"      # 测试里不要起后台 worker
os.environ["CA_LOG"] = "0"

from fastapi.testclient import TestClient          # noqa: E402
from starlette.requests import Request             # noqa: E402

import app as app_mod                              # noqa: E402
from core import access, store                     # noqa: E402

# 建表。lifespan 里也会调一次，init 是幂等的
store.init()

LOCAL = ("127.0.0.1", 50000)
REMOTE = ("203.0.113.7", 50000)     # 文档专用网段，不会撞上真实内网


def client(host=REMOTE, **kw):
    """TestClient 默认来源 IP 是 testclient，会被当成本机；这里显式指定。"""
    return TestClient(app_mod.app, client=host, **kw)


def fake_request(host="203.0.113.7", query="", cookie="", header=""):
    headers = []
    if cookie:
        headers.append((b"cookie", f"{access.COOKIE}={cookie}".encode()))
    if header:
        headers.append((b"x-access-code", header.encode()))
    return Request({
        "type": "http", "method": "GET", "path": "/", "headers": headers,
        "query_string": query.encode(), "client": (host, 50000),
    })


class Base(unittest.TestCase):
    def setUp(self):
        self._old = access.CODE

    def tearDown(self):
        access.CODE = self._old


class NoCodeTest(Base):
    """不开口令 = 单机版，一行行为都不能变。"""

    def test_remote_allowed_when_disabled(self):
        access.CODE = ""
        with client() as c:
            self.assertEqual(c.get("/api/history").status_code, 200)

    def test_index_served(self):
        access.CODE = ""
        with client() as c:
            r = c.get("/")
            self.assertEqual(r.status_code, 200)
            self.assertIn("课程助手", r.text)


class WithCodeTest(Base):
    def setUp(self):
        super().setUp()
        access.CODE = "s3cret"

    def test_remote_without_code_rejected(self):
        with client() as c:
            self.assertEqual(c.get("/api/history").status_code, 401)

    def test_remote_with_wrong_code_rejected(self):
        with client() as c:
            self.assertEqual(c.get("/api/history?k=nope").status_code, 401)

    def test_remote_with_code_ok(self):
        with client() as c:
            self.assertEqual(c.get("/api/history?k=s3cret").status_code, 200)

    def test_local_never_needs_code(self):
        with client(host=LOCAL) as c:
            self.assertEqual(c.get("/api/history").status_code, 200)

    def test_index_shows_login_page(self):
        with client() as c:
            r = c.get("/")
            self.assertEqual(r.status_code, 401)
            self.assertIn("访问口令", r.text)

    def test_login_sets_cookie_then_passes(self):
        with client() as c:
            r = c.post("/login", data={"k": "s3cret"}, follow_redirects=False)
            self.assertEqual(r.status_code, 303)
            self.assertIn(access.COOKIE, r.cookies)
            # 浏览器会自动带上 cookie，这里确认后续请求不再需要 ?k=
            self.assertEqual(c.get("/api/history").status_code, 200)

    def test_bad_login_does_not_set_cookie(self):
        with client() as c:
            r = c.post("/login", data={"k": "wrong"}, follow_redirects=False)
            self.assertEqual(r.status_code, 303)
            self.assertNotIn(access.COOKIE, r.cookies)
            self.assertEqual(c.get("/api/history").status_code, 401)

    def test_config_write_blocked_even_with_code(self):
        """口令能看计划，但不能改 key——这是两件不同的事。"""
        with client() as c:
            r = c.post("/api/config?k=s3cret", json={"api_key": "sk-x"})
            self.assertEqual(r.status_code, 403)

    def test_config_test_blocked(self):
        with client() as c:
            r = c.post("/api/config/test?k=s3cret", json={"api_key": "sk-x"})
            self.assertEqual(r.status_code, 403)

    def test_config_get_hides_admin_for_remote(self):
        with client() as c:
            d = c.get("/api/config?k=s3cret").json()
            self.assertFalse(d["admin"])

    def test_config_get_marks_admin_for_local(self):
        with client(host=LOCAL) as c:
            self.assertTrue(c.get("/api/config").json()["admin"])


class OwnerTest(Base):
    def setUp(self):
        super().setUp()
        # 两个口令 = 两个人。一个口令时所有人共用归属 id，隔离等于没做
        access.CODE = "code-a,code-b"
        self.tid = "testownertask"
        owner = access.owner_of("code-a")
        store.create_task(self.tid, owner=owner)
        store.update_task(self.tid, status="done", step="完成", progress=100)
        store.save_session(self.tid, owner, {"meta": {}, "knowledge": {"stats": {"count": 1}}})

    def tearDown(self):
        # 清掉，避免影响后面的用例
        with store.tx() as conn:
            conn.execute("DELETE FROM tasks WHERE id=?", (self.tid,))
            conn.execute("DELETE FROM sessions WHERE id=?", (self.tid,))
        super().tearDown()

    def test_other_code_cannot_read_task(self):
        with client() as c:
            r = c.get(f"/api/task/{self.tid}?k=code-b")
            self.assertEqual(r.status_code, 404)   # 404 而不是 403：不确认 id 存在

    def test_own_task_visible(self):
        with client() as c:
            self.assertEqual(c.get(f"/api/task/{self.tid}?k=code-a").status_code, 200)

    def test_other_code_cannot_read_result(self):
        """历史隔离不能只挡任务状态，结果本身也要挡。"""
        with client() as c:
            self.assertEqual(c.get(f"/api/result/{self.tid}?k=code-b").status_code, 404)
            self.assertEqual(c.get(f"/api/result-by-id/{self.tid}?k=code-b").status_code, 404)
            self.assertEqual(c.get(f"/api/result-by-id/{self.tid}?k=code-a").status_code, 200)

    def test_history_only_lists_own(self):
        with client() as c:
            ids = [x["id"] for x in c.get("/api/history?k=code-b").json()]
        self.assertNotIn(self.tid, ids)

    def test_two_codes_mean_two_owners(self):
        self.assertNotEqual(access.owner_of("code-a"), access.owner_of("code-b"))

    def test_result_by_id_rejects_path_tricks(self):
        """路径穿越要在函数里挡：走 HTTP 时客户端会先把 ../ 规范化掉，测不出来。"""
        import asyncio
        for bad in ["../config", "a/b", "..", "conf ig"]:
            with self.assertRaises(Exception):
                asyncio.run(app_mod.result_by_id(bad, owner=""))


class OwnerHashTest(unittest.TestCase):
    def test_same_code_same_owner(self):
        self.assertEqual(access.owner_of("abc"), access.owner_of("abc"))

    def test_different_code_different_owner(self):
        self.assertNotEqual(access.owner_of("abc"), access.owner_of("abd"))

    def test_code_not_stored_in_plain(self):
        self.assertNotIn("abc", access.owner_of("abc"))

    def test_local_has_empty_owner(self):
        self.assertEqual(access.owner_of(""), "")


class RequireAdminTest(Base):
    def test_admin_dependency_rejects_remote(self):
        access.CODE = "s3cret"
        with self.assertRaises(Exception) as ctx:
            import asyncio
            asyncio.run(access.require_admin(fake_request()))
        self.assertIn("本机", str(ctx.exception))

    def test_admin_dependency_allows_local_even_with_code(self):
        access.CODE = "s3cret"
        import asyncio
        self.assertEqual(asyncio.run(access.require_admin(fake_request(host="127.0.0.1"))), "")


if __name__ == "__main__":
    unittest.main()
