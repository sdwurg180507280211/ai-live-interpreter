import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from live_interpreter.board import SubtitleBoard
from live_interpreter.capture import CaptureHub
from live_interpreter.config import AstConfig
from live_interpreter.pipeline import Subtitler
from live_interpreter.server import create_app
from live_interpreter.volc_ast import AstResponse, Event


class FakeRunner:
    """代替真实同传会话：记录收到的音频，收到 None（结束）后返回。"""

    def __init__(self, subtitler: Subtitler):
        self.subtitler = subtitler
        self.sessions = []

    async def __call__(self, ast, queue, finished):
        record = {"lang": ast.source_language, "audio": b"", "finished_cleanly": False}
        self.sessions.append(record)
        while True:
            chunk = await queue.get()
            if chunk is None:
                record["finished_cleanly"] = finished()
                return
            record["audio"] += chunk
            self.subtitler.handle(AstResponse(Event.TRANSLATION_START, start_ms=len(record["audio"])))
            self.subtitler.handle(AstResponse(Event.TRANSLATION_END, text=f"收到{len(chunk)}字节"))


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.board = SubtitleBoard()
        self.subtitler = Subtitler(self.board)
        self.runner = FakeRunner(self.subtitler)
        self.hub = CaptureHub(AstConfig(api_key="k"), self.board, self.subtitler, self.runner, Path(self.tmp.name))

    async def asyncTearDown(self):
        await self.hub.close()
        self.tmp.cleanup()


class CaptureHubTest(Base):
    async def test_start_feed_stop(self):
        owner = lambda message: None  # noqa: E731
        await self.hub.start(owner, "ja")
        self.hub.feed(owner, b"\x01\x00" * 10)
        await asyncio.sleep(0)
        await self.hub.stop(owner)
        session = self.runner.sessions[0]
        self.assertEqual(session["lang"], "ja")
        self.assertEqual(session["audio"], b"\x01\x00" * 10)
        self.assertTrue(session["finished_cleanly"])
        self.assertEqual(self.board.snapshot()["status"]["state"], "idle")
        transcripts = list(Path(self.tmp.name).glob("*.jsonl"))
        self.assertEqual(len(transcripts), 1)
        self.assertIn("收到20字节", transcripts[0].read_text(encoding="utf-8"))

    async def test_takeover_notifies_previous_owner(self):
        notices = []
        first = notices.append
        second = lambda message: None  # noqa: E731
        await self.hub.start(first, "en")
        await self.hub.start(second, "en")
        self.assertEqual(notices, [{"type": "capture", "state": "replaced"}])
        self.assertIs(self.hub.owner, second)
        # 旧收音端的音频和停止指令都被忽略
        self.hub.feed(first, b"xx")
        await self.hub.stop(first)
        self.assertIs(self.hub.owner, second)
        await asyncio.sleep(0)  # 让新会话任务开始运行
        self.assertEqual(len(self.runner.sessions), 2)

    async def test_rejects_unknown_language(self):
        with self.assertRaises(ValueError):
            await self.hub.start(lambda m: None, "xx")


class LocalModeTest(unittest.IsolatedAsyncioTestCase):
    async def test_no_capture_page_without_hub(self):
        client = TestClient(TestServer(create_app(SubtitleBoard())))
        await client.start_server()
        try:
            self.assertEqual((await client.get("/ws/capture")).status, 404)
            self.assertEqual((await client.get("/api/state")).status, 200)
        finally:
            await client.close()


if __name__ == "__main__":
    unittest.main()
