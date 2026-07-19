#!/bin/bash
# 热点聚合 - 阿里云 ECS 一键部署脚本
# 用法: sudo bash setup.sh
# 适用于 Ubuntu 20.04/22.04/24.04

set -e

DOMAIN="wzti-test.asia"
APP_DIR="/opt/hotnews"
APP_USER="www-hotnews"

echo "================================"
echo "  热点聚合 - ECS 部署脚本"
echo "  域名: $DOMAIN"
echo "================================"

# ── 1. 系统依赖 ──
echo "[1/6] 安装系统依赖..."
apt update
apt install -y python3 python3-venv python3-pip nginx certbot python3-certbot-nginx git

# ── 2. 创建用户 ──
echo "[2/6] 创建服务用户..."
if ! id "$APP_USER" &>/dev/null; then
    useradd -r -s /bin/false "$APP_USER"
    echo "  已创建用户 $APP_USER"
else
    echo "  用户已存在，跳过"
fi

# ── 3. 部署代码 ──
echo "[3/6] 部署代码到 $APP_DIR ..."
# 首次部署：手动把代码上传后再跑此脚本
if [ ! -d "$APP_DIR" ]; then
    mkdir -p "$APP_DIR"
fi
mkdir -p "$APP_DIR/data"
mkdir -p /var/log/hotnews

# 虚拟环境 & 依赖
if [ ! -d "$APP_DIR/venv" ]; then
    python3 -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q --disable-pip-version-check

# 权限
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chown -R "$APP_USER:$APP_USER" /var/log/hotnews

# ── 4. systemd 服务 ──
echo "[4/6] 配置 systemd 服务..."
cp "$APP_DIR/deploy/hotnews.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable hotnews
systemctl restart hotnews
echo "  等待应用启动..."
sleep 3
systemctl status hotnews --no-pager || true

# ── 5. Nginx ──
echo "[5/6] 配置 Nginx..."
cp "$APP_DIR/deploy/nginx-hotnews.conf" /etc/nginx/sites-available/hotnews
ln -sf /etc/nginx/sites-available/hotnews /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

# ── 6. HTTPS 证书 ──
echo "[6/6] 申请 HTTPS 证书..."
echo ""
echo "  !!! 请确认 DNS 已将 $DOMAIN 指向本服务器 IP !!!"
echo "  如果还没配置 DNS，按 Ctrl+C 跳过，稍后手动执行："
echo "  sudo certbot --nginx -d $DOMAIN -d www.$DOMAIN"
echo ""
read -p "  DNS 已配置好？继续申请证书？(y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    certbot --nginx -d "$DOMAIN" -d "www.$DOMAIN" --non-interactive --agree-tos -m admin@"$DOMAIN"
    echo "  HTTPS 证书已安装！"
fi

echo ""
echo "================================"
echo "  部署完成！"
echo ""
echo "  访问地址: https://$DOMAIN"
echo ""
echo "  常用命令:"
echo "    查看状态:  systemctl status hotnews"
echo "    查看日志:  tail -f /var/log/hotnews/error.log"
echo "    重启服务:  systemctl restart hotnews"
echo "    更新代码后: systemctl restart hotnews"
echo "================================"
