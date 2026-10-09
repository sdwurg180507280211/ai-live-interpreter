"""字幕板：保存最近的字幕段，并把变化推送给所有显示端。"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any, Callable

Listener = Callable[[dict[str, Any]], None]


class SubtitleBoard:
    def __init__(self, keep: int = 30, level_interval_s: float = 0.25) -> None:
        self._keep = keep
        self._segments: OrderedDict[int, dict[str, Any]] = OrderedDict()
        self._listeners: set[Listener] = set()
        self._listening = False
        self._status: dict[str, str] = {"state": "starting", "detail": ""}
        self._level_interval_s = level_interval_s
        self._last_level_at = 0.0
        # 字幕屏的显示设置（行数、字号、是否显示英文），由控制台统一下发。
        self._display: dict[str, Any] = {}

    def subscribe(self, listener: Listener) -> None:
        self._listeners.add(listener)

    def unsubscribe(self, listener: Listener) -> None:
        self._listeners.discard(listener)

    def _emit(self, message: dict[str, Any]) -> None:
        for listener in list(self._listeners):
            listener(message)

    def snapshot(self) -> dict[str, Any]:
        return {
            "type": "snapshot",
            "segments": list(self._segments.values()),
            "listening": self._listening,
            "status": dict(self._status),
            "display": dict(self._display),
        }

    def get(self, seg_id: int) -> dict[str, Any] | None:
        return self._segments.get(seg_id)

    def upsert(self, seg_id: int, en: str, zh: str, final: bool) -> None:
        segment = {"id": seg_id, "en": en, "zh": zh, "final": final}
        if self._segments.get(seg_id) == segment:
            return
        self._segments[seg_id] = segment
        self._segments = OrderedDict(sorted(self._segments.items()))
        while len(self._segments) > self._keep:
            self._segments.popitem(last=False)
        self._emit({"type": "segment", **segment})

    def remove(self, seg_id: int) -> None:
        if self._segments.pop(seg_id, None) is not None:
            self._emit({"type": "remove", "id": seg_id})

    def clear(self) -> None:
        self._segments.clear()
        self._emit({"type": "clear"})

    def set_listening(self, listening: bool) -> None:
        if listening != self._listening:
            self._listening = listening
            self._emit({"type": "listening", "active": listening})

    def set_status(self, state: str, detail: str = "") -> None:
        status = {"state": state, "detail": detail}
        if status != self._status:
            self._status = status
            self._emit({"type": "status", **status})

    def set_display(self, display: dict[str, Any]) -> None:
        if display != self._display:
            self._display = dict(display)
            self._emit({"type": "display", **self._display})

    def set_level(self, level: float) -> None:
        now = time.monotonic()
        if now - self._last_level_at >= self._level_interval_s:
            self._last_level_at = now
            self._emit({"type": "level", "value": round(level, 3)})
