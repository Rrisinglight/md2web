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

# Функция для обновления секретного ключа
update_secret_key() {
    echo "Обновляем secret_key в app.py..."
    
    # Меняем размер случайного ключа с 24 на 8 байт
    sed -i "s/app.secret_key = os.urandom(24)/app.secret_key = os.urandom(8)/" "$APP_DIR/app.py"
    
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
server {
    listen 80;
    server_name _;  # Замените на ваше доменное имя если есть

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

    # Копируем новые файлы
    echo "Копируем обновленные файлы приложения в $APP_DIR..."
    cp -r ./* "$APP_DIR"

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

    # Запускаем службу снова
    echo "Запускаем службу $SERVICE_NAME после обновления..."
    systemctl start "$SERVICE_NAME"

    echo "Код приложения успешно обновлен и служба перезапущена."
}

# Функция для очистки предыдущей установки
cleanup_old_installation() {
    echo "Проверяем наличие старой установки..."
    
    # Проверяем и останавливаем старую службу
    if systemctl is-active --quiet mark2web.service; then
        echo "Останавливаем существующую службу mark2web.service..."
        systemctl stop mark2web.service
    fi
    
    # Проверяем и отключаем старую службу
    if systemctl is-enabled --quiet mark2web.service; then
        echo "Отключаем существующую службу mark2web.service..."
        systemctl disable mark2web.service
    fi
    
    # Удаляем старую директорию, если существует пользователь mark2web
    if id "mark2web" >/dev/null 2>&1; then
        echo "Обнаружен старый пользователь mark2web..."
        if [ -d "/home/mark2web" ]; then
            echo "Удаляем старую директорию /home/mark2web..."
            rm -rf "/home/mark2web"
        fi
        
        echo "Удаляем пользователя mark2web..."
        userdel -r mark2web 2>/dev/null || true
    fi
}

# Функция для проверки, является ли это первоначальной установкой или обновлением
initial_setup() {
    # Проверяем, существует ли уже сервис
    if systemctl list-unit-files | grep -q "$SERVICE_NAME"; then
        # Также проверяем, существует ли пользователь weber
        if id "$APP_USER" >/dev/null 2>&1 && [ -d "$APP_DIR" ]; then
            return 1  # Уже установлено, нужно обновление
        fi
    fi
    return 0  # Нужна первоначальная установка
}

# Основная логика скрипта
main() {
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
        update_secret_key
        create_systemd_service
        setup_nginx
        reload_and_start_service
        echo "Первоначальная настройка завершена. Сервис запущен."
    else
        echo "Обнаружена существующая установка. Выполняем обновление..."
        update_app_code
    fi
}

# Запуск основной функции
main

echo "Setup script завершен успешно."
echo "Ваше приложение доступно по адресу: http://localhost/"