"""把同传服务的字幕事件变成字幕段：译文逐字上屏，结束后定稿并写入会议记录。"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from .board import SubtitleBoard
from .volc_ast import AstResponse, Event

log = logging.getLogger(__name__)


class TranscriptWriter:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._file = path.open("a", encoding="utf-8")

    def write(self, en: str, zh: str) -> None:
        record = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "en": en, "zh": zh}
        self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()


class Subtitler:
    """一句译文对应一个字幕段；原文按开始时间和译文配对，供调试显示和会议记录使用。"""

    def __init__(self, board: SubtitleBoard, transcript: TranscriptWriter | None = None) -> None:
        self._board = board
        self.transcript = transcript
        self._next_id = 1
        self._sources: dict[int, str] = {}
        self._source_text = ""
        self._seg_id: int | None = None
        self._seg_en = ""
        self._seg_zh = ""

    def reset_session(self) -> None:
        """会话重连后时间戳归零：未完成的译文按已有内容定稿。"""
        if self._seg_id is not None:
            if self._seg_zh.strip():
                self._board.upsert(self._seg_id, self._seg_en, self._seg_zh.strip(), True)
            else:
                self._board.remove(self._seg_id)
        self._seg_id = None
        self._sources.clear()
        self._source_text = ""
        self._board.set_listening(False)

    def handle(self, resp: AstResponse) -> None:
        event = resp.event
        if event == Event.SOURCE_START:
            self._source_text = ""
            self._board.set_listening(True)
        elif event == Event.SOURCE_RESPONSE:
            self._source_text += resp.text
        elif event == Event.SOURCE_END:
            self._sources[resp.start_ms] = (resp.text or self._source_text).strip()
            # 只保留最近几句，避免长会议里无限增长。
            while len(self._sources) > 20:
                self._sources.pop(next(iter(self._sources)))
        elif event == Event.TRANSLATION_START:
            self._seg_id = self._next_id
            self._next_id += 1
            self._seg_en = self._sources.pop(resp.start_ms, self._source_text.strip())
            self._seg_zh = ""
        elif event == Event.TRANSLATION_RESPONSE and self._seg_id is not None:
            self._seg_zh += resp.text
            if self._seg_zh.strip():
                self._board.upsert(self._seg_id, self._seg_en, self._seg_zh.strip(), False)
        elif event == Event.TRANSLATION_END and self._seg_id is not None:
            zh = (resp.text or self._seg_zh).strip()
            if zh:
                self._board.upsert(self._seg_id, self._seg_en, zh, True)
                log.info("%s\n    → %s", self._seg_en, zh)
                if self.transcript is not None:
                    self.transcript.write(self._seg_en, zh)
            else:
                self._board.remove(self._seg_id)
            self._seg_id = None
            self._board.set_listening(False)
        elif event == Event.AUDIO_MUTED:
            self._board.set_listening(False)
