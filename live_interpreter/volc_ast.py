"""火山引擎豆包同声传译 2.0（AST）WebSocket 客户端。

协议文档：https://www.volcengine.com/docs/6561/1756902
每条 WebSocket 二进制消息是一个 protobuf 消息（TranslateRequest / TranslateResponse），
这里只实现用到的字段，避免依赖 protobuf 运行时和生成代码。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Iterable

import aiohttp

from .config import AstConfig

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
BYTES_PER_SECOND = SAMPLE_RATE * 2  # 16 bit 单声道
# 官方建议 80ms 一包。
PACKET_BYTES = BYTES_PER_SECOND * 80 // 1000

# 热词和术语加起来不能超过 1000 个，否则服务端报错。
MAX_CORPUS_ITEMS = 1000


class Event:
    START_SESSION = 100
    FINISH_SESSION = 102
    SESSION_STARTED = 150
    SESSION_FINISHED = 152
    SESSION_FAILED = 153
    USAGE = 154
    TASK_REQUEST = 200
    UPDATE_CONFIG = 201
    AUDIO_MUTED = 250
    SOURCE_START = 650
    SOURCE_RESPONSE = 651
    SOURCE_END = 652
    TRANSLATION_START = 653
    TRANSLATION_RESPONSE = 654
    TRANSLATION_END = 655


class AstError(RuntimeError):
    def __init__(self, message: str, code: int = 0):
        super().__init__(message)
        self.code = code


# ---------- protobuf 编解码（proto3 wire format） ----------

_VARINT = 0
_I64 = 1
_LEN = 2
_I32 = 5


def _varint(value: int) -> bytes:
    if value < 0:
        value += 1 << 64  # int32/int64 负数按 10 字节补码编码
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _key(field: int, wire_type: int) -> bytes:
    return _varint((field << 3) | wire_type)


def _int(field: int, value: int) -> bytes:
    return _key(field, _VARINT) + _varint(value) if value else b""


def _bool(field: int, value: bool | None) -> bytes:
    return _key(field, _VARINT) + b"\x01" if value else b""


def _bytes(field: int, value: bytes) -> bytes:
    return _key(field, _LEN) + _varint(len(value)) + value if value else b""


def _str(field: int, value: str) -> bytes:
    return _bytes(field, value.encode("utf-8"))


def _msg(field: int, body: bytes, always: bool = False) -> bytes:
    if not body and not always:
        return b""
    return _key(field, _LEN) + _varint(len(body)) + body


def decode_fields(data: bytes) -> Iterable[tuple[int, int, Any]]:
    """逐个产出 (字段号, wire type, 值)。LEN 类型的值是 bytes，由调用方按需再解析。"""
    pos = 0
    end = len(data)

    def read_varint() -> int:
        nonlocal pos
        shift = 0
        result = 0
        while True:
            if pos >= end:
                raise ValueError("protobuf 数据被截断")
            byte = data[pos]
            pos += 1
            result |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return result
            shift += 7

    while pos < end:
        key = read_varint()
        field, wire_type = key >> 3, key & 0x07
        if wire_type == _VARINT:
            yield field, wire_type, read_varint()
        elif wire_type == _LEN:
            size = read_varint()
            if pos + size > end:
                raise ValueError("protobuf 数据被截断")
            yield field, wire_type, data[pos:pos + size]
            pos += size
        elif wire_type == _I64:
            yield field, wire_type, data[pos:pos + 8]
            pos += 8
        elif wire_type == _I32:
            yield field, wire_type, data[pos:pos + 4]
            pos += 4
        else:
            raise ValueError(f"不支持的 protobuf wire type {wire_type}")


def _signed32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - (1 << 32) if value & 0x80000000 else value


# ---------- 请求 ----------

def _audio_format() -> bytes:
    # understanding.Audio：format=4, codec=5, rate=7, bits=8, channel=9
    return _str(4, "wav") + _str(5, "raw") + _int(7, SAMPLE_RATE) + _int(8, 16) + _int(9, 1)


def _corpus(cfg: AstConfig) -> bytes:
    if len(cfg.hot_words) + len(cfg.glossary) > MAX_CORPUS_ITEMS:
        raise AstError(f"热词和术语合计不能超过 {MAX_CORPUS_ITEMS} 个")
    # understanding.Corpus：hot_words_list=9（repeated string），glossary_list=10（map<string,string>）
    body = b"".join(_str(9, word) for word in cfg.hot_words)
    for source, target in cfg.glossary:
        body += _msg(10, _str(1, source) + _str(2, target), always=True)
    return body


def build_start_session(cfg: AstConfig, session_id: str) -> bytes:
    params = (
        _str(1, "s2t")
        + _str(2, cfg.source_language)
        + _str(3, cfg.target_language)
        + _msg(100, _corpus(cfg))
    )
    return (
        _msg(1, _str(6, session_id))           # request_meta.SessionID
        + _int(2, Event.START_SESSION)
        + _msg(3, _str(1, "ai-live-interpreter"))  # user.uid
        + _msg(4, _audio_format())             # source_audio
        + _msg(6, params)                      # request
        + _bool(7, cfg.denoise)
    )


def build_audio(session_id: str, pcm: bytes) -> bytes:
    return (
        _msg(1, _str(6, session_id))
        + _int(2, Event.TASK_REQUEST)
        + _msg(4, _bytes(14, pcm), always=True)  # source_audio.binary_data
    )


def build_finish_session(session_id: str) -> bytes:
    return _msg(1, _str(6, session_id)) + _int(2, Event.FINISH_SESSION)


# ---------- 响应 ----------

@dataclass
class AstResponse:
    event: int = 0
    text: str = ""
    start_ms: int = 0
    end_ms: int = 0
    speaker_changed: bool = False
    muted_ms: int = 0
    status_code: int = 0
    message: str = ""
    session_id: str = ""


def parse_response(data: bytes) -> AstResponse:
    resp = AstResponse()
    for field, wire_type, value in decode_fields(data):
        if field == 1 and wire_type == _LEN:  # response_meta
            for sub, sub_type, sub_value in decode_fields(value):
                if sub == 1 and sub_type == _LEN:
                    resp.session_id = sub_value.decode("utf-8", "replace")
                elif sub == 3 and sub_type == _VARINT:
                    resp.status_code = _signed32(sub_value)
                elif sub == 4 and sub_type == _LEN:
                    resp.message = sub_value.decode("utf-8", "replace")
        elif field == 2 and wire_type == _VARINT:
            resp.event = value
        elif field == 4 and wire_type == _LEN:
            resp.text = value.decode("utf-8", "replace")
        elif field == 5 and wire_type == _VARINT:
            resp.start_ms = _signed32(value)
        elif field == 6 and wire_type == _VARINT:
            resp.end_ms = _signed32(value)
        elif field == 7 and wire_type == _VARINT:
            resp.speaker_changed = bool(value)
        elif field == 8 and wire_type == _VARINT:
            resp.muted_ms = _signed32(value)
    return resp


# ---------- 会话 ----------

def auth_headers(cfg: AstConfig) -> dict[str, str]:
    return {
        "X-Api-Key": cfg.api_key,
        "X-Api-Resource-Id": cfg.resource_id,
        "X-Api-Connect-Id": str(uuid.uuid4()),
    }


async def interpret(
    session: aiohttp.ClientSession,
    cfg: AstConfig,
    audio: asyncio.Queue[bytes | None],
) -> AsyncIterator[AstResponse]:
    """建立一次同传会话，持续发送队列里的音频，逐条产出服务端响应。

    队列里放入 None 表示音频结束；服务端返回 SessionFinished 后迭代器停止。
    """
    try:
        ws = await session.ws_connect(cfg.url, headers=auth_headers(cfg), heartbeat=30, max_msg_size=0)
    except aiohttp.WSServerHandshakeError as exc:
        raise AstError(f"同传服务握手失败 HTTP {exc.status}（检查 API Key 和 Resource Id）") from exc
    logid = ws._response.headers.get("X-Tt-Logid", "") if ws._response is not None else ""
    session_id = str(uuid.uuid4())
    log.info("同传会话已连接 logid=%s", logid)

    async def send_audio() -> None:
        buffer = bytearray()
        while True:
            chunk = await audio.get()
            if chunk is None:
                if buffer:
                    await ws.send_bytes(build_audio(session_id, bytes(buffer)))
                await ws.send_bytes(build_finish_session(session_id))
                return
            buffer.extend(chunk)
            while len(buffer) >= PACKET_BYTES:
                await ws.send_bytes(build_audio(session_id, bytes(buffer[:PACKET_BYTES])))
                del buffer[:PACKET_BYTES]

    sender: asyncio.Task[None] | None = None
    try:
        await ws.send_bytes(build_start_session(cfg, session_id))
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                resp = parse_response(msg.data)
                if resp.event == Event.SESSION_FAILED or resp.status_code not in (0, 20000000):
                    raise AstError(
                        f"同传服务错误 {resp.status_code}：{resp.message}（logid={logid}）", resp.status_code)
                if resp.event == Event.SESSION_STARTED and sender is None:
                    # 文档要求收到 SessionStarted 后再发音频。
                    sender = asyncio.create_task(send_audio())
                yield resp
                if resp.event == Event.SESSION_FINISHED:
                    return
            elif msg.type == aiohttp.WSMsgType.ERROR:
                raise AstError(f"同传连接异常：{ws.exception()}")
        if sender is not None and sender.done() and sender.exception():
            raise sender.exception()  # type: ignore[misc]
        raise AstError(f"同传连接被关闭 code={ws.close_code}（logid={logid}）")
    finally:
        if sender is not None:
            sender.cancel()
            try:
                await sender
            except (asyncio.CancelledError, Exception):
                pass
        await ws.close()
