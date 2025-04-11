#!/bin/bash

# setup.sh — установка Flask приложения с nginx и Let's Encrypt на несколько доменов

set -e

APP_USER="weber"
APP_GROUP="weber"
APP_HOME="/home/$APP_USER"
APP_DIR="$APP_HOME/mark2web"
PYTHON_BIN="/usr/bin/python3"
SERVICE_NAME="mark2web.service"
SERVICE_FILE_DEST="/etc/systemd/system/$SERVICE_NAME"
REQUIREMENTS_FILE="$APP_DIR/requirements.txt"
VENV_DIR="$APP_DIR/venv"
APP_SCRIPT="$APP_DIR/app.py"

UPDATE_ONLY=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --update)
            UPDATE_ONLY=true; shift ;;
        *) echo "Неизвестная опция: $1"; exit 1 ;;
    esac
done

if [ "$EUID" -ne 0 ]; then
    echo "Запустите от root."; exit 1
fi

install_system_packages() {
    apt-get update
    apt-get install -y python3 python3-venv python3-pip nginx certbot python3-certbot-nginx
}

create_app_user() {
    id "$APP_USER" >/dev/null 2>&1 || useradd -m -s /bin/bash -U $APP_USER
}

create_app_directory() {
    mkdir -p "$APP_DIR/uploads" "$APP_DIR/templates"
}

copy_app_files() {
    cp -r ./* "$APP_DIR"
}

set_ownership() {
    chown -R $APP_USER:$APP_GROUP "$APP_DIR"
    chmod -R 755 "$APP_DIR"
}

setup_virtualenv() {
    [ -d "$VENV_DIR" ] || sudo -u $APP_USER $PYTHON_BIN -m venv "$VENV_DIR"
    sudo -u $APP_USER "$VENV_DIR/bin/pip" install --upgrade pip
    if [ -f "$REQUIREMENTS_FILE" ]; then
        sudo -u $APP_USER "$VENV_DIR/bin/pip" install -r "$REQUIREMENTS_FILE"
    else
        sudo -u $APP_USER "$VENV_DIR/bin/pip" install Flask gunicorn markdown
    fi
}

update_paths() {
    sed -i "s|app.config\['UPLOAD_FOLDER'\] = 'uploads'|app.config['UPLOAD_FOLDER'] = '$APP_DIR/uploads'|" "$APP_SCRIPT"
    sed -i "s|app.config\['DATABASE'\] = 'database.db'|app.config['DATABASE'] = '$APP_DIR/database.db'|" "$APP_SCRIPT"
}

create_systemd_service() {
    cat > "$SERVICE_FILE_DEST" << EOF
[Unit]
Description=Mark2Web Flask App via Gunicorn
After=network.target

[Service]
User=$APP_USER
Group=$APP_GROUP
WorkingDirectory=$APP_DIR
ExecStart=$VENV_DIR/bin/gunicorn -w 4 -b 127.0.0.1:5000 app:app
Restart=always
Environment=FLASK_APP=app.py
Environment=FLASK_ENV=production

[Install]
WantedBy=multi-user.target
EOF
    chmod 644 "$SERVICE_FILE_DEST"
}

setup_nginx_initial() {
    cat > "/etc/nginx/sites-available/mark2web" << 'EOF'
server {
    listen 80;
    listen [::]:80;
    server_name mark2web.ru www.mark2web.ru md2web.ru www.md2web.ru;
    root /var/www/html;
    location / {
        return 200 'Temporary HTTP config for Certbot validation';
        add_header Content-Type text/plain;
    }
}
EOF
    ln -sf /etc/nginx/sites-available/mark2web /etc/nginx/sites-enabled/
    rm -f /etc/nginx/sites-enabled/default
    nginx -t
    systemctl restart nginx
}

setup_ssl() {
    certbot certonly --nginx \
        -d mark2web.ru -d www.mark2web.ru \
        -d md2web.ru -d www.md2web.ru \
        --non-interactive --agree-tos --email webmaster@mark2web.ru
    systemctl enable certbot.timer
    systemctl start certbot.timer
}

setup_nginx_secure() {
    cat > "/etc/nginx/sites-available/mark2web" << 'EOF'
server {
    listen 80;
    listen [::]:80;
    server_name mark2web.ru www.mark2web.ru md2web.ru www.md2web.ru;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_redirect off;
        proxy_buffering off;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }

    location /static {
        alias /home/weber/mark2web/static;
        expires 30d;
        add_header Cache-Control "public, no-transform";
    }
}

# Оставляем HTTPS-сервер на случай, если порт 443 станет доступен
server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name mark2web.ru www.mark2web.ru md2web.ru www.md2web.ru;

    ssl_certificate /etc/letsencrypt/live/mark2web.ru/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/mark2web.ru/privkey.pem;

    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers on;
    ssl_session_timeout 1d;
    ssl_session_cache shared:SSL:50m;
    ssl_session_tickets off;
    ssl_stapling on;
    ssl_stapling_verify on;
    resolver 8.8.8.8 8.8.4.4 valid=300s;
    resolver_timeout 5s;
    add_header Strict-Transport-Security "max-age=63072000" always;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_redirect off;
        proxy_buffering off;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }

    location /static {
        alias /home/weber/mark2web/static;
        expires 30d;
        add_header Cache-Control "public, no-transform";
    }
}
EOF
    nginx -t
    systemctl restart nginx
}

setup_nginx() {
    setup_nginx_initial
    setup_ssl
    setup_nginx_secure
}

reload_and_start_service() {
    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"
    systemctl restart "$SERVICE_NAME"
    systemctl enable nginx
    systemctl restart nginx
}

main() {
    if [ "$UPDATE_ONLY" = true ]; then
        echo "Обновление кода..."
        copy_app_files
        set_ownership
        setup_virtualenv
        update_paths
        reload_and_start_service
        echo "Обновление завершено."
        return
    fi

    install_system_packages
    create_app_user
    create_app_directory
    copy_app_files
    set_ownership
    setup_virtualenv
    update_paths
    create_systemd_service
    setup_nginx
    reload_and_start_service

    echo "Установка завершена. Приложение доступно на https://mark2web.ru и https://md2web.ru"
}

main