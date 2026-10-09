import asyncio
import unittest

import aiohttp
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

from live_interpreter.config import AstConfig
from live_interpreter.volc_ast import (
    AstError,
    Event,
    _int,
    _msg,
    _str,
    _varint,
    build_audio,
    build_finish_session,
    build_start_session,
    decode_fields,
    interpret,
    parse_response,
)


def fields(data: bytes) -> dict:
    """把一层 protobuf 解成 {字段号: [值...]}。"""
    out: dict = {}
    for field, _, value in decode_fields(data):
        out.setdefault(field, []).append(value)
    return out


def response(event: int, text: str = "", start: int = 0, end: int = 0,
             status: int = 20000000, message: str = "OK") -> bytes:
    meta = _str(1, "sid") + _int(3, status) + _str(4, message)
    return _msg(1, meta) + _int(2, event) + _str(4, text) + _int(5, start) + _int(6, end)


class CodecTest(unittest.TestCase):
    def test_varint(self):
        self.assertEqual(_varint(1), b"\x01")
        self.assertEqual(_varint(300), b"\xac\x02")
        self.assertEqual(len(_varint(-1)), 10)

    def test_start_session(self):
        cfg = AstConfig(api_key="k", hot_words=["OKR", "Kubernetes"], glossary=[("OKR", "目标与关键成果")], denoise=True)
        top = fields(build_start_session(cfg, "sid-1"))
        self.assertEqual(fields(top[1][0])[6], [b"sid-1"])          # request_meta.SessionID
        self.assertEqual(top[2], [Event.START_SESSION])
        audio = fields(top[4][0])
        self.assertEqual((audio[4], audio[5], audio[7], audio[8], audio[9]), ([b"wav"], [b"raw"], [16000], [16], [1]))
        params = fields(top[6][0])
        self.assertEqual((params[1], params[2], params[3]), ([b"s2t"], [b"en"], [b"zh"]))
        corpus = fields(params[100][0])
        self.assertEqual(corpus[9], [b"OKR", b"Kubernetes"])
        entry = fields(corpus[10][0])
        self.assertEqual((entry[1], entry[2]), ([b"OKR"], ["目标与关键成果".encode()]))
        self.assertEqual(top[7], [1])

    def test_start_session_without_corpus(self):
        params = fields(fields(build_start_session(AstConfig(), "s"))[6][0])
        self.assertNotIn(100, params)

    def test_corpus_limit(self):
        with self.assertRaises(AstError):
            build_start_session(AstConfig(hot_words=["w"] * 1001), "s")

    def test_audio_and_finish(self):
        top = fields(build_audio("s", b"\x01\x02"))
        self.assertEqual(top[2], [Event.TASK_REQUEST])
        self.assertEqual(fields(top[4][0])[14], [b"\x01\x02"])
        self.assertEqual(fields(build_finish_session("s"))[2], [Event.FINISH_SESSION])

    def test_parse_response(self):
        resp = parse_response(response(Event.TRANSLATION_END, "下午好，", 20, 660))
        self.assertEqual((resp.event, resp.text, resp.start_ms, resp.end_ms), (Event.TRANSLATION_END, "下午好，", 20, 660))
        self.assertEqual((resp.status_code, resp.message, resp.session_id), (20000000, "OK", "sid"))

    def test_parse_negative_and_unknown_fields(self):
        data = _int(2, Event.AUDIO_MUTED) + _int(8, 2000) + _int(5, -1) + _str(99, "future field")
        resp = parse_response(data)
        self.assertEqual((resp.event, resp.muted_ms, resp.start_ms), (Event.AUDIO_MUTED, 2000, -1))

    def test_truncated(self):
        with self.assertRaises(ValueError):
            list(decode_fields(_str(4, "hello")[:-2]))


class InterpretTest(unittest.IsolatedAsyncioTestCase):
    """用本地假服务端走一遍会话：SessionStarted 之后才发音频，FinishSession 后结束。"""

    async def asyncSetUp(self):
        self.received = []
        self.headers = {}
        self.fail = False

        async def handler(request):
            self.headers = dict(request.headers)
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for msg in ws:
                if msg.type != WSMsgType.BINARY:
                    continue
                event = fields(msg.data)[2][0]
                self.received.append(event)
                if event == Event.START_SESSION:
                    if self.fail:
                        await ws.send_bytes(response(Event.SESSION_FAILED, status=45000001, message="bad"))
                        continue
                    await ws.send_bytes(response(Event.SESSION_STARTED))
                elif event == Event.FINISH_SESSION:
                    await ws.send_bytes(response(Event.TRANSLATION_END, "你好。"))
                    await ws.send_bytes(response(Event.SESSION_FINISHED))
            return ws

        app = web.Application()
        app.router.add_get("/ast", handler)
        self.server = TestServer(app)
        await self.server.start_server()
        self.session = aiohttp.ClientSession()
        self.cfg = AstConfig(api_key="key-1", url=str(self.server.make_url("/ast")))

    async def asyncTearDown(self):
        await self.session.close()
        await self.server.close()

    async def test_session_flow(self):
        queue = asyncio.Queue()
        for _ in range(5):
            queue.put_nowait(bytes(1600))  # 共 8000 字节 = 3 个 80ms 包 + 尾包
        queue.put_nowait(None)
        events = [r.event async for r in interpret(self.session, self.cfg, queue)]
        self.assertEqual(events, [Event.SESSION_STARTED, Event.TRANSLATION_END, Event.SESSION_FINISHED])
        self.assertEqual(self.received[0], Event.START_SESSION)
        self.assertEqual(self.received[-1], Event.FINISH_SESSION)
        self.assertEqual(self.received.count(Event.TASK_REQUEST), 4)
        self.assertEqual(self.headers["X-Api-Key"], "key-1")
        self.assertEqual(self.headers["X-Api-Resource-Id"], "volc.service_type.10053")

    async def test_session_failed(self):
        self.fail = True
        queue = asyncio.Queue()
        with self.assertRaisesRegex(AstError, "45000001"):
            [r async for r in interpret(self.session, self.cfg, queue)]


if __name__ == "__main__":
    unittest.main()
