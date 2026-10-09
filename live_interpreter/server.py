"""网页服务。

云端模式（传入 auth）：
- /          未登录显示登录页，登录后是控制台（收音、观众链接、显示设置、术语表、会议记录）
- /screen    字幕屏，观众用带 key 的分享链接打开，不用登录；已登录也能直接打开
本机模式（不传 auth）：/ 和 /screen 都是字幕屏，任何人可看，只有本机能清屏，没有控制台。
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import re
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote

from aiohttp import WSMsgType, web

from .auth import Auth, client_ip
from .board import SubtitleBoard
from .capture import SOURCE_LANGUAGES, CaptureHub
from .glossary import parse_glossary, split_corpus
from .settings import SettingsStore
from .volc_ast import MAX_CORPUS_ITEMS

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
BOARD_KEY = web.AppKey("board", SubtitleBoard)
HUB_KEY = web.AppKey("hub", object)
AUTH_KEY = web.AppKey("auth", object)
SETTINGS_KEY = web.AppKey("settings", object)
TRANSCRIPTS_KEY = web.AppKey("transcripts", object)
BRAND_KEY = web.AppKey("brand", str)
# 用观众链接连进来的字幕屏，重置链接时要把它们断开。
VIEWERS_KEY = web.AppKey("viewers", set)
CLOSE_LINK_RESET = 4001

NO_STORE = {"Cache-Control": "no-store"}
TRANSCRIPT_NAME = re.compile(r"^\d{8}-\d{6}\.jsonl$")


def _is_loopback(request: web.Request) -> bool:
    try:
        return ipaddress.ip_address(request.remote or "").is_loopback
    except ValueError:
        return False


def _auth(request: web.Request) -> Auth | None:
    return request.app[AUTH_KEY]


def _logged_in(request: web.Request) -> bool:
    auth = _auth(request)
    return auth.is_logged_in(request) if auth else _is_loopback(request)


def _can_view(request: web.Request) -> bool:
    auth = _auth(request)
    if auth is None:
        return True
    # 带了观众口令就只认口令：观众链接在已登录的浏览器里也一样会因重置而失效。
    # 不带口令（控制台里「打开字幕屏」）才按登录状态放行。
    key = request.query.get("key", "")
    if key:
        store: SettingsStore = request.app[SETTINGS_KEY]
        return hmac.compare_digest(key.encode(), store.view_token.encode())
    return auth.is_logged_in(request)


def _require_login(request: web.Request) -> None:
    if not _logged_in(request):
        raise web.HTTPUnauthorized(text="请先登录")


def _page(request: web.Request, name: str) -> web.Response:
    """页面里的 {{BRAND}} 替换成产品名称（BRAND_NAME），方便给不同客户换名字。"""
    html = (STATIC_DIR / name).read_text(encoding="utf-8")
    html = html.replace("{{BRAND}}", escape(request.app[BRAND_KEY]))
    return web.Response(text=html, content_type="text/html", charset="utf-8", headers=NO_STORE)


async def index(request: web.Request) -> web.Response:
    if _auth(request) is None:
        return _page(request, "index.html")
    return _page(request, "console.html" if _logged_in(request) else "login.html")


async def screen(request: web.Request) -> web.Response:
    return _page(request, "index.html")


async def login(request: web.Request) -> web.Response:
    auth = _auth(request)
    if auth is None:
        raise web.HTTPNotFound()
    ip = client_ip(request)
    wait = auth.locked_for(ip)
    if wait:
        return web.json_response({"ok": False, "error": f"密码错误次数过多，请 {wait // 60 + 1} 分钟后再试"}, status=429)
    try:
        password = str((await request.json()).get("password", ""))
    except (json.JSONDecodeError, AttributeError):
        password = ""
    if not auth.check_password(ip, password):
        log.warning("登录失败 %s", ip)
        await asyncio.sleep(1)
        return web.json_response({"ok": False, "error": "密码不正确"}, status=401)
    log.info("登录成功 %s", ip)
    response = web.json_response({"ok": True})
    secure = request.secure or request.headers.get("X-Forwarded-Proto") == "https"
    auth.set_cookie(response, secure=secure)
    return response


async def logout(request: web.Request) -> web.Response:
    response = web.json_response({"ok": True})
    auth = _auth(request)
    if auth is not None:
        auth.clear_cookie(response)
    return response


async def state(request: web.Request) -> web.Response:
    if not _can_view(request):
        raise web.HTTPForbidden(text="链接无效或已重置")
    return web.json_response(request.app[BOARD_KEY].snapshot())


async def clear(request: web.Request) -> web.Response:
    _require_login(request)
    request.app[BOARD_KEY].clear()
    return web.json_response({"ok": True})


async def console_state(request: web.Request) -> web.Response:
    _require_login(request)
    store: SettingsStore = request.app[SETTINGS_KEY]
    return web.json_response({
        "view_token": store.view_token,
        "display": store.display,
        "source_language": store.source_language,
        "glossary": store.read_glossary(),
    })


async def update_display(request: web.Request) -> web.Response:
    _require_login(request)
    store: SettingsStore = request.app[SETTINGS_KEY]
    try:
        display = store.set_display(await request.json())
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    request.app[BOARD_KEY].set_display(display)
    return web.json_response({"ok": True, "display": display})


async def reset_view_link(request: web.Request) -> web.Response:
    _require_login(request)
    store: SettingsStore = request.app[SETTINGS_KEY]
    token = store.reset_view_token()
    # 已经打开的字幕屏也立即失效：断开后它们重连会被拒绝，并提示链接已失效。
    # 断开时连接会自己从集合里移除，先复制一份再逐个关闭，计数也用这份
    viewers = list(request.app[VIEWERS_KEY])
    for ws in viewers:
        await ws.close(code=CLOSE_LINK_RESET, message="link reset".encode())
    log.info("观众链接已重置，断开 %d 个字幕屏", len(viewers))
    return web.json_response({"ok": True, "view_token": token})


async def update_glossary(request: web.Request) -> web.Response:
    _require_login(request)
    store: SettingsStore = request.app[SETTINGS_KEY]
    try:
        text = str((await request.json()).get("text", ""))
        hot_words, glossary = split_corpus(parse_glossary(text))
        if len(hot_words) + len(glossary) > MAX_CORPUS_ITEMS:
            raise ValueError(f"热词和术语合计不能超过 {MAX_CORPUS_ITEMS} 条")
        store.write_glossary(text)
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    return web.json_response({"ok": True, "terms": len(hot_words)})


async def list_transcripts(request: web.Request) -> web.Response:
    _require_login(request)
    directory: Path = request.app[TRANSCRIPTS_KEY]
    items = []
    for path in sorted(directory.glob("*.jsonl"), reverse=True):
        if not TRANSCRIPT_NAME.match(path.name):
            continue
        with path.open(encoding="utf-8") as file:
            count = sum(1 for line in file if line.strip())
        if count:
            items.append({"name": path.name, "sentences": count})
    return web.json_response({"items": items[:100]})


async def download_transcript(request: web.Request) -> web.Response:
    _require_login(request)
    name = request.match_info["name"]
    if not TRANSCRIPT_NAME.match(name):
        raise web.HTTPNotFound()
    path: Path = request.app[TRANSCRIPTS_KEY] / name
    if not path.is_file():
        raise web.HTTPNotFound()
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        record = json.loads(raw)
        lines.append(f"[{record.get('time', '')}]\n{record.get('en', '')}\n{record.get('zh', '')}\n")
    filename = f"会议记录-{name[:15]}.txt"
    return web.Response(
        text="\n".join(lines),
        content_type="text/plain",
        charset="utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}", **NO_STORE},
    )


def _subscribe(ws: web.WebSocketResponse, board: SubtitleBoard):
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)

    def listener(message: dict[str, Any]) -> None:
        if not queue.full():
            queue.put_nowait(message)

    async def pump() -> None:
        while True:
            message = await queue.get()
            await ws.send_str(json.dumps(message, ensure_ascii=False))

    return listener, pump


async def websocket(request: web.Request) -> web.WebSocketResponse:
    if not _can_view(request):
        raise web.HTTPForbidden(text="链接无效或已重置")
    board = request.app[BOARD_KEY]
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    listener, pump = _subscribe(ws, board)
    await ws.send_str(json.dumps(board.snapshot(), ensure_ascii=False))
    board.subscribe(listener)
    sender = asyncio.create_task(pump())
    # 登录用户打开的字幕屏不靠观众链接，重置链接时不断开。
    viewers: set[web.WebSocketResponse] = request.app[VIEWERS_KEY]
    by_link = _auth(request) is not None and bool(request.query.get("key"))
    if by_link:
        viewers.add(ws)
    try:
        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
                break
    finally:
        viewers.discard(ws)
        board.unsubscribe(listener)
        sender.cancel()
    return ws


async def capture_websocket(request: web.Request) -> web.WebSocketResponse:
    """收音端：文本消息是控制指令，二进制消息是 16kHz 16bit 单声道 PCM。"""
    hub: CaptureHub | None = request.app[HUB_KEY]
    if hub is None:
        raise web.HTTPNotFound()
    _require_login(request)
    board = request.app[BOARD_KEY]
    store: SettingsStore = request.app[SETTINGS_KEY]
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1 << 20)
    await ws.prepare(request)
    listener, pump = _subscribe(ws, board)
    await ws.send_str(json.dumps(board.snapshot(), ensure_ascii=False))
    board.subscribe(listener)
    sender = asyncio.create_task(pump())
    log.info("收音端已连接 %s", client_ip(request))
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                hub.feed(listener, msg.data)
            elif msg.type == WSMsgType.TEXT:
                try:
                    command = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if command.get("type") == "start":
                    language = str(command.get("source_language", "en"))
                    try:
                        await hub.start(listener, language)
                    except ValueError as exc:
                        listener({"type": "capture", "state": "error", "detail": str(exc)})
                        continue
                    store.set_source_language(language)
                    listener({"type": "capture", "state": "started", "source_language": hub.source_language})
                elif command.get("type") == "stop":
                    await hub.stop(listener)
                    listener({"type": "capture", "state": "stopped"})
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        board.unsubscribe(listener)
        await hub.stop(listener)
        sender.cancel()
        log.info("收音端已断开 %s", client_ip(request))
    return ws


def create_app(
    board: SubtitleBoard,
    hub: CaptureHub | None = None,
    auth: Auth | None = None,
    settings: SettingsStore | None = None,
    transcript_dir: Path | None = None,
    brand: str = "AI 同传",
) -> web.Application:
    if auth is not None and settings is None:
        raise ValueError("云端模式需要 settings")
    app = web.Application(client_max_size=1 << 20)
    app[BOARD_KEY] = board
    app[HUB_KEY] = hub
    app[AUTH_KEY] = auth
    app[SETTINGS_KEY] = settings
    app[TRANSCRIPTS_KEY] = transcript_dir or Path(".")
    app[BRAND_KEY] = brand
    app[VIEWERS_KEY] = set()
    if settings is not None:
        board.set_display(settings.display)
    app.router.add_get("/", index)
    app.router.add_get("/screen", screen)
    app.router.add_static("/assets/", STATIC_DIR / "assets", append_version=False)
    app.router.add_get("/ws", websocket)
    app.router.add_get("/api/state", state)
    app.router.add_post("/api/clear", clear)
    if auth is not None:
        app.router.add_post("/api/login", login)
        app.router.add_post("/api/logout", logout)
        app.router.add_get("/ws/capture", capture_websocket)
        app.router.add_get("/api/console", console_state)
        app.router.add_post("/api/display", update_display)
        app.router.add_post("/api/view-link/reset", reset_view_link)
        app.router.add_post("/api/glossary", update_glossary)
        app.router.add_get("/api/transcripts", list_transcripts)
        app.router.add_get("/api/transcripts/{name}", download_transcript)
    return app
