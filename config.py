import os
import secrets

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


def _load_env(path):
    """Читает .env (KEY=VALUE) в os.environ, не перезаписывая уже заданные переменные."""
    if not os.path.exists(path):
        return
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env(os.path.join(BASE_DIR, '.env'))

DATA_DIR = os.path.abspath(os.environ.get('DATA_DIR') or os.path.join(BASE_DIR, 'data'))
# Хранилище — обычная папка или целый диск (например D:\). Если не задано в .env,
# путь спрашивается при первом запуске и хранится в базе.
STORAGE_DIR = os.path.abspath(os.environ['STORAGE_DIR']) if os.environ.get('STORAGE_DIR') else None
os.makedirs(DATA_DIR, exist_ok=True)


def _secret_key():
    """SECRET_KEY из .env, иначе генерируется один раз и хранится в data/secret.key."""
    if os.environ.get('SECRET_KEY'):
        return os.environ['SECRET_KEY']
    key_file = os.path.join(DATA_DIR, 'secret.key')
    if os.path.exists(key_file):
        with open(key_file, encoding='utf-8') as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(key_file, 'w', encoding='utf-8') as f:
        f.write(key)
    return key


class Config:
    SECRET_KEY = _secret_key()
    SQLALCHEMY_DATABASE_URI = 'sqlite:///' + os.path.join(DATA_DIR, 'cloud_disk.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    STORAGE_DIR = STORAGE_DIR
    USERS_FOLDER = os.environ.get('USERS_FOLDER', 'Пользователи')  # папки обычных пользователей
    APP_DIR = BASE_DIR
    DATA_DIR = DATA_DIR
    MAX_CONTENT_LENGTH = int(os.environ.get('MAX_UPLOAD_MB', '4096')) * 1024 * 1024
    HOST = os.environ.get('HOST', '0.0.0.0')
    PORT = int(os.environ.get('PORT', '5000'))
    DISK_NAME = os.environ.get('DISK_NAME', 'Диск')
    REMEMBER_COOKIE_DURATION = 60 * 60 * 24 * 30  # «запомнить» на 30 дней
    SESSION_COOKIE_SAMESITE = 'Lax'
    REMEMBER_COOKIE_SAMESITE = 'Lax'
