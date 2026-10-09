#!/usr/bin/env bash
# 部署或更新云端模式到服务器（Alibaba Cloud Linux 3 / RHEL 系，需 root）。
#
# 用法：deploy/deploy.sh <ssh 主机> <对外网址>
# 例如：deploy/deploy.sh myserver https://1.2.3.4/interpret
#
# 服务挂在服务器上已有 Caddy 站点的一个子路径下（国内服务器上未备案的域名会被拦截，
# 可以直接用 IP 访问）。Caddy 的位置写在本地 deploy/server.env，参考 deploy/server.env.example。
#
# 首次部署会：安装 python3.11 和 rsync、创建 interpreter 用户、生成控制台登录密码、
# 从本地 .env 读取 VOLC_API_KEY 写到服务器、把路由挂进 Caddy 并重启。
# 之后再运行只同步代码并重启服务；服务器上的 .env 和 data/（设置、术语表、会议记录）不会被覆盖。
set -euo pipefail

HOST=${1:?用法：deploy/deploy.sh <ssh 主机> <对外网址>}
PUBLIC_URL=${2:?用法：deploy/deploy.sh <ssh 主机> <对外网址>}
PUBLIC_URL=${PUBLIC_URL%/}
ORIGIN=$(printf '%s' "$PUBLIC_URL" | sed -E 's#^(https://[^/]+).*#\1#')
PREFIX=${PUBLIC_URL#"$ORIGIN"}
[ -n "$PREFIX" ] || { echo "对外网址需要带子路径，例如 https://1.2.3.4/interpret"; exit 1; }

ROOT=$(cd "$(dirname "$0")/.." && pwd)
SERVER_ENV="$ROOT/deploy/server.env"
[ -f "$SERVER_ENV" ] || { echo "缺少 $SERVER_ENV，请参考 deploy/server.env.example 创建"; exit 1; }
# shellcheck disable=SC1090
. "$SERVER_ENV"
for var in CADDYFILE CADDY_ENV CADDY_BIN CADDY_UNIT CADDY_USER CADDY_DATA; do
  [ -n "${!var:-}" ] || { echo "deploy/server.env 缺少 $var"; exit 1; }
done
APP=/opt/ai-live-interpreter
PORT=18765

ssh_run() { ssh -o BatchMode=yes "$HOST" "$@"; }

echo "==> 准备运行环境"
ssh_run "set -e
  command -v python3.11 >/dev/null || dnf install -y -q python3.11
  command -v rsync >/dev/null || dnf install -y -q rsync
  id interpreter >/dev/null 2>&1 || useradd --system --no-create-home --shell /sbin/nologin interpreter
  mkdir -p $APP/data $APP/ops
  # 早期版本的会议记录在 transcripts/，迁到 data/transcripts/
  if [ -d $APP/transcripts ]; then mkdir -p $APP/data/transcripts && mv -n $APP/transcripts/* $APP/data/transcripts/ 2>/dev/null; rmdir $APP/transcripts 2>/dev/null || true; fi
  chown -R interpreter:interpreter $APP/data
  chmod 750 $APP/data"

echo "==> 同步代码"
# 不保留本机的属主（macOS 自带 rsync 不支持 --chown），落地后归 root。
rsync -rlptz --delete \
  --exclude '.git/' --exclude '.env' --exclude 'transcripts/' --exclude '.local-test/' \
  --exclude '__pycache__/' --exclude 'venv/' --exclude 'glossary.txt' --exclude 'ops/' --exclude 'data/' \
  "$ROOT/" "$HOST:$APP/"

echo "==> 安装依赖"
ssh_run "set -e
  [ -x $APP/venv/bin/python ] || python3.11 -m venv $APP/venv
  $APP/venv/bin/pip install -q --disable-pip-version-check 'aiohttp>=3.9'
  install -m 644 $APP/deploy/ai-live-interpreter.service /etc/systemd/system/ai-live-interpreter.service
  systemctl daemon-reload"

echo "==> 配置 .env（已存在则保留，只补缺的项）"
if ! ssh_run "test -f $APP/.env"; then
  KEY=$(grep -E '^VOLC_API_KEY=' "$ROOT/.env" | head -1 | cut -d= -f2-)
  [ -n "$KEY" ] || { echo "本地 .env 里没有 VOLC_API_KEY"; exit 1; }
  # 通过 stdin 传密钥，不出现在命令行参数里。
  printf 'VOLC_API_KEY=%s\n' "$KEY" | ssh_run "umask 027; cat > $APP/.env && chown root:interpreter $APP/.env"
fi
ssh_run "set -e
  cd $APP
  # 早期版本用网址口令，现在改为登录，删掉旧口令
  sed -i '/^CAPTURE_TOKEN=/d; /^VIEW_TOKEN=/d; /^HOST=/d; /^PORT=/d; /^PUBLIC_URL=/d; /^DATA_DIR=/d' .env
  if ! grep -q '^ADMIN_PASSWORD=' .env; then
    # 去掉容易看错的字符（0/o、1/l/i），方便口头告诉现场的人
    echo \"ADMIN_PASSWORD=\$(tr -dc 'abcdefghjkmnpqrstuvwxyz23456789' </dev/urandom | head -c 10)\" >> .env
  fi
  printf 'HOST=127.0.0.1\nPORT=%s\nPUBLIC_URL=%s\nDATA_DIR=%s\n' $PORT $PUBLIC_URL $APP/data >> .env"

echo "==> 启动服务"
ssh_run "systemctl enable -q ai-live-interpreter && systemctl restart ai-live-interpreter && sleep 2 && systemctl is-active ai-live-interpreter && curl -fsS -o /dev/null http://127.0.0.1:$PORT/ && echo '本机访问正常'"

echo "==> 配置 Caddy（无变化则跳过）"
ssh_run bash -s -- "$APP" "$PREFIX" "$ORIGIN" "$PORT" \
  "$CADDYFILE" "$CADDY_ENV" "$CADDY_BIN" "$CADDY_UNIT" "$CADDY_USER" "$CADDY_DATA" <<'REMOTE'
set -euo pipefail
APP=$1 PREFIX=$2 ORIGIN=$3 PORT=$4
CADDYFILE=$5 CADDY_ENV=$6 CADDY_BIN=$7 CADDY_UNIT=$8 CADDY_USER=$9 CADDY_DATA=${10}
SNIPPET=$APP/ops/interpret.caddy
IMPORT="import $SNIPPET"
NAME=interpret$(printf '%s' "$PREFIX" | tr -c 'a-zA-Z0-9' '_')

cat > "$SNIPPET.new" <<EOF
# 由 ai-live-interpreter/deploy/deploy.sh 生成，挂在 IP 站点的 route 块里。
@$NAME path $PREFIX $PREFIX/*
handle @$NAME {
    route {
        @${NAME}_no_slash path $PREFIX
        redir @${NAME}_no_slash $PREFIX/?{query} 308
        # 只允许本站页面发起请求，防止其他网站借用浏览器里的口令连接 WebSocket。
        @${NAME}_cross_origin {
            header Origin *
            not header Origin $ORIGIN
        }
        respond @${NAME}_cross_origin "Request origin rejected" 403
        uri strip_prefix $PREFIX
        header Cache-Control "no-store"
        reverse_proxy 127.0.0.1:$PORT
    }
}
EOF

# 已挂在 route 块里（行首有缩进）且内容没变，就什么都不做。
if [ -f "$SNIPPET" ] && cmp -s "$SNIPPET" "$SNIPPET.new" && grep -qE "^\s+$IMPORT\s*$" "$CADDYFILE"; then
  rm "$SNIPPET.new"
  echo "Caddy 配置无变化"
  exit 0
fi

BACKUP=$CADDYFILE.bak-$(date +%Y%m%d%H%M%S)
cp -p "$CADDYFILE" "$BACKUP"
OLD_SNIPPET=$(mktemp)
if [ -f "$SNIPPET" ]; then cp -p "$SNIPPET" "$OLD_SNIPPET"; fi
restore() {
  cp -p "$BACKUP" "$CADDYFILE"
  if [ -s "$OLD_SNIPPET" ]; then cp -p "$OLD_SNIPPET" "$SNIPPET"; fi
}
mv "$SNIPPET.new" "$SNIPPET"
chmod 644 "$SNIPPET"

# 去掉所有旧的 import 行（包括早期版本加在文件末尾的），再挂到第一个 route 块开头。
grep -vxF "$IMPORT" "$CADDYFILE" | grep -vE "^\s+$IMPORT\s*$" > "$CADDYFILE.tmp" || true
awk -v line="        $IMPORT" '{ print } !done && /^[[:space:]]*route[[:space:]]*\{[[:space:]]*$/ { print line; done = 1 }' \
  "$CADDYFILE.tmp" > "$CADDYFILE.new"
rm "$CADDYFILE.tmp"
if ! grep -qF "$IMPORT" "$CADDYFILE.new"; then
  rm "$CADDYFILE.new"; restore; echo "Caddyfile 里没找到 route 块，已还原"; exit 1
fi
cat "$CADDYFILE.new" > "$CADDYFILE"   # 保留原文件的属主和权限
rm "$CADDYFILE.new"

# 用 Caddy 服务自己的用户、环境变量和数据目录校验，避免以 root 写入证书存储。
if ! runuser -u "$CADDY_USER" -- env $(grep -vE '^\s*(#|$)' "$CADDY_ENV" | xargs) \
     XDG_DATA_HOME="$CADDY_DATA" XDG_CONFIG_HOME="$CADDY_DATA" \
     "$CADDY_BIN" validate --config "$CADDYFILE" --adapter caddyfile >/tmp/interpret-caddy-validate.log 2>&1; then
  tail -5 /tmp/interpret-caddy-validate.log
  restore; echo "Caddy 配置校验失败，已还原"; exit 1
fi
if ! systemctl restart "$CADDY_UNIT" || ! sleep 2 || ! systemctl is-active -q "$CADDY_UNIT"; then
  restore; systemctl restart "$CADDY_UNIT"; echo "Caddy 重启失败，已还原"; exit 1
fi
echo "Caddy 已更新，原配置备份在 $BACKUP"
REMOTE

echo "==> 访问地址"
ssh_run "cd $APP && . ./.env && echo \"控制台：\$PUBLIC_URL/\" && echo \"密码：  \$ADMIN_PASSWORD\""
