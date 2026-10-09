"""控制台里能改的设置，保存在数据目录，服务重启后保留。

- settings.json：观众链接口令、字幕显示设置、默认讲话语言
- glossary.txt：术语表（格式见 glossary.py）
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any

DEFAULT_DISPLAY = {"lines": 3, "size": 6.0, "show_en": False}
LINES_RANGE = (1, 8)
SIZE_RANGE = (2.0, 20.0)
MAX_GLOSSARY_BYTES = 200_000


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def clean_display(raw: dict[str, Any]) -> dict[str, Any]:
    """校验并补全显示设置，非法值抛 ValueError。"""
    display = dict(DEFAULT_DISPLAY)
    if "lines" in raw:
        lines = int(raw["lines"])
        if not LINES_RANGE[0] <= lines <= LINES_RANGE[1]:
            raise ValueError(f"行数需在 {LINES_RANGE[0]}~{LINES_RANGE[1]} 之间")
        display["lines"] = lines
    if "size" in raw:
        size = round(float(raw["size"]), 1)
        if not SIZE_RANGE[0] <= size <= SIZE_RANGE[1]:
            raise ValueError(f"字号需在 {SIZE_RANGE[0]:g}~{SIZE_RANGE[1]:g} 之间")
        display["size"] = size
    if "show_en" in raw:
        display["show_en"] = bool(raw["show_en"])
    return display


class SettingsStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self._settings_path = data_dir / "settings.json"
        self.glossary_path = data_dir / "glossary.txt"
        data: dict[str, Any] = {}
        if self._settings_path.is_file():
            data = json.loads(self._settings_path.read_text(encoding="utf-8"))
        self.view_token: str = data.get("view_token") or secrets.token_urlsafe(12)
        self.display: dict[str, Any] = clean_display(data.get("display") or {})
        self.source_language: str = data.get("source_language") or "en"
        if not self._settings_path.is_file():
            self._save()

    def _save(self) -> None:
        data = {"view_token": self.view_token, "display": self.display, "source_language": self.source_language}
        _atomic_write(self._settings_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    def reset_view_token(self) -> str:
        self.view_token = secrets.token_urlsafe(12)
        self._save()
        return self.view_token

    def set_display(self, raw: dict[str, Any]) -> dict[str, Any]:
        self.display = clean_display({**self.display, **raw})
        self._save()
        return self.display

    def set_source_language(self, language: str) -> None:
        if language != self.source_language:
            self.source_language = language
            self._save()

    def read_glossary(self) -> str:
        if self.glossary_path.is_file():
            return self.glossary_path.read_text(encoding="utf-8")
        return ""

    def write_glossary(self, text: str) -> None:
        if len(text.encode("utf-8")) > MAX_GLOSSARY_BYTES:
            raise ValueError("术语表太大")
        _atomic_write(self.glossary_path, text)
