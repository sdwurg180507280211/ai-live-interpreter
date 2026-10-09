import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from live_interpreter.auth import COOKIE_NAME, MAX_FAILURES, Auth
from live_interpreter.board import SubtitleBoard
from live_interpreter.capture import CaptureHub
from live_interpreter.config import AstConfig
from live_interpreter.pipeline import Subtitler
from live_interpreter.server import create_app
from live_interpreter.settings import SettingsStore, clean_display
from live_interpreter.volc_ast import AstResponse, Event

PASSWORD = "correct-horse"


class FakeRunner:
    def __init__(self, subtitler):
        self.subtitler = subtitler
        self.sessions = []

    async def __call__(self, ast, queue, finished):
        record = {"lang": ast.source_language, "hot_words": ast.hot_words, "audio": b""}
        self.sessions.append(record)
        while (chunk := await queue.get()) is not None:
            record["audio"] += chunk
            self.subtitler.handle(AstResponse(Event.TRANSLATION_START, start_ms=len(record["audio"])))
            self.subtitler.handle(AstResponse(Event.TRANSLATION_END, text=f"收到{len(chunk)}字节"))


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


class AuthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.auth = Auth(PASSWORD, Path(self.tmp.name), clock=self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def test_cookie_roundtrip_and_expiry(self):
        cookie = self.auth.issue()
        self.assertTrue(self.auth.verify(cookie))
        self.assertFalse(self.auth.verify(cookie[:-1] + ("0" if cookie[-1] != "0" else "1")))
        self.assertFalse(self.auth.verify("garbage"))
        self.clock.now += 31 * 86400
        self.assertFalse(self.auth.verify(cookie))

    def test_changing_password_invalidates_sessions(self):
        cookie = self.auth.issue()
        other = Auth("another-password", Path(self.tmp.name), clock=self.clock)
        self.assertFalse(other.verify(cookie))
        # 同一个密码、同一个数据目录（重启服务）仍然有效
        self.assertTrue(Auth(PASSWORD, Path(self.tmp.name), clock=self.clock).verify(cookie))

    def test_lockout_after_failures(self):
        for _ in range(MAX_FAILURES):
            self.assertFalse(self.auth.check_password("1.2.3.4", "wrong"))
        self.assertGreater(self.auth.locked_for("1.2.3.4"), 0)
        self.assertEqual(self.auth.locked_for("5.6.7.8"), 0)
        self.clock.now += 16 * 60
        self.assertEqual(self.auth.locked_for("1.2.3.4"), 0)


class SettingsTest(unittest.TestCase):
    def test_persist_and_validate(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SettingsStore(Path(tmp))
            token = store.view_token
            store.set_display({"lines": 4, "size": 8})
            store.write_glossary("OKR => 目标与关键成果\n")
            again = SettingsStore(Path(tmp))
            self.assertEqual(again.view_token, token)
            self.assertEqual(again.display, {"lines": 4, "size": 8.0, "show_en": False})
            self.assertIn("OKR", again.read_glossary())
            self.assertNotEqual(again.reset_view_token(), token)
        with self.assertRaises(ValueError):
            clean_display({"lines": 99})
        with self.assertRaises(ValueError):
            clean_display({"size": 0.5})


class ConsoleServerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        data = Path(self.tmp.name)
        self.board = SubtitleBoard()
        self.subtitler = Subtitler(self.board)
        self.runner = FakeRunner(self.subtitler)
        self.store = SettingsStore(data)
        self.store.write_glossary("Kubernetes\n")
        self.transcripts = data / "transcripts"

        def corpus():
            return ["Kubernetes"], []

        self.hub = CaptureHub(AstConfig(api_key="k"), self.board, self.subtitler, self.runner, self.transcripts, corpus=corpus)
        auth = Auth(PASSWORD, data)
        app = create_app(self.board, self.hub, auth=auth, settings=self.store, transcript_dir=self.transcripts)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.hub.close()
        self.tmp.cleanup()

    async def login(self):
        resp = await self.client.post("/api/login", json={"password": PASSWORD})
        self.assertEqual(resp.status, 200)
        self.assertIn(COOKIE_NAME, resp.cookies)

    async def test_index_shows_login_then_console(self):
        self.assertIn("请输入密码", await (await self.client.get("/")).text())
        bad = await self.client.post("/api/login", json={"password": "nope"})
        self.assertEqual(bad.status, 401)
        await self.login()
        self.assertIn("控制台", await (await self.client.get("/")).text())
        await self.client.post("/api/logout")
        self.client.session.cookie_jar.clear()
        self.assertIn("请输入密码", await (await self.client.get("/")).text())

    async def test_console_apis_require_login(self):
        for method, path in [("GET", "/api/console"), ("POST", "/api/display"), ("POST", "/api/glossary"),
                             ("POST", "/api/view-link/reset"), ("GET", "/api/transcripts"), ("POST", "/api/clear"),
                             ("GET", "/ws/capture")]:
            resp = await self.client.request(method, path)
            self.assertEqual(resp.status, 401, path)

    async def test_viewer_link(self):
        token = self.store.view_token
        self.assertEqual((await self.client.get("/api/state")).status, 403)
        self.assertEqual((await self.client.get(f"/api/state?key={token}")).status, 200)
        self.assertEqual((await self.client.get("/screen")).status, 200)
        await self.login()
        new = (await (await self.client.post("/api/view-link/reset")).json())["view_token"]
        self.client.session.cookie_jar.clear()
        self.assertEqual((await self.client.get(f"/api/state?key={token}")).status, 403)
        self.assertEqual((await self.client.get(f"/api/state?key={new}")).status, 200)

    async def test_reset_disconnects_open_screens(self):
        viewer = await self.client.ws_connect(f"/ws?key={self.store.view_token}")
        await viewer.receive()
        await self.login()
        await self.client.post("/api/view-link/reset")
        msg = await asyncio.wait_for(viewer.receive(), 2)
        self.assertEqual(msg.type, WSMsgType.CLOSE)
        self.assertEqual(viewer.close_code, 4001)

    async def test_reset_also_applies_in_logged_in_browser(self):
        # 管理员在自己（已登录）的浏览器里打开观众链接，重置后同样要失效
        await self.login()
        old = self.store.view_token
        viewer = await self.client.ws_connect(f"/ws?key={old}")
        await viewer.receive()
        await self.client.post("/api/view-link/reset")
        msg = await asyncio.wait_for(viewer.receive(), 2)
        self.assertEqual(msg.type, WSMsgType.CLOSE)
        self.assertEqual((await self.client.get(f"/api/state?key={old}")).status, 403)
        # 不带口令的字幕屏（控制台里「打开字幕屏」）仍按登录状态放行
        self.assertEqual((await self.client.get("/api/state")).status, 200)

    async def test_display_settings_pushed_to_screens(self):
        viewer = await self.client.ws_connect(f"/ws?key={self.store.view_token}")
        snapshot = json.loads((await viewer.receive()).data)
        self.assertEqual(snapshot["display"], {"lines": 3, "size": 6.0, "show_en": False})
        await self.login()
        resp = await self.client.post("/api/display", json={"size": 9, "show_en": True})
        self.assertEqual(resp.status, 200)
        update = json.loads((await asyncio.wait_for(viewer.receive(), 2)).data)
        self.assertEqual(update, {"type": "display", "lines": 3, "size": 9.0, "show_en": True})
        bad = await self.client.post("/api/display", json={"lines": 0})
        self.assertEqual(bad.status, 400)
        await viewer.close()

    async def test_glossary_save(self):
        await self.login()
        resp = await self.client.post("/api/glossary", json={"text": "OKR => 目标与关键成果\nKubernetes\n"})
        self.assertEqual((await resp.json())["terms"], 2)
        self.assertIn("OKR", self.store.read_glossary())
        too_many = "\n".join(f"word{i}" for i in range(1001))
        self.assertEqual((await self.client.post("/api/glossary", json={"text": too_many})).status, 400)

    async def test_capture_session_and_transcript_download(self):
        await self.login()
        viewer = await self.client.ws_connect(f"/ws?key={self.store.view_token}")
        await viewer.receive()
        capture = await self.client.ws_connect("/ws/capture")
        await capture.receive()
        await capture.send_str(json.dumps({"type": "start", "source_language": "ja"}))
        await capture.send_bytes(b"\x00\x01" * 640)

        while True:
            msg = await asyncio.wait_for(viewer.receive(), timeout=2)
            assert msg.type == WSMsgType.TEXT
            data = json.loads(msg.data)
            if data["type"] == "segment" and data["final"]:
                break
        self.assertEqual(data["zh"], "收到1280字节")
        await capture.send_str(json.dumps({"type": "stop"}))
        await capture.close()
        await viewer.close()
        await asyncio.sleep(0.05)

        self.assertEqual(self.runner.sessions[0]["lang"], "ja")
        self.assertEqual(self.runner.sessions[0]["hot_words"], ["Kubernetes"])
        self.assertEqual(self.store.source_language, "ja")
        items = (await (await self.client.get("/api/transcripts")).json())["items"]
        self.assertEqual(len(items), 1)
        download = await self.client.get(f"/api/transcripts/{items[0]['name']}")
        self.assertIn("收到1280字节", await download.text())
        self.assertIn("attachment", download.headers["Content-Disposition"])
        self.assertEqual((await self.client.get("/api/transcripts/..%2F.env")).status, 404)

    async def test_lockout_returns_429(self):
        for _ in range(MAX_FAILURES):
            await self.client.post("/api/login", json={"password": "wrong"})
        resp = await self.client.post("/api/login", json={"password": PASSWORD})
        self.assertEqual(resp.status, 429)


if __name__ == "__main__":
    unittest.main()
