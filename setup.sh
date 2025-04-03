#!/bin/bash

# setup.sh
# Этот скрипт настраивает и запускает Flask приложение как сервис systemd

# Немедленно выходить при ошибке
set -e

# Переменные (измените при необходимости)
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

# Определение режима работы (полная установка или только обновление)
UPDATE_ONLY=false

# Обработка аргументов командной строки
while [[ $# -gt 0 ]]; do
    key="$1"
    case $key in
        --update)
            UPDATE_ONLY=true
            shift
            ;;
        --help)
            echo "Использование: $0 [опции]"
            echo "Опции:"
            echo "  --update     Выполнить только обновление кода приложения"
            echo "  --help       Показать это сообщение помощи"
            exit 0
            ;;
        *)
            echo "Неизвестная опция: $1"
            echo "Используйте --help для получения справки"
            exit 1
            ;;
    esac
done

# Проверка, что скрипт выполняется от root
if [ "$EUID" -ne 0 ]; then
    echo "Пожалуйста, запустите скрипт от имени root."
    exit 1
fi

# Функция для установки системных пакетов
install_system_packages() {
    echo "Устанавливаем системные пакеты..."
    apt-get update
    apt-get install -y python3 python3-venv python3-pip nginx
}

# Функция для создания пользователя и группы приложения
create_app_user() {
    if id "$APP_USER" >/dev/null 2>&1; then
        echo "Пользователь $APP_USER уже существует."
    else
        echo "Создаем пользователя и группу $APP_USER..."
        useradd -m -s /bin/bash -U $APP_USER
    fi
}

# Функция для создания каталога приложения
create_app_directory() {
    if [ ! -d "$APP_DIR" ]; then
        echo "Создаем каталог приложения $APP_DIR..."
        mkdir -p "$APP_DIR"
        mkdir -p "$APP_DIR/uploads"
        mkdir -p "$APP_DIR/templates"
    fi
}

# Функция для копирования файлов приложения
copy_app_files() {
    echo "Копируем файлы приложения в $APP_DIR..."
    cp -r ./* "$APP_DIR"
    
    # Создание uploads директории если её нет
    if [ ! -d "$APP_DIR/uploads" ]; then
        mkdir -p "$APP_DIR/uploads"
    fi
}

# Функция для установки прав собственности
set_ownership() {
    echo "Устанавливаем права собственности на $APP_DIR для $APP_USER:$APP_GROUP..."
    chown -R $APP_USER:$APP_GROUP "$APP_DIR"
    chmod -R 755 "$APP_DIR"
}

# Функция для настройки виртуального окружения Python
setup_virtualenv() {
    if [ ! -d "$VENV_DIR" ]; then
        echo "Создаем виртуальное окружение Python..."
        sudo -u $APP_USER $PYTHON_BIN -m venv "$VENV_DIR"
    fi

    echo "Устанавливаем зависимости Python..."
    sudo -u $APP_USER "$VENV_DIR/bin/pip" install --upgrade pip
    
    # Проверяем наличие requirements.txt
    if [ ! -f "$REQUIREMENTS_FILE" ]; then
        echo "ВНИМАНИЕ: Файл requirements.txt не найден!"
        echo "Для корректной работы приложения необходимо создать файл requirements.txt"
        echo "с указанием всех зависимостей (Flask, markdown, pymdownx, gunicorn и т.д.)."
        echo "Установка продолжится, но может работать некорректно."
        
        # Запрашиваем у пользователя, хочет ли он продолжить
        read -p "Хотите продолжить без requirements.txt? (y/n): " answer
        if [ "$answer" != "y" ] && [ "$answer" != "Y" ]; then
            echo "Установка прервана пользователем."
            exit 1
        fi
        
        # Устанавливаем минимальный набор зависимостей для базовой работы
        echo "Устанавливаем минимальный набор зависимостей..."
        sudo -u $APP_USER "$VENV_DIR/bin/pip" install Flask gunicorn markdown
    else
        echo "Найден файл requirements.txt. Устанавливаем зависимости..."
        sudo -u $APP_USER "$VENV_DIR/bin/pip" install -r "$REQUIREMENTS_FILE"
    fi
}

# Функция для настройки абсолютных путей
update_paths() {
    echo "Обновляем пути к директориям на абсолютные..."
    
    # Обновляем пути к директориям на абсолютные
    sed -i "s|app.config\['UPLOAD_FOLDER'\] = 'uploads'|app.config\['UPLOAD_FOLDER'\] = '$APP_DIR/uploads'|" "$APP_DIR/app.py"
    sed -i "s|app.config\['DATABASE'\] = 'database.db'|app.config\['DATABASE'\] = '$APP_DIR/database.db'|" "$APP_DIR/app.py"
}

# Функция для создания файла службы systemd
create_systemd_service() {
    echo "Создаем файл службы systemd..."
    cat > "$SERVICE_FILE_DEST" << EOF
[Unit]
Description=Mark2Web Flask Application via Gunicorn
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

# Функция для настройки Nginx как прокси
setup_nginx() {
    echo "Настраиваем Nginx как прокси для Flask приложения..."
    cat > "/etc/nginx/sites-available/mark2web" << EOF
# HTTP редирект на HTTPS
server {
    listen 80;
    server_name mark2web.ru www.mark2web.ru;
    return 301 https://\$server_name\$request_uri;
}

# HTTPS конфигурация
server {
    listen 443 ssl;
    server_name mark2web.ru www.mark2web.ru;

    ssl_certificate /etc/letsencrypt/live/mark2web.ru/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/mark2web.ru/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    location /static {
        alias $APP_DIR/static;
    }
}
EOF

    # Активируем конфигурацию
    ln -sf /etc/nginx/sites-available/mark2web /etc/nginx/sites-enabled/
    
    # Удаляем default конфигурацию если она существует
    if [ -f "/etc/nginx/sites-enabled/default" ]; then
        rm -f /etc/nginx/sites-enabled/default
    fi
    
    # Проверяем синтаксис Nginx
    nginx -t
    
    # Перезапускаем Nginx
    systemctl restart nginx
}

# Функция для установки и настройки SSL с помощью Certbot
setup_ssl() {
    # Проверяем, существует ли уже сертификат
    if [ -f "/etc/letsencrypt/renewal/mark2web.ru.conf" ]; then
        echo "Сертификат SSL уже существует для домена mark2web.ru. Пропускаем получение нового сертификата."
        return 0
    fi

    echo "Устанавливаем Certbot для настройки HTTPS..."
    apt-get update
    apt-get install -y certbot python3-certbot-nginx
    
    echo "Получаем SSL сертификат для mark2web.ru..."
    certbot --nginx -d mark2web.ru -d www.mark2web.ru --non-interactive --agree-tos --email webmaster@mark2web.ru
    
    echo "Настройка автоматического обновления сертификата..."
    systemctl enable certbot.timer
    systemctl start certbot.timer
    
    echo "HTTPS успешно настроен для вашего домена mark2web.ru!"
}

# Функция для перезагрузки и запуска службы systemd
reload_and_start_service() {
    echo "Перезагружаем демона systemd..."
    systemctl daemon-reload

    echo "Включаем и запускаем службу $SERVICE_NAME..."
    systemctl enable "$SERVICE_NAME"
    systemctl restart "$SERVICE_NAME"

    echo "Включаем и запускаем службу nginx..."
    systemctl enable nginx
    systemctl restart nginx

    echo "Служба $SERVICE_NAME успешно запущена."
}

# Функция для обновления кода приложения
update_app_code() {
    echo "Обновляем код приложения..."

    # Останавливаем службу перед обновлением
    echo "Останавливаем службу $SERVICE_NAME..."
    systemctl stop "$SERVICE_NAME" || true  # Добавляем || true, чтобы скрипт не падал, если служба не запущена

    # Создаем директорию, если она не существует
    if [ ! -d "$APP_DIR" ]; then
        echo "Каталог $APP_DIR не существует. Создаем..."
        mkdir -p "$APP_DIR"
        mkdir -p "$APP_DIR/uploads"
        mkdir -p "$APP_DIR/templates"
    fi

    # Создаем временный бэкап пользовательских данных и базы данных
    if [ -d "$APP_DIR/uploads" ] && [ "$(ls -A "$APP_DIR/uploads" 2>/dev/null)" ]; then
        echo "Создаем бэкап пользовательских файлов..."
        BACKUP_DIR="$APP_HOME/backup_$(date +%Y%m%d%H%M%S)"
        mkdir -p "$BACKUP_DIR"
        cp -r "$APP_DIR/uploads" "$BACKUP_DIR/"
    fi

    # Бэкап базы данных, если она существует
    if [ -f "$APP_DIR/database.db" ]; then
        echo "Создаем бэкап базы данных..."
        if [ ! -d "$BACKUP_DIR" ]; then
            BACKUP_DIR="$APP_HOME/backup_$(date +%Y%m%d%H%M%S)"
            mkdir -p "$BACKUP_DIR"
        fi
        cp "$APP_DIR/database.db" "$BACKUP_DIR/"
    fi

    # Копируем новые файлы, но сохраняем определенные директории
    echo "Копируем обновленные файлы приложения в $APP_DIR..."
    # Сохраняем uploads директорию и базу данных
    if [ -d "$APP_DIR/uploads" ]; then
        TMP_UPLOADS="$APP_HOME/tmp_uploads_$(date +%Y%m%d%H%M%S)"
        mv "$APP_DIR/uploads" "$TMP_UPLOADS"
    fi
    
    if [ -f "$APP_DIR/database.db" ]; then
        TMP_DB="$APP_HOME/tmp_db_$(date +%Y%m%d%H%M%S)"
        mv "$APP_DIR/database.db" "$TMP_DB"
    fi
    
    # Копируем новые файлы
    cp -r ./* "$APP_DIR"
    
    # Восстанавливаем uploads и базу данных, если они были сохранены
    if [ -d "$TMP_UPLOADS" ]; then
        echo "Восстанавливаем пользовательские файлы..."
        mkdir -p "$APP_DIR/uploads"
        cp -r "$TMP_UPLOADS"/* "$APP_DIR/uploads/"
        rm -rf "$TMP_UPLOADS"
    fi
    
    if [ -f "$TMP_DB" ]; then
        echo "Восстанавливаем базу данных..."
        cp "$TMP_DB" "$APP_DIR/database.db"
        rm -f "$TMP_DB"
    fi

    # Устанавливаем права собственности
    echo "Устанавливаем права собственности на обновленные файлы..."
    chown -R $APP_USER:$APP_GROUP "$APP_DIR"
    chmod -R 755 "$APP_DIR"

    # Создаем виртуальное окружение, если его нет
    if [ ! -d "$VENV_DIR" ]; then
        echo "Виртуальное окружение не существует. Создаем..."
        sudo -u $APP_USER $PYTHON_BIN -m venv "$VENV_DIR"
    fi

    # Устанавливаем зависимости (если изменились)
    echo "Устанавливаем зависимости Python после обновления..."
    sudo -u $APP_USER "$VENV_DIR/bin/pip" install --upgrade pip
    sudo -u $APP_USER "$VENV_DIR/bin/pip" install -r "$REQUIREMENTS_FILE"

    # Обновляем пути, если это необходимо
    update_paths

    # Запускаем службу снова
    echo "Запускаем службу $SERVICE_NAME после обновления..."
    systemctl start "$SERVICE_NAME"

    echo "Код приложения успешно обновлен и служба перезапущена."
    
    # Информация о созданном бэкапе, если он был создан
    if [ -d "$BACKUP_DIR" ]; then
        echo "Был создан бэкап ваших данных в директории: $BACKUP_DIR"
    fi
}

# Функция для очистки предыдущей установки
cleanup_old_installation() {
    echo "Проверяем наличие устаревшей установки..."
    
    # Проверяем наличие старого пользователя mark2web (если переходим с предыдущей версии)
    if id "mark2web" >/dev/null 2>&1; then
        echo "Обнаружен устаревший пользователь mark2web..."
        
        # Проверяем и останавливаем старую службу
        if systemctl is-active --quiet mark2web.service; then
            echo "Останавливаем устаревшую службу mark2web.service..."
            systemctl stop mark2web.service
        fi
        
        # Проверяем и отключаем старую службу
        if systemctl is-enabled --quiet mark2web.service; then
            echo "Отключаем устаревшую службу mark2web.service..."
            systemctl disable mark2web.service
        fi
        
        # Удаляем старую директорию, если существует пользователь mark2web
        if [ -d "/home/mark2web" ]; then
            echo "Удаляем устаревшую директорию /home/mark2web..."
            # Создаем архив данных перед удалением
            if [ -d "/home/mark2web/mark2web/uploads" ] || [ -f "/home/mark2web/mark2web/database.db" ]; then
                echo "Создаем архив данных из устаревшей установки..."
                BACKUP_DIR="/root/old_mark2web_backup_$(date +%Y%m%d%H%M%S)"
                mkdir -p "$BACKUP_DIR"
                
                if [ -d "/home/mark2web/mark2web/uploads" ]; then
                    cp -r "/home/mark2web/mark2web/uploads" "$BACKUP_DIR/"
                fi
                
                if [ -f "/home/mark2web/mark2web/database.db" ]; then
                    cp "/home/mark2web/mark2web/database.db" "$BACKUP_DIR/"
                fi
                
                echo "Архив данных создан в директории: $BACKUP_DIR"
            fi
            
            # Теперь удаляем директорию
            rm -rf "/home/mark2web"
        fi
        
        echo "Удаляем устаревшего пользователя mark2web..."
        userdel -r mark2web 2>/dev/null || true
        
        echo "Удаление устаревшей установки завершено."
    else
        echo "Устаревшая установка не обнаружена."
    fi
}

# Функция для проверки, является ли это первоначальной установкой или обновлением
initial_setup() {
    # Проверяем наличие основных компонентов существующей установки
    
    # 1. Проверяем, существует ли уже сервис systemd
    if systemctl list-unit-files | grep -q "$SERVICE_NAME"; then
        echo "Обнаружен существующий systemd сервис: $SERVICE_NAME"
        
        # 2. Проверяем, существует ли пользователь приложения
        if id "$APP_USER" >/dev/null 2>&1; then
            echo "Обнаружен существующий пользователь: $APP_USER"
            
            # 3. Проверяем, существует ли директория приложения
            if [ -d "$APP_DIR" ]; then
                echo "Обнаружена существующая директория приложения: $APP_DIR"
                
                # 4. Проверяем, существует ли виртуальное окружение
                if [ -d "$VENV_DIR" ]; then
                    echo "Обнаружено существующее виртуальное окружение Python"
                    return 1  # Уже установлено, нужно обновление
                fi
            fi
        fi
    fi
    
    echo "Не обнаружена полная существующая установка. Выполняем первоначальную установку."
    return 0  # Нужна первоначальная установка
}

# Основная логика скрипта
main() {
    # Если указан режим только обновления, обходим все проверки и сразу обновляем код
    if [ "$UPDATE_ONLY" = true ]; then
        echo "Запуск в режиме обновления кода..."
        update_app_code
        reload_and_start_service
        echo "Обновление кода завершено успешно."
        return 0
    fi

    # Сначала очищаем старую установку
    cleanup_old_installation
    
    if initial_setup; then
        echo "Начинаем первоначальную настройку приложения..."
        install_system_packages
        create_app_user
        create_app_directory
        copy_app_files
        set_ownership
        setup_virtualenv
        update_paths
        create_systemd_service
        setup_nginx
        setup_ssl
        reload_and_start_service
        echo "Первоначальная настройка завершена. Сервис запущен."
    else
        echo "Обнаружена существующая установка. Выполняем обновление..."
        update_app_code
        setup_nginx
        # Не запускаем setup_ssl при обновлении, чтобы избежать запросов подтверждения
        reload_and_start_service
    fi
}

# Запуск основной функции
main

echo "Setup script завершен успешно."
if [ "$UPDATE_ONLY" = true ]; then
    echo "Код приложения обновлен."
else
    echo "Ваше приложение доступно по адресу: https://mark2web.ru/"
fi