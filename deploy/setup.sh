#!/usr/bin/env bash
# 课程助手 · 服务器一键部署（Ubuntu 22.04 / 24.04）
#
# 用法：把本文件传到服务器，然后
#   chmod +x setup.sh && sudo ./setup.sh
#
# 默认从公开仓库拉代码。改 REPO 可以换成自己的 fork。

set -euo pipefail

REPO="${REPO:-https://github.com/42-beep/course-assistant.git}"
APP_DIR="${APP_DIR:-/opt/course-assistant}"
SERVICE="course-assistant"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

say "1/6 安装系统依赖"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git curl

say "2/6 拉取代码到 $APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  mkdir -p "$(dirname "$APP_DIR")"
  git clone --depth 1 "$REPO" "$APP_DIR"
fi

say "3/6 建虚拟环境并装依赖（PyMuPDF 有点大，约 1-2 分钟）"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q -U pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

say "4/6 准备数据目录与运行用户"
mkdir -p "$APP_DIR/data/uploads" "$APP_DIR/data/sessions"
if ! id -u www-data >/dev/null 2>&1; then
  useradd --system --no-create-home --shell /usr/sbin/nologin www-data
fi
chown -R www-data:www-data "$APP_DIR/data"

# config.json 里是 API key，只让运行用户读
if [ -f "$APP_DIR/config.json" ]; then
  chown www-data:www-data "$APP_DIR/config.json"
  chmod 600 "$APP_DIR/config.json"
  echo "    检测到已有 config.json，权限已收紧为 600"
else
  echo "    没有 config.json —— 服务器上填 key 不方便，建议现在建："
  echo "      sudo -u www-data nano $APP_DIR/config.json"
  echo "    内容参考 $APP_DIR/config.example.json"
fi

say "5/6 安装 systemd 服务"
install -m 644 "$APP_DIR/deploy/course-assistant.service" "/etc/systemd/system/$SERVICE.service"
systemctl daemon-reload
systemctl enable --now "$SERVICE"

say "6/6 检查状态"
sleep 2
systemctl --no-pager --lines=5 status "$SERVICE" || true

IP=$(curl -s --max-time 3 https://api.ipify.org || echo "服务器公网IP")
cat <<EOF

────────────────────────────────────────────
  服务已在 127.0.0.1:8000 起来，只监听本机。

  下一步二选一：

  A. 有域名（要备案）→ 装 Caddy 自动 HTTPS
       apt install -y caddy
       把 deploy/Caddyfile 里的域名改成你的，放到 /etc/caddy/Caddyfile
       systemctl reload caddy

  B. 没域名，先用 IP 直连（明文 HTTP，仅自己测试用）
       改 /etc/systemd/system/$SERVICE.service 里
         --host 127.0.0.1  →  --host 0.0.0.0
       在服务商控制台的安全组放行 8000 端口
       systemctl daemon-reload && systemctl restart $SERVICE
       然后访问 http://$IP:8000

  日志：journalctl -u $SERVICE -f
────────────────────────────────────────────
EOF
