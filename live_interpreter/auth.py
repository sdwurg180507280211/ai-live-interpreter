"""控制台登录：密码 + 签名 Cookie，连续输错会暂时锁定。

Cookie 内容是「过期时间.签名」，签名密钥由数据目录里的随机密钥和登录密码共同派生：
改了密码，所有已登录的设备都会失效。
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import secrets
import time
from collections import deque
from pathlib import Path

from aiohttp import web

COOKIE_NAME = "interp_session"
SESSION_DAYS = 30
MAX_FAILURES = 8
FAILURE_WINDOW_S = 15 * 60


def _load_secret(path: Path) -> bytes:
    if path.is_file():
        return bytes.fromhex(path.read_text().strip())
    secret = secrets.token_bytes(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write(secret.hex())
    return secret


def client_ip(request: web.Request) -> str:
    """在本机反向代理后面时，取代理传来的真实客户端地址。"""
    remote = request.remote or ""
    try:
        behind_proxy = ipaddress.ip_address(remote).is_loopback
    except ValueError:
        behind_proxy = False
    forwarded = request.headers.get("X-Forwarded-For", "")
    if behind_proxy and forwarded:
        return forwarded.split(",")[0].strip()
    return remote


class Auth:
    def __init__(self, password: str, data_dir: Path, cookie_path: str = "/", clock=time.time) -> None:
        if not password:
            raise ValueError("缺少 ADMIN_PASSWORD")
        self._password = password
        # 和其他应用共用一个域名时，Cookie 只发给本应用的路径。
        self._cookie_path = cookie_path
        base = _load_secret(data_dir / "session.key")
        self._key = hashlib.sha256(base + b"|" + password.encode("utf-8")).digest()
        self._clock = clock
        self._failures: dict[str, deque[float]] = {}

    def _sign(self, payload: str) -> str:
        return hmac.new(self._key, payload.encode(), hashlib.sha256).hexdigest()

    def issue(self) -> str:
        expires = int(self._clock()) + SESSION_DAYS * 86400
        return f"{expires}.{self._sign(str(expires))}"

    def verify(self, cookie: str | None) -> bool:
        if not cookie or "." not in cookie:
            return False
        expires, signature = cookie.split(".", 1)
        if not expires.isdigit() or int(expires) < self._clock():
            return False
        return hmac.compare_digest(signature, self._sign(expires))

    def is_logged_in(self, request: web.Request) -> bool:
        return self.verify(request.cookies.get(COOKIE_NAME))

    def locked_for(self, ip: str) -> int:
        """返回还要锁定多少秒，0 表示可以尝试。"""
        failures = self._failures.get(ip)
        if not failures:
            return 0
        now = self._clock()
        while failures and now - failures[0] > FAILURE_WINDOW_S:
            failures.popleft()
        if len(failures) < MAX_FAILURES:
            return 0
        return int(FAILURE_WINDOW_S - (now - failures[0])) + 1

    def check_password(self, ip: str, password: str) -> bool:
        ok = hmac.compare_digest(password.encode("utf-8"), self._password.encode("utf-8"))
        if ok:
            self._failures.pop(ip, None)
        else:
            self._failures.setdefault(ip, deque()).append(self._clock())
        return ok

    def set_cookie(self, response: web.StreamResponse, secure: bool) -> None:
        response.set_cookie(
            COOKIE_NAME, self.issue(), max_age=SESSION_DAYS * 86400,
            httponly=True, secure=secure, samesite="Strict", path=self._cookie_path,
        )

    def clear_cookie(self, response: web.StreamResponse) -> None:
        response.del_cookie(COOKIE_NAME, path=self._cookie_path)
