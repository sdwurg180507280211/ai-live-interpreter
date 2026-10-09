"""命令行入口：run / devices / check。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import socket
import sys
import time
import webbrowser
from urllib.parse import urlsplit
from typing import Callable

import aiohttp
from aiohttp import web

from .audio import MicrophoneSource, list_input_devices, play_file, resolve_device
from .board import SubtitleBoard
from .capture import CaptureHub
from .config import AppConfig, AstConfig, load_config
from .glossary import load_glossary, parse_glossary, split_corpus
from .auth import Auth
from .pipeline import Subtitler, TranscriptWriter
from .settings import SettingsStore
from .server import create_app
from .volc_ast import AstError, Event, interpret

log = logging.getLogger("live_interpreter")


def lan_addresses() -> list[str]:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))  # 不会真的发包，只用来选出本机出口地址
            return [sock.getsockname()[0]]
    except OSError:
        return []


def prepare(cfg: AppConfig) -> None:
    if not cfg.ast.api_key:
        sys.exit("缺少 VOLC_API_KEY，请参考 .env.example 创建 .env")
    terms = load_glossary(cfg.glossary_file)
    cfg.ast.hot_words, cfg.ast.glossary = split_corpus(terms)
    if terms:
        log.info("已加载术语表 %s（%d 条）", cfg.glossary_file, len(terms))


async def run_session_loop(
    session: aiohttp.ClientSession,
    ast: AstConfig,
    queue: asyncio.Queue[bytes | None],
    subtitler: Subtitler,
    board: SubtitleBoard,
    source_finished: Callable[[], bool],
) -> None:
    backoff = 1.0
    while True:
        subtitler.reset_session()
        try:
            async for resp in interpret(session, ast, queue):
                if resp.event == Event.SESSION_STARTED:
                    board.set_status("ok")
                    backoff = 1.0
                subtitler.handle(resp)
            if source_finished():
                return
            log.warning("同传会话提前结束，重新连接")
        except (AstError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.error("%s；%.0f 秒后重连", exc, backoff)
            board.set_status("reconnecting", str(exc))
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 10.0)


async def run(args: argparse.Namespace, cfg: AppConfig) -> None:
    prepare(cfg)
    board = SubtitleBoard()
    # 最多缓存约 10 秒音频：连接或重连期间的讲话不会丢，连上后补发。
    queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=100)
    transcript = TranscriptWriter(cfg.transcript_dir / f"{time.strftime('%Y%m%d-%H%M%S')}.jsonl")
    subtitler = Subtitler(board, transcript)

    async with aiohttp.ClientSession() as session:
        runner = web.AppRunner(create_app(board, brand=cfg.brand_name), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, cfg.host, cfg.port).start()
        local_url = f"http://127.0.0.1:{cfg.port}/"
        print(f"\n字幕页面：{local_url}")
        if cfg.host in ("0.0.0.0", "::"):
            for address in lan_addresses():
                print(f"局域网访问：http://{address}:{cfg.port}/")
        print(f"调试页面：{local_url}?debug=1&en=1")
        print(f"会议记录：{transcript.path}\n")
        if args.open:
            webbrowser.open(local_url)

        mic: MicrophoneSource | None = None
        source_task: asyncio.Task[None] | None = None
        try:
            if args.file:
                source_task = asyncio.create_task(
                    play_file(args.file, queue, realtime=not args.fast, on_level=board.set_level))
                finished = source_task.done
                log.info("正在播放音频文件 %s", args.file)
            else:
                mic = MicrophoneSource(queue, resolve_device(args.device), on_level=board.set_level)
                log.info("正在收音：%s", mic.start())
                finished = lambda: False  # noqa: E731
            await run_session_loop(session, cfg.ast, queue, subtitler, board, finished)
            board.set_listening(False)
            print("\n音频播放结束，字幕页面保持打开，按 Ctrl+C 退出。")
            await asyncio.Event().wait()
        finally:
            if mic is not None:
                mic.stop()
            if source_task is not None:
                source_task.cancel()
            await runner.cleanup()
            transcript.close()


async def check(args: argparse.Namespace, cfg: AppConfig) -> None:
    """会前自检：同传服务能否建立会话。"""
    prepare(cfg)
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    for _ in range(10):
        queue.put_nowait(bytes(3200))
    queue.put_nowait(None)
    started = time.monotonic()
    async with aiohttp.ClientSession() as session:
        try:
            async for resp in interpret(session, cfg.ast, queue):
                if resp.event == Event.SESSION_STARTED:
                    print(f"✓ 同传服务正常（建立会话 {time.monotonic() - started:.1f} 秒）")
        except (AstError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            sys.exit(f"✗ 同传服务：{exc}")


async def serve(args: argparse.Namespace, cfg: AppConfig) -> None:
    """云端模式：登录后在控制台收音，观众用分享链接看字幕。"""
    if not cfg.ast.api_key:
        sys.exit("缺少 VOLC_API_KEY，请参考 .env.example 创建 .env")
    if len(cfg.admin_password) < 6:
        sys.exit("云端模式需要在 .env 里设置 ADMIN_PASSWORD（至少 6 位）")
    store = SettingsStore(cfg.data_dir)
    transcript_dir = cfg.data_dir / "transcripts"
    cookie_path = urlsplit(cfg.public_url).path.rstrip("/") + "/" if cfg.public_url else "/"
    auth = Auth(cfg.admin_password, cfg.data_dir, cookie_path=cookie_path)
    board = SubtitleBoard()
    board.set_status("idle")
    subtitler = Subtitler(board)

    def corpus() -> tuple[list[str], list[tuple[str, str]]]:
        return split_corpus(parse_glossary(store.read_glossary()))

    async with aiohttp.ClientSession() as session:
        async def runner(ast: AstConfig, queue: asyncio.Queue[bytes | None], finished: Callable[[], bool]) -> None:
            await run_session_loop(session, ast, queue, subtitler, board, finished)

        hub = CaptureHub(cfg.ast, board, subtitler, runner, transcript_dir, corpus=corpus)
        app = create_app(board, hub, auth=auth, settings=store, transcript_dir=transcript_dir,
                         brand=cfg.brand_name)
        web_runner = web.AppRunner(app, access_log=None)
        await web_runner.setup()
        await web.TCPSite(web_runner, cfg.host, cfg.port).start()
        base = (cfg.public_url or f"http://{cfg.host}:{cfg.port}").rstrip("/")
        print(f"\n控制台：{base}/（用 ADMIN_PASSWORD 登录）\n")
        try:
            await asyncio.Event().wait()
        finally:
            await hub.close()
            await web_runner.cleanup()


def print_devices() -> None:
    for device in list_input_devices():
        mark = "  ← 默认" if device["default"] else ""
        print(f"[{device['index']}] {device['name']}（{device['channels']} 声道）{mark}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="live_interpreter", description="英译中 AI 同传字幕")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="开始同传")
    run_parser.add_argument("--device", help="输入设备序号或名称片段，默认用系统默认麦克风")
    run_parser.add_argument("--file", help="用音频文件代替麦克风（测试用）")
    run_parser.add_argument("--fast", action="store_true", help="音频文件不按实时速度播放")
    run_parser.add_argument("--host", help="监听地址，平板等设备访问请用 0.0.0.0")
    run_parser.add_argument("--port", type=int, help="网页端口，默认 8765")
    run_parser.add_argument("--open", action="store_true", help="启动后打开浏览器")

    serve_parser = sub.add_parser("serve", help="云端模式：网页登录控制台收音，观众用链接看字幕")
    serve_parser.add_argument("--host", help="监听地址，默认 127.0.0.1（由反向代理对外提供 HTTPS）")
    serve_parser.add_argument("--port", type=int, help="端口，默认 8765")

    sub.add_parser("check", help="检查同传服务配置")
    sub.add_parser("devices", help="列出麦克风设备")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    if args.command == "devices":
        print_devices()
        return
    cfg = load_config()
    if getattr(args, "host", None):
        cfg.host = args.host
    if getattr(args, "port", None):
        cfg.port = args.port
    try:
        commands = {"run": run, "serve": serve, "check": check}
        asyncio.run(commands[args.command](args, cfg))
    except KeyboardInterrupt:
        pass
    except RuntimeError as exc:
        sys.exit(str(exc))
