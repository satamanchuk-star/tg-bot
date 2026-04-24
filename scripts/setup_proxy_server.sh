#!/bin/bash
# Скрипт настройки SOCKS5-прокси (3proxy) на чистом Ubuntu 24.04
# Запускать от root на Hetzner VPS:
#   curl -sSL https://raw.githubusercontent.com/.../setup_proxy.sh | bash
#
# После запуска скрипт напечатает строку PROXY_MANUAL для .env

set -euo pipefail

PROXY_USER="${PROXY_USER:-alexbot}"
PROXY_PASS="${PROXY_PASS:-$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 20)}"
PROXY_PORT="${PROXY_PORT:-1080}"

echo "==> Устанавливаю 3proxy..."
apt-get update -qq
apt-get install -y -qq 3proxy

echo "==> Настраиваю конфиг..."
mkdir -p /etc/3proxy
cat > /etc/3proxy/3proxy.cfg <<EOF
# DNS-серверы
nserver 1.1.1.1
nserver 8.8.8.8

# Аутентификация по логину/паролю
auth strong
users ${PROXY_USER}:CL:${PROXY_PASS}
allow ${PROXY_USER}

# Лог
log /var/log/3proxy.log D

# SOCKS5 на порту ${PROXY_PORT}
socks -p${PROXY_PORT}
EOF

echo "==> Запускаю 3proxy..."
systemctl enable 3proxy
systemctl restart 3proxy

echo "==> Открываю порт в фаерволе (ufw)..."
if command -v ufw &>/dev/null; then
    ufw allow "${PROXY_PORT}/tcp" comment "3proxy SOCKS5" || true
fi

# Пауза чтобы сервис поднялся
sleep 2

# Проверка
if ss -tlnp | grep -q ":${PROXY_PORT}"; then
    echo ""
    echo "=================================================="
    echo "  3proxy запущен и слушает порт ${PROXY_PORT}"
    echo "=================================================="
    echo ""
    SERVER_IP=$(curl -s4 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')
    echo "  Добавьте в .env на боте:"
    echo ""
    echo "  PROXY_MANUAL=socks5://${PROXY_USER}:${PROXY_PASS}@${SERVER_IP}:${PROXY_PORT}"
    echo ""
    echo "  Или в docker-compose.yaml:"
    echo "  - PROXY_MANUAL=socks5://${PROXY_USER}:${PROXY_PASS}@${SERVER_IP}:${PROXY_PORT}"
    echo "=================================================="
else
    echo "ОШИБКА: 3proxy не запустился. Смотри: systemctl status 3proxy"
    exit 1
fi
