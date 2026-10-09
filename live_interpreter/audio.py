"""音频输入：麦克风（sounddevice）或音频文件（ffmpeg 解码，按实时速度回放）。

输出统一为 16kHz、16 bit、单声道 PCM，按约 100ms 一块放进 asyncio 队列，
队列里的 None 表示音频结束。
"""

from __future__ import annotations

import array
import asyncio
import logging
import shutil
from typing import Any, Callable

from .volc_ast import BYTES_PER_SECOND, SAMPLE_RATE

log = logging.getLogger(__name__)

BLOCK_FRAMES = SAMPLE_RATE // 10  # 100ms
BLOCK_BYTES = BLOCK_FRAMES * 2

LevelCallback = Callable[[float], None]


def peak_level(pcm: bytes) -> float:
    """返回 0~1 的峰值电平，用于显示麦克风是否有声音。"""
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    if not samples:
        return 0.0
    return max(abs(min(samples)), max(samples)) / 32768


def put_dropping_oldest(queue: asyncio.Queue[bytes | None], item: bytes | None) -> None:
    if queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
    queue.put_nowait(item)


def _sounddevice() -> Any:
    try:
        import sounddevice
    except (ImportError, OSError) as exc:
        raise RuntimeError("无法加载 sounddevice，请先执行 python3 -m pip install -r requirements.txt") from exc
    return sounddevice


def list_input_devices() -> list[dict[str, Any]]:
    sd = _sounddevice()
    default_input = sd.default.device[0]
    devices = []
    for index, info in enumerate(sd.query_devices()):
        if info["max_input_channels"] > 0:
            devices.append({
                "index": index,
                "name": info["name"],
                "channels": info["max_input_channels"],
                "default": index == default_input,
            })
    return devices


def resolve_device(spec: str | None) -> int | None:
    """设备可以写序号，也可以写名称里的一段文字（不区分大小写）。"""
    if spec is None or spec == "":
        return None
    if spec.isdigit():
        return int(spec)
    matches = [d for d in list_input_devices() if spec.lower() in d["name"].lower()]
    if not matches:
        raise RuntimeError(f"找不到名称包含「{spec}」的输入设备，可用 devices 命令查看")
    if len(matches) > 1:
        names = "、".join(d["name"] for d in matches)
        raise RuntimeError(f"「{spec}」匹配到多个设备：{names}，请写得更具体或改用序号")
    return matches[0]["index"]


class MicrophoneSource:
    def __init__(self, queue: asyncio.Queue[bytes | None], device: int | None = None,
                 on_level: LevelCallback | None = None) -> None:
        self._queue = queue
        self._device = device
        self._on_level = on_level
        self._stream: Any = None

    def start(self) -> str:
        sd = _sounddevice()
        loop = asyncio.get_running_loop()

        def deliver(data: bytes) -> None:
            put_dropping_oldest(self._queue, data)
            if self._on_level is not None:
                self._on_level(peak_level(data))

        def callback(indata: Any, frames: int, time_info: Any, status: Any) -> None:
            if status:
                log.debug("音频状态：%s", status)
            loop.call_soon_threadsafe(deliver, bytes(indata))

        self._stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=BLOCK_FRAMES,
            device=self._device,
            callback=callback,
        )
        self._stream.start()
        info = sd.query_devices(self._stream.device)
        return info["name"]

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


async def play_file(path: str, queue: asyncio.Queue[bytes | None], realtime: bool = True,
                    on_level: LevelCallback | None = None) -> None:
    """把音频文件解码后按实时速度送入队列，结尾补 1.5 秒静音让最后一句定稿。"""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("播放音频文件需要 ffmpeg")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-v", "error", "-i", path,
        "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-",
        stdout=asyncio.subprocess.PIPE,
    )
    assert proc.stdout is not None
    loop = asyncio.get_running_loop()
    started = loop.time()
    sent = 0
    try:
        while True:
            chunk = await proc.stdout.read(BLOCK_BYTES)
            if not chunk:
                break
            await queue.put(chunk)
            if on_level is not None:
                on_level(peak_level(chunk))
            sent += len(chunk)
            if realtime:
                delay = started + sent / BYTES_PER_SECOND - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
        for _ in range(15):
            await queue.put(bytes(BLOCK_BYTES))
            if realtime:
                await asyncio.sleep(0.1)
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
    await queue.put(None)
