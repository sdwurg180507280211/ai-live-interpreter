import json
import tempfile
import unittest
from pathlib import Path

from live_interpreter.board import SubtitleBoard
from live_interpreter.pipeline import Subtitler, TranscriptWriter
from live_interpreter.volc_ast import AstResponse, Event


def ev(event, text="", start=0, end=0):
    return AstResponse(event=event, text=text, start_ms=start, end_ms=end)


def sentence(en, zh_pieces, start, end):
    yield ev(Event.SOURCE_START, start=start, end=end)
    for word in en.split(" "):
        yield ev(Event.SOURCE_RESPONSE, word + " ")
    yield ev(Event.SOURCE_END, en + " ", start, end)
    yield ev(Event.TRANSLATION_START, start=start, end=end)
    for piece in zh_pieces:
        yield ev(Event.TRANSLATION_RESPONSE, piece)
    yield ev(Event.TRANSLATION_END, "".join(zh_pieces), start, end)


class SubtitlerTest(unittest.TestCase):
    def setUp(self):
        self.board = SubtitleBoard()
        self.events = []
        self.board.subscribe(self.events.append)

    def feed(self, subtitler, items):
        for item in items:
            subtitler.handle(item)

    def segments(self):
        return self.board.snapshot()["segments"]

    def test_translation_streams_then_finalizes(self):
        sub = Subtitler(self.board)
        self.feed(sub, sentence("Good afternoon,", ["下午", "好，"], 20, 660))
        updates = [(e["zh"], e["final"]) for e in self.events if e["type"] == "segment"]
        self.assertEqual(updates, [("下午", False), ("下午好，", False), ("下午好，", True)])
        self.assertEqual(self.segments(), [{"id": 1, "en": "Good afternoon,", "zh": "下午好，", "final": True}])

    def test_sentences_get_increasing_ids(self):
        sub = Subtitler(self.board)
        self.feed(sub, sentence("Good afternoon,", ["下午好，"], 20, 660))
        self.feed(sub, sentence("everyone.", ["大家。"], 660, 1140))
        self.assertEqual([(s["id"], s["en"], s["zh"]) for s in self.segments()],
                         [(1, "Good afternoon,", "下午好，"), (2, "everyone.", "大家。")])

    def test_listening_indicator(self):
        sub = Subtitler(self.board)
        sub.handle(ev(Event.SOURCE_START, start=0))
        self.assertTrue(self.board.snapshot()["listening"])
        self.feed(sub, list(sentence("Hi.", ["你好。"], 0, 400))[1:])
        self.assertFalse(self.board.snapshot()["listening"])
        sub.handle(ev(Event.SOURCE_START, start=500))
        sub.handle(ev(Event.AUDIO_MUTED))
        self.assertFalse(self.board.snapshot()["listening"])

    def test_empty_translation_is_removed(self):
        sub = Subtitler(self.board)
        self.feed(sub, sentence("Um.", [], 0, 300))
        self.assertEqual(self.segments(), [])

    def test_reset_session_keeps_partial_translation(self):
        sub = Subtitler(self.board)
        self.feed(sub, list(sentence("So the plan", ["所以", "计划"], 0, 900))[:-1])
        sub.reset_session()
        self.assertEqual(self.segments(), [{"id": 1, "en": "So the plan", "zh": "所以计划", "final": True}])
        # 重连后时间戳从 0 开始，新句子仍然拿到新的 id。
        self.feed(sub, sentence("Next.", ["下一个。"], 0, 400))
        self.assertEqual([s["id"] for s in self.segments()], [1, 2])

    def test_reset_session_drops_empty_segment(self):
        sub = Subtitler(self.board)
        self.feed(sub, list(sentence("Hmm", [], 0, 300))[:-1])
        sub.reset_session()
        self.assertEqual(self.segments(), [])

    def test_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = TranscriptWriter(Path(tmp) / "t.jsonl")
            sub = Subtitler(self.board, writer)
            self.feed(sub, sentence("Hello.", ["你好。"], 0, 500))
            writer.close()
            record = json.loads(writer.path.read_text(encoding="utf-8"))
            self.assertEqual((record["en"], record["zh"]), ("Hello.", "你好。"))


if __name__ == "__main__":
    unittest.main()
