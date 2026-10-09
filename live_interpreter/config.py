"""从环境变量和项目根目录的 .env 读取配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_AST_URL = "wss://openspeech.bytedance.com/api/v4/ast/v2/translate"
DEFAULT_AST_RESOURCE_ID = "volc.service_type.10053"


def load_dotenv(path: Path) -> dict[str, str]:
    """解析简单的 KEY=VALUE 文件；已存在的环境变量优先。"""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def _env_bool(env: dict[str, str], key: str, default: bool) -> bool:
    value = env.get(key)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(env: dict[str, str], key: str, default: int) -> int:
    value = env.get(key)
    if value is None or value == "":
        return default
    return int(value)


@dataclass
class AstConfig:
    api_key: str = ""
    resource_id: str = DEFAULT_AST_RESOURCE_ID
    url: str = DEFAULT_AST_URL
    source_language: str = "en"
    target_language: str = "zh"
    denoise: bool = False
    hot_words: list[str] = field(default_factory=list)
    glossary: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class AppConfig:
    ast: AstConfig
    host: str = "127.0.0.1"
    port: int = 8765
    glossary_file: Path | None = None
    transcript_dir: Path = PROJECT_ROOT / "transcripts"
    # 云端模式：控制台登录密码、数据目录（设置、术语表、会议记录）、对外网址
    admin_password: str = ""
    data_dir: Path = PROJECT_ROOT / "data"
    public_url: str = ""
    # 页面上显示的产品名称
    brand_name: str = "AI 同传"


def load_config(env: dict[str, str] | None = None, dotenv: Path | None = None) -> AppConfig:
    if env is None:
        merged = load_dotenv(dotenv or PROJECT_ROOT / ".env")
        merged.update(os.environ)
        env = merged

    ast = AstConfig(
        api_key=env.get("VOLC_API_KEY", ""),
        resource_id=env.get("VOLC_AST_RESOURCE_ID") or DEFAULT_AST_RESOURCE_ID,
        url=env.get("VOLC_AST_URL") or DEFAULT_AST_URL,
        source_language=env.get("SOURCE_LANGUAGE") or AstConfig.source_language,
        target_language=env.get("TARGET_LANGUAGE") or AstConfig.target_language,
        denoise=_env_bool(env, "AST_DENOISE", False),
    )
    glossary = env.get("GLOSSARY_FILE", "")
    glossary_path: Path | None
    if glossary:
        glossary_path = Path(glossary).expanduser()
        if not glossary_path.is_absolute():
            glossary_path = PROJECT_ROOT / glossary_path
    else:
        default = PROJECT_ROOT / "glossary.txt"
        glossary_path = default if default.is_file() else None

    return AppConfig(
        ast=ast,
        host=env.get("HOST") or AppConfig.host,
        port=_env_int(env, "PORT", AppConfig.port),
        glossary_file=glossary_path,
        transcript_dir=Path(env["TRANSCRIPT_DIR"]).expanduser() if env.get("TRANSCRIPT_DIR") else AppConfig.transcript_dir,
        admin_password=env.get("ADMIN_PASSWORD", ""),
        data_dir=Path(env["DATA_DIR"]).expanduser() if env.get("DATA_DIR") else AppConfig.data_dir,
        public_url=env.get("PUBLIC_URL", ""),
        brand_name=env.get("BRAND_NAME") or AppConfig.brand_name,
    )
