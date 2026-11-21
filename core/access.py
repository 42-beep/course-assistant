"""分享给别人用时的最小访问控制。

这里刻意**不做用户体系**——没有注册、没有密码库、没有 session 表。
只有两件事：

1. 一道口令（环境变量 ACCESS_CODE，空则不启用，行为和单机版完全一致）
2. 一个判断：这次请求是不是从本机发来的

本机不发口令直接放行，所以你自己的浏览器永远不用输。别人从局域网或
公网进来，必须带口令，否则只能看到登录页。

为什么不引真正的账号系统：这个工具的使用场景是「一个宿舍/一个班几十个人，
用两三周」。上账号系统要数据库、要找回密码、要防撞库，成本远超收益。
口令挡的是「隔壁宿舍随便点进来消耗你的 API 额度」，不是黑客——这个定位
要说清楚，否则会给人虚假的安全感。
"""

from __future__ import annotations

import hashlib
import os
import secrets

from fastapi import HTTPException, Request, Response

CODE = os.environ.get("ACCESS_CODE", "").strip()
COOKIE = "ca_key"
_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def codes() -> list[str]:
    """支持一次配多个口令，逗号分隔。

    为什么需要：只有一个口令时，所有人的归属 id 是同一个，等于
    「同学 A 打开历史能看到 B 传的课件和考试日期」。每人一个口令之后，
    归属各自独立，历史自然互不可见——不用引入账号体系就解决了。
    """
    return [c.strip() for c in CODE.split(",") if c.strip()]


def enabled() -> bool:
    return bool(codes())


def is_local(request: Request) -> bool:
    """只有真正从本机发起（含 TestClient）才算自己人。"""
    host = request.client.host if request.client else ""
    return host in _LOCAL_HOSTS


def owner_of(code: str) -> str:
    """口令 → 归属标记。存哈希不存原文：会话文件里不该出现别人的口令。"""
    return hashlib.sha256(code.encode()).hexdigest()[:8] if code else ""


def _supplied(request: Request) -> str:
    return (request.query_params.get("k")
            or request.cookies.get(COOKIE)
            or request.headers.get("X-Access-Code")
            or "")


def match(request: Request) -> str:
    """返回命中的那个口令原文，没命中返回空串。

    逐个比而不是拼起来比：compare_digest 是定长的，一次比一个才不泄露
    「前几位对上了」这种信息。返回原文是为了拿它派生归属。
    """
    got = _supplied(request)
    if not got:
        return ""
    for c in codes():
        if secrets.compare_digest(got, c):
            return got
    return ""


def valid(request: Request) -> bool:
    if not enabled() or is_local(request):
        return True
    return bool(match(request))


async def require(request: Request) -> str:
    """依赖项：通过则返回归属 id（本机为空串），否则 401。

    返回的是 owner id 而不是布尔值，因为调用方几乎总要拿它去过滤数据。
    """
    if not enabled() or is_local(request):
        return ""
    got = match(request)
    if not got:
        raise HTTPException(401, "需要访问口令：请用管理员给你的链接打开")
    return owner_of(got)


async def require_admin(request: Request) -> str:
    """改模型配置只有本机能做。

    这条比口令更要紧：/api/config 会把 api_key 写进 config.json，
    谁都能调就等于谁都能把你的 key 换成他的，或者把 base_url 指到
    他自己搭的服务器上，把你以后传的每份资料都收走。
    """
    if not is_local(request):
        raise HTTPException(403, "模型配置只能在服务器本机上修改")
    return ""


def set_cookie(response: Response, code: str) -> None:
    """登录后把口令存 cookie，省得每个请求都带。

    httponly：前端 JS 读不到；samesite=lax：别的站点发起的请求带不上。
    """
    response.set_cookie(COOKIE, code, max_age=30 * 24 * 3600,
                        httponly=True, samesite="lax")
