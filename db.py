import os
import sqlite3
import shutil
import threading
import time
import logging
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

# Настраиваем логгер отдельно, если нужно
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
file_handler = RotatingFileHandler('mark2web_db.log', maxBytes=10485760, backupCount=10)
logger.addHandler(file_handler)

DATABASE_PATH = 'database.db'
UPLOAD_FOLDER = 'uploads'

def init_db():
    """Инициализация таблиц в базе данных (если их нет)."""
    logger.info('Инициализация базы данных')
    try:
        conn = sqlite3.connect(DATABASE_PATH)
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
    except Exception as e:
        logger.error(f'Ошибка инициализации БД: {str(e)}')
        raise

def cleanup_expired_documents():
    """
    Фоновая задача, периодически удаляет истекшие документы из базы
    и их директории из UPLOAD_FOLDER.
    Запускается в отдельном потоке.
    """
    while True:
        try:
            logger.info('Запуск процедуры очистки истекших документов')
            conn = sqlite3.connect(DATABASE_PATH)
            cursor = conn.cursor()
            current_time = datetime.now()
            cursor.execute(
                'SELECT document_id FROM documents WHERE expires_at < ?',
                (current_time,)
            )
            expired_docs = cursor.fetchall()

            for doc in expired_docs:
                doc_id = doc[0]
                logger.info(f'Удаление истекшего документа: {doc_id}')
                doc_path = os.path.join(UPLOAD_FOLDER, doc_id)
                if os.path.exists(doc_path):
                    shutil.rmtree(doc_path, ignore_errors=True)
                cursor.execute(
                    'DELETE FROM documents WHERE document_id = ?',
                    (doc_id,)
                )

            conn.commit()
            conn.close()
            logger.info(f'Очистка завершена, удалено документов: {len(expired_docs)}')
        except Exception as e:
            logger.error(f'Ошибка при очистке истекших документов: {str(e)}')

        time.sleep(3600)  # Проверяем каждый час

def start_cleanup_thread():
    """Запустить поток очистки (вызывается из основного кода один раз)."""
    thread = threading.Thread(target=cleanup_expired_documents, daemon=True)
    thread.start()

def get_connection():
    """
    Возвращает соединение с базой данных.
    В больших приложениях можно использовать что-то вроде SQLAlchemy или Flask g.
    """
    return sqlite3.connect(DATABASE_PATH)
