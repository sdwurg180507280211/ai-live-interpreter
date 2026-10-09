"""术语表：英文术语作为识别热词，带译名的同时作为翻译术语。

每行一个术语，格式：
    English term => 中文译名
    ProductName            （只写英文表示只做识别热词，译文由模型决定）
以 # 开头的行是注释。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Term:
    source: str
    target: str  # 空字符串表示只做热词


def parse_glossary(text: str) -> list[Term]:
    terms: list[Term] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=>" in line:
            source, target = (part.strip() for part in line.split("=>", 1))
        else:
            source, target = line, ""
        if source:
            terms.append(Term(source, target))
    return terms


def load_glossary(path: Path | None) -> list[Term]:
    if path is None or not path.is_file():
        return []
    return parse_glossary(path.read_text(encoding="utf-8"))


def split_corpus(terms: list[Term]) -> tuple[list[str], list[tuple[str, str]]]:
    """拆成同传接口需要的 (热词列表, 术语对照)。"""
    hot_words = list(dict.fromkeys(term.source for term in terms))
    glossary = list(dict.fromkeys((term.source, term.target) for term in terms if term.target))
    return hot_words, glossary
