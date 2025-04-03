import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta

import markdown
import pymdownx.tasklist
from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, session, send_from_directory, jsonify, g
)
from markdown.extensions import Extension
from markdown.preprocessors import Preprocessor
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# -------------------------------
#  ИНИЦИАЛИЗАЦИЯ FLASK-ПРИЛОЖЕНИЯ
# -------------------------------
app = Flask(__name__)
app.secret_key = 'mark2web_static_secret_key_for_sessions'

# ------------------------------------
#  НАСТРОЙКА ПАПОК И СИСТЕМНЫХ ПАРАМЕТРОВ
# ------------------------------------
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 150 * 1024 * 1024  # 150MB
app.config['DATABASE'] = 'database.db'
app.config['SERVER_URL'] = '202.181.188.118'  # Используем ваш сервер
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)

# ------------------
#  НАСТРОЙКА ЛОГОВ
# ------------------
if not app.debug:
    app.logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    app.logger.addHandler(handler)

    file_handler = RotatingFileHandler('mark2web.log', maxBytes=10485760, backupCount=10)
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)
    app.logger.addHandler(file_handler)

    app.logger.info('Mark2Web запущен')

# Создаём папку для загрузок, если она ещё не существует
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)


# --------------------------------------------------------------------
#  КАСТОМНОЕ РАСШИРЕНИЕ ДЛЯ INKDROP: Преобразование ![[filename]] -> ![filename](filename)
# --------------------------------------------------------------------
class InkdropImagePreprocessor(Preprocessor):
    RE = re.compile(r'!\[\[(.*?)\]\]')

    def run(self, lines):
        new_lines = []
        for line in lines:
            new_line = self.RE.sub(r'![\1](\1)', line)
            new_lines.append(new_line)
        return new_lines


class InkdropImageExtension(Extension):
    def extendMarkdown(self, md):
        md.preprocessors.register(InkdropImagePreprocessor(md), 'inkdrop_image', 25)


# -----------------------------------
#  ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ (SQLite)
# -----------------------------------
def init_db():
    app.logger.info('Инициализация базы данных')
    try:
        conn = sqlite3.connect(app.config['DATABASE'])
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                theme TEXT DEFAULT 'light'
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                document_id TEXT UNIQUE NOT NULL,
                filename TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        ''')
        conn.commit()
        conn.close()
    except Exception as e:
        app.logger.error(f'Ошибка инициализации БД: {str(e)}')
        raise


init_db()


# -----------------------------------------
#  ОЧИСТКА ПРОСРОЧЕННЫХ ДОКУМЕНТОВ (ФОНОВО)
# -----------------------------------------
def cleanup_expired_documents():
    """ Периодически удаляет истекшие документы и их директории. """
    while True:
        try:
            app.logger.info('Запуск процедуры очистки истекших документов')
            conn = sqlite3.connect(app.config['DATABASE'])
            cursor = conn.cursor()
            current_time = datetime.now()
            cursor.execute('SELECT document_id FROM documents WHERE expires_at < ?', (current_time,))
            expired_docs = cursor.fetchall()

            for doc in expired_docs:
                doc_id = doc[0]
                app.logger.info(f'Удаление истекшего документа: {doc_id}')
                doc_path = os.path.join(app.config['UPLOAD_FOLDER'], doc_id)
                if os.path.exists(doc_path):
                    shutil.rmtree(doc_path)
                cursor.execute('DELETE FROM documents WHERE document_id = ?', (doc_id,))

            conn.commit()
            conn.close()
            app.logger.info(f'Очистка завершена, удалено документов: {len(expired_docs)}')

        except Exception as e:
            app.logger.error(f'Ошибка при очистке истекших документов: {str(e)}')

        time.sleep(3600)  # Проверяем каждый час


cleanup_thread = threading.Thread(target=cleanup_expired_documents, daemon=True)
cleanup_thread.start()


# --------------------------------
#  ФУНКЦИИ ДЛЯ РАБОТЫ С MARKDOWN
# --------------------------------
def convert_markdown_to_html(markdown_text):
    """
    Преобразует Markdown в HTML с поддержкой Inkdrop-синтаксиса и рядом расширений.
    """
    try:
        md_instance = markdown.Markdown(
            extensions=[
                'tables',
                'toc',
                'fenced_code',
                'codehilite',
                'footnotes',
                'admonition',
                'def_list',
                'attr_list',
                'pymdownx.tasklist',
                InkdropImageExtension()
            ],
            extension_configs={
                'toc': {
                    'permalink': False
                },
                'footnotes': {
                    'BACKLINK_TEXT': "↩"
                },
                'codehilite': {
                    'noclasses': True,
                    'linenums': False
                },
                'pymdownx.tasklist': {
                    'clickable_checkbox': False,
                    'custom_checkbox': True
                }
            }
        )
        html_content = md_instance.convert(markdown_text)
        toc = getattr(md_instance, 'toc', '')
        return html_content, toc
    except Exception as e:
        app.logger.error(f'Ошибка при конвертации Markdown в HTML: {str(e)}')
        raise


def extract_headers(markdown_text):
    """ Извлекает заголовки (уровень, текст) для оглавления из Markdown. """
    headers = []
    try:
        lines = markdown_text.split('\n')
        for line in lines:
            if line.startswith('#'):
                match = re.match(r'^#+', line)
                if match:
                    level = len(match.group())
                    text = line.lstrip('#').strip()
                    headers.append((level, text))
    except Exception as e:
        app.logger.error(f'Ошибка при извлечении заголовков: {str(e)}')
    return headers


# --------------------
#  ПОЛУЧЕНИЕ ТЕМЫ UI
# --------------------
def get_user_theme():
    """ Получает предпочитаемую тему (light/dark) для текущего пользователя. """
    user_id = session.get('user_id')
    theme = 'light'
    if user_id:
        try:
            conn = sqlite3.connect(app.config['DATABASE'])
            cursor = conn.cursor()
            cursor.execute('SELECT theme FROM users WHERE id = ?', (user_id,))
            result = cursor.fetchone()
            conn.close()
            if result and result[0]:
                theme = result[0]
        except Exception as e:
            app.logger.error(f'Ошибка при получении темы пользователя: {str(e)}')
    return theme


# -----------------------------
#  СОХРАНЕНИЕ MARKDOWN-КОНТЕНТА
# -----------------------------
def generate_document_id():
    return str(uuid.uuid4())


def save_markdown_content(content, user_id=None, filename=None):
    """
    Сохраняет Markdown-контент в файловую систему и регистрирует документ в БД.
    Возвращает сгенерированный идентификатор документа (document_id).
    """
    try:
        document_id = generate_document_id()
        user_folder = os.path.join(app.config['UPLOAD_FOLDER'], document_id)
        os.makedirs(user_folder, exist_ok=True)

        # Генерируем имя .md файла, если не указано
        if not filename:
            timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
            filename = f"document_{timestamp}.md"
        else:
            # Добавляем .md, если не хватает
            if not filename.lower().endswith('.md'):
                filename += '.md'

        safe_filename = secure_filename(filename)
        markdown_path = os.path.join(user_folder, safe_filename)

        # Сохраняем содержимое
        with open(markdown_path, 'w', encoding='utf-8') as f:
            f.write(content)

        # Если пользователь не авторизован, документ будет действителен 3 дня
        expires_at = None
        if not user_id:
            expires_at = datetime.now() + timedelta(days=3)

        # Сохраняем запись в БД
        conn = sqlite3.connect(app.config['DATABASE'])
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO documents (user_id, document_id, filename, expires_at) VALUES (?, ?, ?, ?)',
            (user_id, document_id, safe_filename, expires_at)
        )
        conn.commit()
        conn.close()

        app.logger.info(f'Сохранен новый markdown документ: {document_id}, пользователь: {user_id}')
        return document_id

    except Exception as e:
        app.logger.error(f'Ошибка при сохранении Markdown контента: {str(e)}')
        raise


# -----------------------------------------------
#  ПРЕДОБРАБОТКА ЗАПРОСОВ (проверка сессии Flask)
# -----------------------------------------------
@app.before_request
def before_request():
    g.user = None
    user_id = session.get('user_id')
    if user_id:
        try:
            conn = sqlite3.connect(app.config['DATABASE'])
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM users WHERE id = ?', (user_id,))
            user = cursor.fetchone()
            conn.close()

            if user:
                g.user = {'id': user[0], 'username': user[1]}
            else:
                # Если пользователя нет в базе (удалён?), чистим сессию
                app.logger.warning(f'Пользователь с ID {user_id} не найден в БД, сессия очищена')
                session.pop('user_id', None)
                session.pop('username', None)
        except Exception as e:
            app.logger.error(f'Ошибка при проверке пользователя: {str(e)}')


# ----------------
#  ГЛАВНАЯ СТРАНИЦА
# ----------------
@app.route('/')
def index():
    theme = get_user_theme()
    app.logger.info(f'Запрос главной страницы, пользователь: {session.get("username", "Гость")}')
    return render_template('index.html', theme=theme)


# -----------------------------------
#  ЗАГРУЗКА Markdown-файла или текста
# -----------------------------------
@app.route('/upload', methods=['POST'])
def upload_file():
    try:
        # Расширенное логирование для отладки
        app.logger.info('=============== НАЧАЛО ЗАГРУЗКИ ФАЙЛА ===============')
        app.logger.info(f'Данные формы: {request.form}')
        app.logger.info(f'Файлы в запросе: {list(request.files.keys())}')

        # Вывод заголовков запроса
        app.logger.info('Заголовки запроса:')
        for header, value in request.headers:
            app.logger.info(f'  {header}: {value}')

        # Техническая информация
        app.logger.info(f'Content-Type: {request.content_type}')
        app.logger.info(f'Метод запроса: {request.method}')
        app.logger.info(f'Размер запроса: {request.content_length} байт')

        # Детальная информация о пришедших файлах
        for file_key in request.files:
            if file_key == 'markdown_file':
                file = request.files[file_key]
                if file and file.filename:
                    try:
                        content_preview = file.read(100)  # читаем первые 100 байт
                        file.seek(0)                     # сбрасываем указатель обратно
                        app.logger.info(
                            f'[DEBUG] markdown_file: "{file.filename}", preview: {repr(content_preview)[:50]}...'
                        )
                    except Exception as e:
                        app.logger.error(f'Ошибка при чтении markdown файла: {str(e)}')
                else:
                    app.logger.warning(f'markdown_file присутствует, но имя файла пустое: {file.filename}')

            elif file_key == 'additional_files':
                additional = request.files.getlist('additional_files')
                app.logger.info(f'[DEBUG] Кол-во дополнительных файлов: {len(additional)}')

        # Проверка, есть ли markdown_file
        has_markdown_file = (
            'markdown_file' in request.files
            and request.files['markdown_file'].filename
        )
        has_additional_files = False
        if 'additional_files' in request.files:
            for f in request.files.getlist('additional_files'):
                if f and f.filename:
                    has_additional_files = True
                    break

        app.logger.info(f'Проверка наличия файлов: markdown={has_markdown_file}, additional={has_additional_files}')

        # 1) Если пришёл именно .md файл
        if has_markdown_file:
            markdown_file = request.files['markdown_file']
            if markdown_file.filename == '':
                flash('Файл не выбран')
                return redirect(url_for('index'))

            document_id = generate_document_id()
            app.logger.info(f'Сгенерирован документ ID={document_id}')

            user_folder = os.path.join(app.config['UPLOAD_FOLDER'], document_id)
            os.makedirs(user_folder, exist_ok=True)

            # Сохраняем с учетом безопасности имени
            original_filename = markdown_file.filename
            safe_filename = secure_filename(original_filename)
            markdown_path = os.path.join(user_folder, safe_filename)

            # Сохраняем на диск
            markdown_file.save(markdown_path)
            app.logger.info(f'Markdown файл "{original_filename}" сохранен как "{safe_filename}"')

            # Дополнительные файлы
            additional_file_count = 0
            if has_additional_files:
                for file in request.files.getlist('additional_files'):
                    if file.filename:
                        original_add_fn = file.filename
                        safe_add_fn = secure_filename(original_add_fn)
                        file_path = os.path.join(user_folder, safe_add_fn)
                        file.save(file_path)
                        additional_file_count += 1
                        app.logger.info(f'  Доп. файл "{original_add_fn}" сохранен как "{safe_add_fn}"')

            app.logger.info(f'Всего дополнительных файлов: {additional_file_count}')

            # Запись в БД
            user_id = session.get('user_id')
            expires_at = None
            if not user_id:
                expires_at = datetime.now() + timedelta(days=3)

            conn = sqlite3.connect(app.config['DATABASE'])
            cursor = conn.cursor()
            cursor.execute(
                'INSERT INTO documents (user_id, document_id, filename, expires_at) VALUES (?, ?, ?, ?)',
                (user_id, document_id, safe_filename, expires_at)
            )
            conn.commit()
            conn.close()

            # Проверка флага edit_after
            if 'edit_after' in request.form and request.form['edit_after'] == 'true':
                return redirect(url_for('edit_document', document_id=document_id))

            app.logger.info('=============== КОНЕЦ ЗАГРУЗКИ ФАЙЛА ===============')
            return redirect(url_for('publish_document', document_id=document_id))

        # 2) Если пришёл текстовый контент (Ctrl+V и т.п.)
        elif 'markdown_content' in request.form and request.form['markdown_content'].strip():
            content = request.form['markdown_content']
            filename = request.form.get('filename', '')
            is_paste_action = (request.form.get('paste_action') == 'true')
            user_id = session.get('user_id')

            document_id = save_markdown_content(content, user_id, filename)

            # Если вставка или указан edit_after — редактируем сразу
            if is_paste_action or ('edit_after' in request.form and request.form['edit_after'] == 'true'):
                return redirect(url_for('edit_document', document_id=document_id))

            return redirect(url_for('publish_document', document_id=document_id))

        # 3) Если есть только дополнительные файлы без markdown
        elif has_additional_files:
            flash('Необходимо загрузить markdown файл вместе с дополнительными файлами')
            return redirect(url_for('index'))
        else:
            flash('Необходимо загрузить файл или ввести текст')
            return redirect(url_for('index'))

    except Exception as e:
        app.logger.error(f'Ошибка при загрузке файла: {str(e)}')
        flash('Произошла ошибка при загрузке файла')
        return redirect(url_for('index'))


# -----------------------
#  СТРАНИЦА ПУБЛИКАЦИИ
# -----------------------
@app.route('/publish/<document_id>')
def publish_document(document_id):
    try:
        conn = sqlite3.connect(app.config['DATABASE'])
        cursor = conn.cursor()
        cursor.execute('SELECT filename, expires_at FROM documents WHERE document_id = ?', (document_id,))
        result = cursor.fetchone()
        conn.close()

        if not result:
            flash('Документ не найден')
            return redirect(url_for('index'))

        filename, expires_at = result
        document_url = url_for('view_document', document_id=document_id, _external=True)

        expiration_info = None
        if expires_at:
            expiration_date = datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S.%f")
            days_left = (expiration_date - datetime.now()).days
            expiration_info = f"Ваш документ будет доступен в течение {days_left} дней без регистрации"

        theme = get_user_theme()
        return render_template('publish.html', document_url=document_url, expiration_info=expiration_info, theme=theme)

    except Exception as e:
        app.logger.error(f'Ошибка при публикации документа: {str(e)}')
        flash('Произошла ошибка при публикации документа')
        return redirect(url_for('index'))


# --------------------
#  ПРОСМОТР ДОКУМЕНТА
# --------------------
@app.route('/view/<document_id>')
def view_document(document_id):
    try:
        conn = sqlite3.connect(app.config['DATABASE'])
        cursor = conn.cursor()
        cursor.execute('SELECT filename, user_id FROM documents WHERE document_id = ?', (document_id,))
        result = cursor.fetchone()
        conn.close()

        if not result:
            flash('Документ не найден')
            return redirect(url_for('index'))

        filename, user_id = result
        document_folder = os.path.join(app.config['UPLOAD_FOLDER'], document_id)
        file_list = os.listdir(document_folder) if os.path.exists(document_folder) else []

        # Читаем markdown
        markdown_path = os.path.join(document_folder, filename)
        with open(markdown_path, 'r', encoding='utf-8') as f:
            markdown_content = f.read()

        html_content, toc = convert_markdown_to_html(markdown_content)
        headers = extract_headers(markdown_content)

        # Заменяем пути к картинкам на правильные URL
        for file in file_list:
            if file.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.svg')):
                html_content = html_content.replace(
                    f'src="{file}"',
                    f'src="{url_for("document_file", document_id=document_id, filename=file)}"'
                )

        # Проверяем права на редактирование
        can_edit = False
        show_file_structure = False
        current_user_id = session.get('user_id')
        if current_user_id and (current_user_id == user_id or user_id is None):
            can_edit = True
            show_file_structure = True
            # Загружаем список файлов пользователя, если нужно
            conn = sqlite3.connect(app.config['DATABASE'])
            cursor = conn.cursor()
            cursor.execute('SELECT document_id, filename FROM documents WHERE user_id = ?', (current_user_id,))
            user_files = cursor.fetchall()
            conn.close()
        else:
            user_files = []

        theme = get_user_theme()
        return render_template(
            'view.html',
            html_content=html_content,
            toc=toc,
            headers=headers,
            document_id=document_id,
            show_file_structure=show_file_structure,
            user_files=user_files,
            can_edit=can_edit,
            theme=theme
        )

    except Exception as e:
        app.logger.error(f'Ошибка при просмотре документа: {str(e)}')
        flash('Произошла ошибка при просмотре документа')
        return redirect(url_for('index'))


# ----------------------------
#  ВЫДАЧА ФАЙЛОВ ДОКУМЕНТА
# ----------------------------
@app.route('/file/<document_id>/<filename>')
def document_file(document_id, filename):
    try:
        document_folder = os.path.join(app.config['UPLOAD_FOLDER'], document_id)
        app.logger.info(f'Запрос файла: {filename} из документа {document_id}')
        return send_from_directory(document_folder, filename)
    except Exception as e:
        app.logger.error(f'Ошибка при доступе к файлу: {str(e)}')
        return "Файл не найден", 404


# ----------------------------
#  РЕДАКТИРОВАНИЕ ДОКУМЕНТА
# ----------------------------
@app.route('/edit/<document_id>')
def edit_document(document_id):
    try:
        conn = sqlite3.connect(app.config['DATABASE'])
        cursor = conn.cursor()
        cursor.execute('SELECT filename, user_id FROM documents WHERE document_id = ?', (document_id,))
        result = cursor.fetchone()
        conn.close()

        if not result:
            flash('Документ не найден')
            return redirect(url_for('index'))

        filename, user_id = result
        current_user_id = session.get('user_id')

        # Проверка прав: либо не привязан, либо пользователь = владелец
        if user_id is not None and current_user_id != user_id:
            flash('У вас нет прав для редактирования этого документа')
            return redirect(url_for('view_document', document_id=document_id))

        # Загружаем текст для редактирования
        document_folder = os.path.join(app.config['UPLOAD_FOLDER'], document_id)
        markdown_path = os.path.join(document_folder, filename)
        with open(markdown_path, 'r', encoding='utf-8') as f:
            markdown_content = f.read()

        theme = get_user_theme()
        return render_template(
            'edit.html',
            document_id=document_id,
            filename=filename,
            markdown_content=markdown_content,
            theme=theme
        )

    except Exception as e:
        app.logger.error(f'Ошибка при редактировании документа: {str(e)}')
        flash('Произошла ошибка при редактировании документа')
        return redirect(url_for('index'))


# ----------------------------
#  СОХРАНЕНИЕ ДОКУМЕНТА (AJAX)
# ----------------------------
@app.route('/save/<document_id>', methods=['POST'])
def save_document(document_id):
    try:
        conn = sqlite3.connect(app.config['DATABASE'])
        cursor = conn.cursor()
        cursor.execute('SELECT filename, user_id FROM documents WHERE document_id = ?', (document_id,))
        result = cursor.fetchone()
        conn.close()

        if not result:
            return jsonify({'success': False, 'message': 'Документ не найден'})

        filename, user_id = result
        current_user_id = session.get('user_id')

        # Проверка прав
        if user_id is not None and current_user_id != user_id:
            return jsonify({'success': False, 'message': 'У вас нет прав для редактирования этого документа'})

        markdown_content = request.form.get('content', '')
        document_folder = os.path.join(app.config['UPLOAD_FOLDER'], document_id)
        markdown_path = os.path.join(document_folder, filename)

        # Перезаписываем файл
        with open(markdown_path, 'w', encoding='utf-8') as f:
            f.write(markdown_content)

        app.logger.info(f'Документ успешно сохранен: {document_id}')
        return jsonify({'success': True, 'message': 'Документ успешно сохранен'})

    except Exception as e:
        app.logger.error(f'Ошибка при сохранении документа: {str(e)}')
        return jsonify({'success': False, 'message': f'Ошибка при сохранении документа: {str(e)}'})


# -------------------
#  ПЕРЕКЛЮЧЕНИЕ ТЕМЫ
# -------------------
@app.route('/toggle-theme', methods=['POST'])
def toggle_theme():
    try:
        if 'user_id' not in session:
            return jsonify({'success': False, 'message': 'Необходимо войти в систему'})

        user_id = session['user_id']
        new_theme = request.form.get('theme', 'light')

        conn = sqlite3.connect(app.config['DATABASE'])
        cursor = conn.cursor()
        cursor.execute('UPDATE users SET theme = ? WHERE id = ?', (new_theme, user_id))
        conn.commit()
        conn.close()

        app.logger.info(f'Тема изменена для пользователя {user_id}: {new_theme}')
        return jsonify({'success': True, 'theme': new_theme})

    except Exception as e:
        app.logger.error(f'Ошибка при изменении темы: {str(e)}')
        return jsonify({'success': False, 'message': 'Ошибка при изменении темы'})


# ------------------------------------------------------
#  ЗАГРУЗКА ДОПОЛНИТЕЛЬНЫХ ФАЙЛОВ ДЛЯ СУЩЕСТВУЮЩЕГО ДОКА
# ------------------------------------------------------
@app.route('/upload_additional/<document_id>', methods=['POST'])
def upload_additional_files(document_id):
    try:
        # Проверяем права (автор документа)
        conn = sqlite3.connect(app.config['DATABASE'])
        cursor = conn.cursor()
        cursor.execute('SELECT user_id FROM documents WHERE document_id = ?', (document_id,))
        result = cursor.fetchone()
        conn.close()

        if not result:
            return jsonify({'success': False, 'message': 'Документ не найден'})

        user_id = result[0]
        current_user_id = session.get('user_id')
        if user_id is not None and current_user_id != user_id:
            return jsonify({'success': False, 'message': 'У вас нет прав для изменения этого документа'})

        if 'files[]' not in request.files:
            return jsonify({'success': False, 'message': 'Не выбраны файлы для загрузки'})

        # Сохраняем файлы в папку документа
        document_folder = os.path.join(app.config['UPLOAD_FOLDER'], document_id)
        os.makedirs(document_folder, exist_ok=True)

        uploaded_files = []
        files = request.files.getlist('files[]')
        for file in files:
            if file and file.filename:
                original_filename = file.filename
                safe_filename = secure_filename(original_filename)
                file_path = os.path.join(document_folder, safe_filename)
                file.save(file_path)
                uploaded_files.append(safe_filename)
                app.logger.info(f'Загружен дополнительный файл {safe_filename} для документа {document_id}')

        if not uploaded_files:
            return jsonify({'success': False, 'message': 'Не удалось загрузить файлы'})

        return jsonify({'success': True, 'message': 'Файлы успешно загружены', 'files': uploaded_files})

    except Exception as e:
        app.logger.error(f'Ошибка при загрузке дополнительных файлов: {str(e)}')
        return jsonify({'success': False, 'message': f'Ошибка при загрузке файлов: {str(e)}'})


# -------------------------------------
#  ПОЛУЧЕНИЕ СПИСКА ФАЙЛОВ ДОКУМЕНТА
# -------------------------------------
@app.route('/document_files/<document_id>', methods=['GET'])
def get_document_files(document_id):
    try:
        conn = sqlite3.connect(app.config['DATABASE'])
        cursor = conn.cursor()
        cursor.execute('SELECT user_id FROM documents WHERE document_id = ?', (document_id,))
        result = cursor.fetchone()
        conn.close()

        if not result:
            return jsonify({'success': False, 'message': 'Документ не найден'})

        user_id = result[0]
        current_user_id = session.get('user_id')
        if user_id is not None and current_user_id != user_id:
            return jsonify({'success': False, 'message': 'У вас нет прав для просмотра файлов этого документа'})

        # Собираем список файлов
        document_folder = os.path.join(app.config['UPLOAD_FOLDER'], document_id)
        if not os.path.exists(document_folder):
            return jsonify({'success': True, 'files': []})

        all_files = os.listdir(document_folder)
        media_files = [f for f in all_files if f.lower().endswith(
            ('.png', '.jpg', '.jpeg', '.gif', '.svg', '.mp4', '.webp', '.pdf')
        )]

        return jsonify({'success': True, 'files': media_files})

    except Exception as e:
        app.logger.error(f'Ошибка при получении списка файлов документа: {str(e)}')
        return jsonify({'success': False, 'message': f'Ошибка при получении списка файлов: {str(e)}'})


# ------------------------------
#  УДАЛЕНИЕ ДОКУМЕНТА /delete
# ------------------------------
@app.route('/delete/<document_id>', methods=['POST'])
def delete_document(document_id):
    try:
        if 'user_id' not in session:
            flash('Необходимо войти в систему')
            return redirect(url_for('login'))

        user_id = session['user_id']
        conn = sqlite3.connect(app.config['DATABASE'])
        cursor = conn.cursor()
        cursor.execute('SELECT user_id FROM documents WHERE document_id = ?', (document_id,))
        result = cursor.fetchone()

        if not result:
            conn.close()
            flash('Документ не найден')
            return redirect(url_for('profile'))

        if result[0] != user_id:
            conn.close()
            flash('У вас нет прав для удаления этого документа')
            return redirect(url_for('profile'))

        # Удаляем запись из БД
        cursor.execute('DELETE FROM documents WHERE document_id = ?', (document_id,))
        conn.commit()
        conn.close()

        # Удаляем файлы
        doc_path = os.path.join(app.config['UPLOAD_FOLDER'], document_id)
        if os.path.exists(doc_path):
            shutil.rmtree(doc_path)

        app.logger.info(f'Документ успешно удален: {document_id}')
        flash('Документ успешно удален')
        return redirect(url_for('profile'))

    except Exception as e:
        app.logger.error(f'Ошибка при удалении документа: {str(e)}')
        flash('Произошла ошибка при удалении документа')
        return redirect(url_for('profile'))


# ----------------
#  РЕГИСТРАЦИЯ
# ----------------
@app.route('/register', methods=['GET', 'POST'])
def register():
    try:
        if request.method == 'POST':
            username = request.form['username']
            password = request.form['password']
            confirm_password = request.form['confirm_password']

            # Проверяем совпадение паролей
            if password != confirm_password:
                flash('Пароли не совпадают!')
                return redirect(url_for('register'))

            conn = sqlite3.connect(app.config['DATABASE'])
            cursor = conn.cursor()

            # Проверяем, нет ли уже такого пользователя
            cursor.execute('SELECT id FROM users WHERE username = ?', (username,))
            if cursor.fetchone():
                conn.close()
                flash('Пользователь с таким именем уже существует')
                return redirect(url_for('register'))

            # Хешируем пароль
            hashed_password = generate_password_hash(password)
            cursor.execute('INSERT INTO users (username, password) VALUES (?, ?)', (username, hashed_password))
            conn.commit()

            cursor.execute('SELECT id FROM users WHERE username = ?', (username,))
            user_id = cursor.fetchone()[0]
            conn.close()

            # Ставим данные сессии
            session['user_id'] = user_id
            session['username'] = username
            session.permanent = True

            flash('Регистрация успешна!')
            return redirect(url_for('index'))

        theme = get_user_theme()
        return render_template('register.html', theme=theme)

    except Exception as e:
        app.logger.error(f'Ошибка при регистрации: {str(e)}')
        flash('Произошла ошибка при регистрации')
        return redirect(url_for('index'))


# -----------
#  ВЫХОД
# -----------
@app.route('/logout')
def logout():
    try:
        username = session.get('username', 'Неизвестный')
        session.pop('user_id', None)
        session.pop('username', None)
        app.logger.info(f'Пользователь вышел из системы: {username}')
        flash('Вы вышли из системы')
        return redirect(url_for('index'))
    except Exception as e:
        app.logger.error(f'Ошибка при выходе из системы: {str(e)}')
        return redirect(url_for('index'))


# -----------
#  ПРОФИЛЬ
# -----------
@app.route('/profile')
def profile():
    try:
        if 'user_id' not in session:
            flash('Необходимо войти в систему')
            return redirect(url_for('login'))

        user_id = session['user_id']

        conn = sqlite3.connect(app.config['DATABASE'])
        cursor = conn.cursor()
        cursor.execute('''
            SELECT document_id, filename, created_at, expires_at
            FROM documents
            WHERE user_id = ?
        ''', (user_id,))
        documents = cursor.fetchall()
        conn.close()

        total_size = 0
        formatted_documents = []

        for doc_id, filename, created_at, expires_at in documents:
            doc_path = os.path.join(app.config['UPLOAD_FOLDER'], doc_id)
            size = 0
            preview_url = None

            # Считаем суммарный размер
            if os.path.exists(doc_path):
                for dirpath, _, filenames in os.walk(doc_path):
                    for f in filenames:
                        fp = os.path.join(dirpath, f)
                        size += os.path.getsize(fp)
                        if not preview_url and f.lower().endswith(('.png', '.jpg', '.jpeg', '.gif')):
                            preview_url = url_for('document_file', document_id=doc_id, filename=f)

            total_size += size
            formatted_documents.append((doc_id, filename, created_at, expires_at, preview_url))

        total_size_mb = total_size / (1024 * 1024)
        storage_limit_mb = 256
        storage_left_mb = max(0, storage_limit_mb - total_size_mb)

        theme = get_user_theme()
        return render_template(
            'profile.html',
            documents=formatted_documents,
            total_size_mb=total_size_mb,
            storage_limit_mb=storage_limit_mb,
            storage_left_mb=storage_left_mb,
            theme=theme
        )

    except Exception as e:
        app.logger.error(f'Ошибка при просмотре профиля: {str(e)}')
        flash('Произошла ошибка при загрузке профиля')
        return redirect(url_for('index'))


# -----------
#  СОЗДАНИЕ
# -----------
@app.route('/create', methods=['GET'])
def create_document():
    try:
        theme = get_user_theme()
        return render_template('create.html', theme=theme)
    except Exception as e:
        app.logger.error(f'Ошибка при открытии страницы создания: {str(e)}')
        flash('Произошла ошибка при открытии страницы создания')
        return redirect(url_for('index'))


# -------------------
#  ВХОД В СИСТЕМУ
# -------------------
@app.route('/login', methods=['GET', 'POST'])
def login():
    try:
        if request.method == 'POST':
            username = request.form['username']
            password = request.form['password']

            conn = sqlite3.connect(app.config['DATABASE'])
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM users WHERE username = ?', (username,))
            user = cursor.fetchone()
            conn.close()

            if user and check_password_hash(user[2], password):
                session['user_id'] = user[0]
                session['username'] = user[1]
                session.permanent = True
                flash('Вы успешно вошли в систему!')
                return redirect(url_for('index'))
            else:
                flash('Неверное имя пользователя или пароль')
                return redirect(url_for('login'))

        theme = get_user_theme()
        return render_template('login.html', theme=theme)

    except Exception as e:
        app.logger.error(f'Ошибка при входе в систему: {str(e)}')
        flash('Произошла ошибка при входе в систему')
        return redirect(url_for('index'))


# ----------------------
#  ТОЧКА ЗАПУСКА СЕРВЕРА
# ----------------------
if __name__ == '__main__':
    app.run(debug=True)
