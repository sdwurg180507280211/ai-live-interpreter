# AI Live Interpreter · 英译中 AI 同传字幕

会议现场英文发言，屏幕实时显示中文字幕。字幕三行滚动，每个短句说完后约 1~1.5 秒出中文。

```text
领夹麦 / 话筒
    ↓ 16kHz PCM，80ms 一包
火山引擎 · 豆包同声传译 2.0（语音直接出译文，自动断句）
    ↓ WebSocket
浏览器字幕页（Mac 全屏投到电视/投影，或同一 Wi-Fi 下的平板）
```

## 功能

- 只显示中文，三行连续滚动，黑底白字，可调字号；全屏后隐藏鼠标。
- 译文逐字上屏（灰色），一句结束后定稿（白色）。
- 术语表 `glossary.txt`：英文术语作为识别热词，写了中文译名的作为翻译术语。
- 同传服务断线自动重连，期间的音频先缓存，连上后补发；字幕页断线也会自动重连。
- 每场会议的中英对照记录自动保存到 `transcripts/`。

## 准备

| 项目 | 要求 |
| --- | --- |
| 系统 | macOS，Python 3.10+ |
| 同传服务 | [豆包语音控制台](https://console.volcengine.com/speech/new) 开通「同声传译」，在「API Key」页面创建 API Key |
| 收音 | 领夹麦或 USB 麦克风，插在运行程序的 Mac 上 |
| 测试音频（可选） | `ffmpeg`，用于 `--file` 播放音频文件 |

```sh
python3 -m pip install -r requirements.txt
cp .env.example .env                    # 填写 VOLC_API_KEY
cp glossary.example.txt glossary.txt    # 可选：术语表
```

首次收音时，macOS 会询问终端（Terminal / iTerm）是否可以使用麦克风，需要允许。

## 使用

```sh
python3 -m live_interpreter check     # 会前自检：同传服务能否建立会话
python3 -m live_interpreter devices   # 查看麦克风序号和名称
python3 -m live_interpreter run --device "USB" --open
```

浏览器打开 `http://127.0.0.1:8765/`，按 `F` 全屏。

| 按键 | 作用 |
| --- | --- |
| `F` | 全屏 / 退出全屏 |
| `+` / `-` | 调整字号（会记住） |
| `C` | 清屏（只在运行程序的电脑上有效） |
| `E` | 显示 / 隐藏英文原文（改为一句一段） |
| `D` | 调试面板：麦克风电平、连接状态 |

网址参数：`?lines=3` 行数，`?size=6` 字号（屏幕高度的百分比），`?en=1` 显示英文，`?debug=1` 调试面板。

### 平板显示

```sh
python3 -m live_interpreter run --host 0.0.0.0
```

终端会打印局域网地址，平板连同一个 Wi-Fi 后用 Safari 打开。平板只能看字幕，不能清屏。

### 没有麦克风时测试

```sh
say -v Samantha -o /tmp/test.aiff "Good afternoon everyone. Thank you for joining us today."
python3 -m live_interpreter run --file /tmp/test.aiff --open
```

## 云端模式（远程服务客户）

程序部署在云服务器上，客户现场只需要浏览器：

```text
会场电脑接领夹麦 → 浏览器登录控制台，点「开始同传」
        ↓ HTTPS 上传音频
云服务器（serve 命令）→ 火山同传 → 推送字幕
        ↓
电视 / 投影 / 平板打开观众链接；你在远程登录控制台，查看和调整
```

**只有一个入口**：`https://<服务器 IP>/interpret/`，用密码登录后进入控制台：

| 区域 | 作用 |
| --- | --- |
| ① 收音 | 选麦克风、「测试麦克风」看音量、选讲话语言、开始/停止同传 |
| 字幕预览 | 最近几句中英对照；清屏 |
| ② 观众字幕 | 复制观众链接（观众不用登录）、在本机打开字幕屏、重置链接（旧链接立即失效） |
| ③ 显示设置 | 行数、字号、是否显示英文，改完所有字幕屏立即生效 |
| ④ 术语表 | 在线编辑，下次开始同传时生效 |
| ⑤ 会议记录 | 每场会议的中英对照，可下载 |

同一时间只有一个收音端；在别的设备点「开始同传」会接管。登录状态保存 30 天；密码连续输错 8 次锁定 15 分钟。

部署或更新（第一次运行会生成登录密码；之后再运行只同步代码，不改动密码、设置和会议记录）：

```sh
cp deploy/server.env.example deploy/server.env   # 首次：填写服务器上 Caddy 的位置
deploy/deploy.sh <ssh 主机> https://<服务器 IP>/interpret
```

脚本把服务挂到服务器上已有 Caddy 站点的一个子路径下（国内服务器上未备案的域名会被拦截，可以直接用 IP 访问）。查看密码：

```sh
ssh <ssh 主机> 'grep ^ADMIN_PASSWORD= /opt/ai-live-interpreter/.env'
```

改密码：编辑服务器 `/opt/ai-live-interpreter/.env` 里的 `ADMIN_PASSWORD`，然后 `systemctl restart ai-live-interpreter`，所有已登录设备会退出。数据（设置、术语表、会议记录）在 `/opt/ai-live-interpreter/data/`，日志用 `journalctl -u ai-live-interpreter -f` 查看。

## 配置

都在 `.env` 里，说明见 `.env.example`。云端模式另外需要 `ADMIN_PASSWORD`、`PUBLIC_URL`、`DATA_DIR`，由部署脚本生成。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `VOLC_API_KEY` | — | 豆包语音控制台的 API Key |
| `SOURCE_LANGUAGE` / `TARGET_LANGUAGE` | `en` / `zh` | 源语言 / 目标语言 |
| `AST_DENOISE` | 0 | 服务端降噪 |
| `HOST` / `PORT` | `127.0.0.1` / 8765 | 字幕网页监听地址 |
| `GLOSSARY_FILE` | `glossary.txt` | 术语表路径 |

### 网络

程序直接连接 `openspeech.bytedance.com`，不读取 `HTTP_PROXY` 等代理变量。如果电脑开着代理软件，并且把这个域名转给了境外节点，建立会话会多花几秒；可以在代理规则里把 `bytedance.com` 设为直连。

## 开发

```sh
python3 -m unittest discover -s tests -v
```

| 文件 | 内容 |
| --- | --- |
| `live_interpreter/volc_ast.py` | 同传 WebSocket 协议（自带精简 protobuf 编解码，无需 protobuf 依赖） |
| `live_interpreter/pipeline.py` | 字幕事件 → 字幕段、原文配对、会议记录 |
| `live_interpreter/board.py` | 字幕状态和推送 |
| `live_interpreter/server.py` | 网页、接口和 WebSocket |
| `live_interpreter/auth.py` | 控制台登录 |
| `live_interpreter/settings.py` | 控制台设置的保存 |
| `live_interpreter/capture.py` | 云端模式的收音会话 |
| `live_interpreter/static/index.html` | 字幕屏 |
| `live_interpreter/static/console.html`、`login.html` | 控制台、登录页 |
| `live_interpreter/audio.py` | 麦克风和音频文件输入 |
| `live_interpreter/glossary.py` | 术语表解析 |
| `live_interpreter/app.py` | 命令行 |
