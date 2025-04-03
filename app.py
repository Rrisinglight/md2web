import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta

import markdown
import pymdownx.tasklist
from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, session, send_from_directory, jsonify
)
from markdown.extensions import Extension
from markdown.preprocessors import Preprocessor
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.urandom(8)
app.config['UPLOAD_FOLDER'] = '/root/mark2web/uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size
app.config['DATABASE'] = '/root/mark2web/database.db'
app.config['SERVER_URL'] = '202.181.188.118'  # Используем ваш сервер
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)  # Увеличиваем время жизни сессии

# Создаем директорию для загрузок, если её нет
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)


# --- Кастомное расширение для Inkdrop синтаксиса изображений --- #
class InkdropImagePreprocessor(Preprocessor):
    """Преобразует синтаксис ![[filename]] в стандартный Markdown-вид."""
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


# --- Инициализация базы данных --- #
def init_db():
    conn = sqlite3.connect(app.config['DATABASE'])
    cursor = conn.cursor()
    cursor.execute(
        '''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            theme TEXT DEFAULT 'light'
        )
        '''
    )
    cursor.execute(
        '''
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            document_id TEXT UNIQUE NOT NULL,
            filename TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
        '''
    )
    conn.commit()
    conn.close()


init_db()


def generate_document_id():
    """Генерирует уникальный ID документа."""
    return str(uuid.uuid4())


def cleanup_expired_documents():
    """Периодически удаляет истекшие документы и их директории."""
    while True:
        conn = sqlite3.connect(app.config['DATABASE'])
        cursor = conn.cursor()
        current_time = datetime.now()
        cursor.execute(
            'SELECT document_id FROM documents WHERE expires_at < ?',
            (current_time,)
        )
        expired_docs = cursor.fetchall()

        for doc in expired_docs:
            doc_id = doc[0]
            doc_path = os.path.join(app.config['UPLOAD_FOLDER'], doc_id)
            if os.path.exists(doc_path):
                shutil.rmtree(doc_path)
            cursor.execute(
                'DELETE FROM documents WHERE document_id = ?',
                (doc_id,)
            )

        conn.commit()
        conn.close()
        time.sleep(3600)  # Проверяем каждый час


cleanup_thread = threading.Thread(
    target=cleanup_expired_documents,
    daemon=True
)
cleanup_thread.start()


def convert_markdown_to_html(markdown_text):
    """
    Преобразует Markdown в HTML с поддержкой Inkdrop-синтаксиса.
    Добавлены необходимые расширения и кастомное расширение для обработки
    конструкции ![[filename]].
    """
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
                'permalink': False  # Отключаем символ "¶" в заголовках
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


def extract_headers(markdown_text):
    """Извлекает заголовки для оглавления из Markdown текста."""
    headers = []
    lines = markdown_text.split('\n')
    for line in lines:
        if line.startswith('#'):
            match = re.match(r'^#+', line)
            if match:
                level = len(match.group())
                text = line.lstrip('#').strip()
                headers.append((level, text))
    return headers


def get_user_theme():
    """Получает предпочтительную тему пользователя"""
    user_id = session.get('user_id')
    theme = 'light'  # По умолчанию светлая тема
    
    if user_id:
        conn = sqlite3.connect(app.config['DATABASE'])
        cursor = conn.cursor()
        cursor.execute('SELECT theme FROM users WHERE id = ?', (user_id,))
        result = cursor.fetchone()
        conn.close()
        
        if result and result[0]:
            theme = result[0]
    
    return theme


@app.route('/')
def index():
    theme = get_user_theme()
    return render_template('index.html', theme=theme)


@app.route('/upload', methods=['POST'])
def upload_file():
    if 'markdown_file' not in request.files:
        flash('Файл не выбран')
        return redirect(request.url)

    markdown_file = request.files['markdown_file']

    if markdown_file.filename == '':
        flash('Файл не выбран')
        return redirect(request.url)

    document_id = generate_document_id()
    user_folder = os.path.join(app.config['UPLOAD_FOLDER'], document_id)
    os.makedirs(user_folder, exist_ok=True)

    filename = secure_filename(markdown_file.filename)
    markdown_path = os.path.join(user_folder, filename)
    markdown_file.save(markdown_path)

    additional_files = request.files.getlist('additional_files')
    for file in additional_files:
        if file.filename:
            file_path = os.path.join(user_folder, secure_filename(file.filename))
            file.save(file_path)

    user_id = session.get('user_id')
    conn = sqlite3.connect(app.config['DATABASE'])
    cursor = conn.cursor()

    expires_at = None
    if not user_id:
        expires_at = datetime.now() + timedelta(days=3)

    cursor.execute(
        'INSERT INTO documents (user_id, document_id, filename, expires_at) '
        'VALUES (?, ?, ?, ?)',
        (user_id, document_id, filename, expires_at)
    )
    conn.commit()
    conn.close()

    return redirect(url_for('publish_document', document_id=document_id))


@app.route('/publish/<document_id>')
def publish_document(document_id):
    conn = sqlite3.connect(app.config['DATABASE'])
    cursor = conn.cursor()
    cursor.execute(
        'SELECT filename, expires_at FROM documents WHERE document_id = ?',
        (document_id,)
    )
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
        expiration_info = (
            f"Ваш документ будет доступен в течение {days_left} дней без регистрации"
        )

    theme = get_user_theme()
    return render_template(
        'publish.html',
        document_url=document_url,
        expiration_info=expiration_info,
        theme=theme
    )


@app.route('/view/<document_id>')
def view_document(document_id):
    conn = sqlite3.connect(app.config['DATABASE'])
    cursor = conn.cursor()
    cursor.execute(
        'SELECT filename, user_id FROM documents WHERE document_id = ?',
        (document_id,)
    )
    result = cursor.fetchone()
    conn.close()

    if not result:
        flash('Документ не найден')
        return redirect(url_for('index'))

    filename, user_id = result
    document_folder = os.path.join(app.config['UPLOAD_FOLDER'], document_id)
    file_list = os.listdir(document_folder) if os.path.exists(document_folder) else []

    markdown_path = os.path.join(document_folder, filename)
    with open(markdown_path, 'r', encoding='utf-8') as f:
        markdown_content = f.read()

    html_content, toc = convert_markdown_to_html(markdown_content)
    headers = extract_headers(markdown_content)

    # Заменяем пути к изображениям на правильные URL
    for file in file_list:
        if file.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.svg')):
            html_content = html_content.replace(
                f'src="{file}"',
                f'src="{url_for("document_file", document_id=document_id, filename=file)}"'
            )

    show_file_structure = False
    user_files = []
    current_user_id = session.get('user_id')
    can_edit = False

    if current_user_id and (current_user_id == user_id or user_id is None):
        show_file_structure = True
        can_edit = True
        conn = sqlite3.connect(app.config['DATABASE'])
        cursor = conn.cursor()
        cursor.execute(
            'SELECT document_id, filename FROM documents WHERE user_id = ?',
            (current_user_id,)
        )
        user_files = cursor.fetchall()
        conn.close()

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


@app.route('/file/<document_id>/<filename>')
def document_file(document_id, filename):
    document_folder = os.path.join(app.config['UPLOAD_FOLDER'], document_id)
    return send_from_directory(document_folder, filename)


@app.route('/edit/<document_id>')
def edit_document(document_id):
    if 'user_id' not in session:
        flash('Необходимо войти в систему для редактирования документов')
        return redirect(url_for('login'))

    conn = sqlite3.connect(app.config['DATABASE'])
    cursor = conn.cursor()
    cursor.execute(
        'SELECT filename, user_id FROM documents WHERE document_id = ?',
        (document_id,)
    )
    result = cursor.fetchone()
    conn.close()

    if not result:
        flash('Документ не найден')
        return redirect(url_for('index'))

    filename, user_id = result
    if user_id != session['user_id']:
        flash('У вас нет прав для редактирования этого документа')
        return redirect(url_for('view_document', document_id=document_id))

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


@app.route('/save/<document_id>', methods=['POST'])
def save_document(document_id):
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': 'Необходимо войти в систему'})

    conn = sqlite3.connect(app.config['DATABASE'])
    cursor = conn.cursor()
    cursor.execute(
        'SELECT filename, user_id FROM documents WHERE document_id = ?',
        (document_id,)
    )
    result = cursor.fetchone()
    conn.close()

    if not result:
        return jsonify({'success': False, 'message': 'Документ не найден'})

    filename, user_id = result
    if user_id != session['user_id']:
        return jsonify({'success': False, 'message': 'У вас нет прав для редактирования этого документа'})

    markdown_content = request.form.get('content', '')
    document_folder = os.path.join(app.config['UPLOAD_FOLDER'], document_id)
    markdown_path = os.path.join(document_folder, filename)

    with open(markdown_path, 'w', encoding='utf-8') as f:
        f.write(markdown_content)

    return jsonify({'success': True, 'message': 'Документ успешно сохранен'})


@app.route('/toggle-theme', methods=['POST'])
def toggle_theme():
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': 'Необходимо войти в систему'})

    user_id = session['user_id']
    new_theme = request.form.get('theme', 'light')
    
    conn = sqlite3.connect(app.config['DATABASE'])
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET theme = ? WHERE id = ?', (new_theme, user_id))
    conn.commit()
    conn.close()

    return jsonify({'success': True, 'theme': new_theme})


@app.route('/delete/<document_id>', methods=['POST'])
def delete_document(document_id):
    if 'user_id' not in session:
        flash('Необходимо войти в систему')
        return redirect(url_for('login'))

    user_id = session['user_id']
    conn = sqlite3.connect(app.config['DATABASE'])
    cursor = conn.cursor()
    cursor.execute(
        'SELECT user_id FROM documents WHERE document_id = ?',
        (document_id,)
    )
    result = cursor.fetchone()

    if not result:
        conn.close()
        flash('Документ не найден')
        return redirect(url_for('profile'))

    if result[0] != user_id:
        conn.close()
        flash('У вас нет прав для удаления этого документа')
        return redirect(url_for('profile'))

    # Удаляем запись из базы данных
    cursor.execute('DELETE FROM documents WHERE document_id = ?', (document_id,))
    conn.commit()
    conn.close()

    # Удаляем файлы документа
    doc_path = os.path.join(app.config['UPLOAD_FOLDER'], document_id)
    if os.path.exists(doc_path):
        shutil.rmtree(doc_path)

    flash('Документ успешно удален')
    return redirect(url_for('profile'))


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        confirm_password = request.form['confirm_password']
        
        # Проверка совпадения паролей
        if password != confirm_password:
            flash('Пароли не совпадают!')
            return redirect(url_for('register'))

        conn = sqlite3.connect(app.config['DATABASE'])
        cursor = conn.cursor()
        cursor.execute(
            'SELECT id FROM users WHERE username = ?',
            (username,)
        )
        if cursor.fetchone():
            conn.close()
            flash('Пользователь с таким именем уже существует')
            return redirect(url_for('register'))

        hashed_password = generate_password_hash(password)
        cursor.execute(
            'INSERT INTO users (username, password) VALUES (?, ?)',
            (username, hashed_password)
        )
        conn.commit()

        cursor.execute(
            'SELECT id FROM users WHERE username = ?',
            (username,)
        )
        user_id = cursor.fetchone()[0]
        conn.close()

        # Установка данных сессии
        session['user_id'] = user_id
        session['username'] = username
        session.permanent = True  # Делаем сессию постоянной

        flash('Регистрация успешна!')
        return redirect(url_for('index'))

    theme = get_user_theme()
    return render_template('register.html', theme=theme)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        conn = sqlite3.connect(app.config['DATABASE'])
        cursor = conn.cursor()
        cursor.execute(
            'SELECT id, password FROM users WHERE username = ?',
            (username,)
        )
        result = cursor.fetchone()
        conn.close()

        if not result:
            flash('Неверное имя пользователя или пароль')
            return redirect(url_for('login'))

        user_id, hashed_password = result
        if check_password_hash(hashed_password, password):
            session['user_id'] = user_id
            session['username'] = username
            session.permanent = True  # Делаем сессию постоянной
            flash('Вы успешно вошли в систему')
            return redirect(url_for('index'))
        else:
            flash('Неверное имя пользователя или пароль')
            return redirect(url_for('login'))

    theme = get_user_theme()
    return render_template('login.html', theme=theme)


@app.route('/logout')
def logout():
    session.pop('user_id', None)
    session.pop('username', None)
    flash('Вы вышли из системы')
    return redirect(url_for('index'))


@app.route('/profile')
def profile():
    if 'user_id' not in session:
        flash('Необходимо войти в систему')
        return redirect(url_for('login'))

    user_id = session['user_id']

    conn = sqlite3.connect(app.config['DATABASE'])
    cursor = conn.cursor()
    cursor.execute(
        'SELECT document_id, filename, created_at, expires_at FROM documents '
        'WHERE user_id = ?',
        (user_id,)
    )
    documents = cursor.fetchall()
    conn.close()

    total_size = 0
    formatted_documents = []
    for doc in documents:
        doc_id, filename, created_at, expires_at = doc
        doc_path = os.path.join(app.config['UPLOAD_FOLDER'], doc_id)
        size = 0
        preview_url = None
        
        if os.path.exists(doc_path):
            for dirpath, _, filenames in os.walk(doc_path):
                for f in filenames:
                    fp = os.path.join(dirpath, f)
                    file_size = os.path.getsize(fp)
                    size += file_size
                    
                    # Поиск подходящего изображения для превью
                    if not preview_url and f.lower().endswith(('.png', '.jpg', '.jpeg', '.gif')):
                        preview_url = url_for('document_file', document_id=doc_id, filename=f)
        
        total_size += size
        formatted_documents.append((doc_id, filename, created_at, expires_at, preview_url))

    total_size_mb = total_size / (1024 * 1024)
    storage_limit_mb = 256  # 256MB лимит
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


if __name__ == '__main__':
    app.run(debug=True)