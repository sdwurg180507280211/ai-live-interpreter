"""云端模式：接收浏览器收音页上传的音频，转给同传会话。

同一时间只有一个收音端。新的收音端开始时接管旧的，旧收音端会收到 replaced 通知。
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from .audio import peak_level, put_dropping_oldest
from .board import SubtitleBoard
from .config import AstConfig
from .pipeline import Subtitler, TranscriptWriter

log = logging.getLogger(__name__)

# 同传支持作为源语言的语种（语音转文字模式）。
SOURCE_LANGUAGES = {
    "en", "ja", "ko", "fr", "de", "es", "pt", "it", "ru", "ar", "tr", "ms", "id",
    "vi", "th", "nl", "ro", "pl", "cs", "zh", "yue-CN", "sh-CN",
}

Owner = Callable[[dict[str, Any]], None]
# 每次开始同传时读取最新的 (热词, 术语对照)，控制台改了术语表下次开始就生效。
CorpusProvider = Callable[[], "tuple[list[str], list[tuple[str, str]]]"]
SessionRunner = Callable[[AstConfig, "asyncio.Queue[bytes | None]", Callable[[], bool]], Awaitable[None]]


class CaptureHub:
    def __init__(
        self,
        ast: AstConfig,
        board: SubtitleBoard,
        subtitler: Subtitler,
        runner: SessionRunner,
        transcript_dir: Path,
        corpus: CorpusProvider | None = None,
    ) -> None:
        self._ast = ast
        self._board = board
        self._subtitler = subtitler
        self._runner = runner
        self._transcript_dir = transcript_dir
        self._corpus = corpus
        self._owner: Owner | None = None
        self._queue: asyncio.Queue[bytes | None] | None = None
        self._task: asyncio.Task[None] | None = None
        self._finishing = False
        self._transcript: TranscriptWriter | None = None
        self.source_language = ast.source_language

    @property
    def owner(self) -> Owner | None:
        return self._owner

    async def start(self, owner: Owner, source_language: str) -> None:
        if source_language not in SOURCE_LANGUAGES:
            raise ValueError(f"不支持的源语言：{source_language}")
        previous = self._owner
        await self._shutdown()
        if previous is not None and previous is not owner:
            previous({"type": "capture", "state": "replaced"})

        self._owner = owner
        self.source_language = source_language
        self._finishing = False
        self._queue = asyncio.Queue(maxsize=100)  # 约 10 秒缓存，重连期间不丢音频
        self._transcript = TranscriptWriter(self._transcript_dir / f"{time.strftime('%Y%m%d-%H%M%S')}.jsonl")
        self._subtitler.transcript = self._transcript
        ast = dataclasses.replace(self._ast, source_language=source_language)
        if self._corpus is not None:
            ast.hot_words, ast.glossary = self._corpus()
        self._board.set_status("starting")
        self._task = asyncio.create_task(self._runner(ast, self._queue, lambda: self._finishing))
        self._task.add_done_callback(self._on_done)
        log.info("收音端开始，源语言 %s，会议记录 %s", source_language, self._transcript.path)

    def feed(self, owner: Owner, pcm: bytes) -> None:
        if owner is not self._owner or self._queue is None or self._finishing:
            return
        put_dropping_oldest(self._queue, pcm)
        self._board.set_level(peak_level(pcm))

    async def stop(self, owner: Owner) -> None:
        """收音端主动停止或断开。只有当前收音端能停止会话。"""
        if owner is not self._owner:
            return
        self._owner = None
        await self._shutdown()
        log.info("收音端停止")

    async def close(self) -> None:
        self._owner = None
        await self._shutdown()

    async def _shutdown(self) -> None:
        task, queue = self._task, self._queue
        self._task = None
        self._queue = None
        if task is not None and not task.done():
            # 先正常结束会话，让最后一句译文回来；等不到就直接取消。
            self._finishing = True
            if queue is not None:
                put_dropping_oldest(queue, None)
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            except (asyncio.TimeoutError, Exception):
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._subtitler.reset_session()
        self._subtitler.transcript = None
        if self._transcript is not None:
            self._transcript.close()
            self._transcript = None
        self._board.set_status("idle")

    def _on_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("同传会话异常结束：%s", exc)
            self._board.set_status("error", f"同传会话异常结束：{exc}")
