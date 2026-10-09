import json
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from live_interpreter.board import SubtitleBoard
from live_interpreter.config import load_config, load_dotenv
from live_interpreter.glossary import Term, parse_glossary, split_corpus
from live_interpreter.server import create_app


class ServerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.board = SubtitleBoard()
        self.client = TestClient(TestServer(create_app(self.board)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_index_page(self):
        resp = await self.client.get("/")
        self.assertEqual(resp.status, 200)
        text = await resp.text()
        self.assertIn("实时字幕 · AI 同传", text)
        self.assertNotIn("{{BRAND}}", text)

    async def test_websocket_snapshot_then_updates(self):
        self.board.upsert(1, "Hi.", "你好。", True)
        ws = await self.client.ws_connect("/ws")
        snapshot = json.loads((await ws.receive()).data)
        self.assertEqual(snapshot["type"], "snapshot")
        self.assertEqual(snapshot["segments"][0]["zh"], "你好。")

        self.board.upsert(2, "Bye.", "再见。", False)
        update = json.loads((await ws.receive()).data)
        self.assertEqual(update, {"type": "segment", "id": 2, "en": "Bye.", "zh": "再见。", "final": False})
        await ws.close()

    async def test_clear_from_loopback(self):
        self.board.upsert(1, "Hi.", "你好。", True)
        resp = await self.client.post("/api/clear")
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.board.snapshot()["segments"], [])


class BoardTest(unittest.TestCase):
    def test_keeps_recent_segments_in_order(self):
        board = SubtitleBoard(keep=3)
        for seg_id in [2, 1, 4, 3, 5]:
            board.upsert(seg_id, "", str(seg_id), True)
        self.assertEqual([s["id"] for s in board.snapshot()["segments"]], [3, 4, 5])

    def test_unchanged_upsert_is_not_broadcast(self):
        board = SubtitleBoard()
        events = []
        board.subscribe(events.append)
        board.upsert(1, "a", "b", False)
        board.upsert(1, "a", "b", False)
        self.assertEqual(len(events), 1)


class ConfigTest(unittest.TestCase):
    def test_dotenv_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text('# c\nA=1\nexport B="two words"\nC=\'x=y\'\nbad line\n', encoding="utf-8")
            self.assertEqual(load_dotenv(path), {"A": "1", "B": "two words", "C": "x=y"})

    def test_load_config(self):
        cfg = load_config({
            "VOLC_API_KEY": "key",
            "AST_DENOISE": "1",
            "PORT": "9000",
            "GLOSSARY_FILE": "/tmp/g.txt",
        })
        self.assertEqual(cfg.ast.api_key, "key")
        self.assertTrue(cfg.ast.denoise)
        self.assertEqual(cfg.ast.resource_id, "volc.service_type.10053")
        self.assertEqual((cfg.ast.source_language, cfg.ast.target_language), ("en", "zh"))
        self.assertEqual(cfg.port, 9000)
        self.assertEqual(cfg.glossary_file, Path("/tmp/g.txt"))


class GlossaryTest(unittest.TestCase):
    def test_parse(self):
        terms = parse_glossary("# x\n\nOKR => 目标与关键成果\nKubernetes\n  go-to-market =>  市场进入策略 \n")
        self.assertEqual(terms, [
            Term("OKR", "目标与关键成果"),
            Term("Kubernetes", ""),
            Term("go-to-market", "市场进入策略"),
        ])


    def test_split_corpus(self):
        terms = [Term("OKR", "目标与关键成果"), Term("Kubernetes", ""), Term("OKR", "目标与关键成果")]
        hot_words, glossary = split_corpus(terms)
        self.assertEqual(hot_words, ["OKR", "Kubernetes"])
        self.assertEqual(glossary, [("OKR", "目标与关键成果")])


if __name__ == "__main__":
    unittest.main()
