#!/usr/bin/env bash
# Установка на Ubuntu: служба приложения, дополнительный сайт Caddy и копии SQLite.
set -euo pipefail
DOMAIN="${1:?Укажите домен: sudo bash deploy/install.sh 360.transtehcom.ru [порт HTTPS]}"
HTTPS_PORT="${2:-443}"
APP_DIR=/opt/360-workflow
APP_USER=s360
REPO=https://github.com/Transtehcom/360-workflow.git
[[ "$DOMAIN" =~ ^[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?$ ]] || { echo "Неверный домен"; exit 1; }
[[ "$HTTPS_PORT" =~ ^[0-9]{1,5}$ ]] && ((10#$HTTPS_PORT >= 1 && 10#$HTTPS_PORT <= 65535)) || { echo "Неверный порт HTTPS"; exit 1; }
[ "$(id -u)" -eq 0 ] || { echo "Запустите через sudo"; exit 1; }

apt-get update -q
apt-get install -y -q python3 git sqlite3
if ! apt-get install -y -q caddy; then
    # Caddy нет в репозиториях Ubuntu — ставим из официального репозитория Caddy
    apt-get install -y -q debian-keyring debian-archive-keyring apt-transport-https curl gnupg
    curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -q
    apt-get install -y -q caddy
fi
python3 -c 'import sqlite3, sys; assert sys.version_info >= (3, 10), "Нужен Python 3.10 или новее"'
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --no-create-home --shell /usr/sbin/nologin "$APP_USER"
if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" pull --ff-only
elif [ ! -d "$APP_DIR" ]; then
    git clone "$REPO" "$APP_DIR"
else
    echo "$APP_DIR существует, но не является Git-репозиторием. Проверьте каталог."; exit 1
fi
mkdir -p "$APP_DIR/data"
chown -R "$APP_USER:$APP_USER" "$APP_DIR/data"
chmod 750 "$APP_DIR/data"
if [ ! -f "$APP_DIR/config.json" ]; then
    cp "$APP_DIR/config.example.json" "$APP_DIR/config.json"
    echo "Создан config.json. Для переноса ответов скопируйте также data/360.sqlite при остановленном приложении."
fi
# Копия настроек делается один раз: повторный запуск не затирает исходный файл.
[ -f "$APP_DIR/config.json.before-install" ] || cp -p "$APP_DIR/config.json" "$APP_DIR/config.json.before-install"
chown "root:$APP_USER" "$APP_DIR/config.json" "$APP_DIR/config.json.before-install"
chmod 640 "$APP_DIR/config.json" "$APP_DIR/config.json.before-install"
PORT=$(python3 - "$APP_DIR/config.json" "$DOMAIN" "$HTTPS_PORT" <<'PY'
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
config = json.loads(path.read_text(encoding="utf-8"))
server = config.setdefault("server", {})
port = server.setdefault("port", 8082)
if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
    raise SystemExit("server.port должен быть числом от 1 до 65535")
suffix = "" if int(sys.argv[3]) == 443 else ":" + str(int(sys.argv[3]))
server.update(host="127.0.0.1", secure_cookie=True, trust_proxy=True, open_browser=False,
              public_url="https://" + sys.argv[2] + suffix)
# сервер обычно в UTC, а офис — UTC+5: от пояса зависят «сегодня», сроки и напоминания. Заданный пояс не меняем
if not config.get("timezone"):
    config["timezone"] = "Asia/Yekaterinburg"
path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(port)
PY
)

# Добавляем сайт к существующим блокам, не заменяя Caddyfile.
if ! grep -q '^# 360-workflow:' /etc/caddy/Caddyfile 2>/dev/null; then
    SITE="$DOMAIN"
    [ "$HTTPS_PORT" = "443" ] || SITE="$DOMAIN:$HTTPS_PORT"
    BLOCK=$(mktemp)
    trap 'rm -f "$BLOCK"' EXIT
    sed -e "s/^DOMAIN {/$SITE {/" -e "s/127.0.0.1:PORT/127.0.0.1:$PORT/" "$APP_DIR/deploy/Caddyfile" > "$BLOCK"
    # Проверка отдельного блока до изменения общего файла.
    caddy validate --config "$BLOCK" --adapter caddyfile >/dev/null
    if [ -f /etc/caddy/Caddyfile ]; then
        cp -p /etc/caddy/Caddyfile /etc/caddy/Caddyfile.before-360
    fi
    printf '\n' >> /etc/caddy/Caddyfile
    cat "$BLOCK" >> /etc/caddy/Caddyfile
    if ! caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile; then
        if [ -f /etc/caddy/Caddyfile.before-360 ]; then
            cp -p /etc/caddy/Caddyfile.before-360 /etc/caddy/Caddyfile
        fi
        echo "Caddyfile не прошёл проверку. Предыдущий файл восстановлен."; exit 1
    fi
else
    echo "Сайт 360-workflow уже есть в Caddyfile. При смене домена или порта обновите его вручную."
    if awk '/^# 360-workflow:/{f=1} f' /etc/caddy/Caddyfile | grep -q '127\.0\.0\.1:18082'; then
        echo "ВНИМАНИЕ: блок 360-workflow всё ещё указывает на 127.0.0.1:18082 — трафик идёт на офисный туннель."
        echo "Замените порт на 127.0.0.1:$PORT в /etc/caddy/Caddyfile вручную, затем: caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile && systemctl reload caddy"
    fi
    caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
fi
install -m 644 "$APP_DIR/deploy/360-workflow.service" /etc/systemd/system/360-workflow.service
systemctl daemon-reload
systemctl enable 360-workflow >/dev/null
systemctl restart 360-workflow
systemctl enable caddy >/dev/null
systemctl reload-or-restart caddy

cat > /etc/cron.daily/360-workflow-backup <<'BACKUP'
#!/bin/sh
set -eu
dir=/var/backups/360-workflow
mkdir -p "$dir"
chmod 700 "$dir"
sqlite3 /opt/360-workflow/data/360.sqlite ".backup '$dir/360-$(date +%F).sqlite'"
find "$dir" -name '360-*.sqlite' -mtime +14 -delete
BACKUP
chmod 755 /etc/cron.daily/360-workflow-backup
if command -v ufw >/dev/null && ufw status | grep -q 'Status: active'; then
    ufw allow 80/tcp >/dev/null
    ufw allow "$HTTPS_PORT/tcp" >/dev/null
fi
echo "Готово. Приложение: https://$DOMAIN$([ "$HTTPS_PORT" = "443" ] || echo ":$HTTPS_PORT")"
echo "Первый администратор: cd $APP_DIR && sudo -u $APP_USER python3 manage.py create-admin ЛОГИН"
echo "Настройки Битрикс24: $APP_DIR/config.json; проверка: sudo -u $APP_USER python3 manage.py check"
echo "Журнал: journalctl -u 360-workflow -f"
