from datetime import datetime
from flask_login import UserMixin
from app import db

class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)  # имя = название его папки
    password_hash = db.Column(db.String(128), nullable=False)          # пароль = ключ входа
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    quota_gb = db.Column(db.Float, nullable=True)  # None = без отдельного ограничения
    full_access = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime, nullable=True)

class Album(db.Model):
    __tablename__ = 'albums'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    name = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class AlbumItem(db.Model):
    __tablename__ = 'album_items'
    id = db.Column(db.Integer, primary_key=True)
    album_id = db.Column(db.Integer, db.ForeignKey('albums.id', ondelete='CASCADE'), nullable=False)
    path = db.Column(db.String(1024), nullable=False)

class Activity(db.Model):
    __tablename__ = 'activities'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    action = db.Column(db.String(64), nullable=False)
    path = db.Column(db.String(1024), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Setting(db.Model):
    __tablename__ = 'settings'
    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.String(1024), nullable=False)

class TrashItem(db.Model):
    """Удалённый файл/папка: лежит в data/trash/<trash_name>, можно вернуть на место."""
    __tablename__ = 'trash_items'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    name = db.Column(db.String(256), nullable=False)
    parent_path = db.Column(db.String(1024), nullable=False)  # откуда удалён (путь на сайте)
    trash_name = db.Column(db.String(64), unique=True, nullable=False)
    is_directory = db.Column(db.Boolean, default=False)
    size = db.Column(db.BigInteger, default=0)
    deleted_at = db.Column(db.DateTime, default=datetime.utcnow)

class UploadSession(db.Model):
    """Незавершённая загрузка большого файла по частям (chunked upload).

    Части лежат в data/chunked/<id>/<номер>. После приёма всех частей они
    склеиваются в один файл в папку пользователя. Состояние живёт в базе,
    поэтому обрыв соединения не портит загрузку: клиент спрашивает список
    принятых частей и присылает только недостающие (докачка).
    """
    __tablename__ = 'upload_sessions'
    id = db.Column(db.String(64), primary_key=True)          # случайный токен загрузки
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    filename = db.Column(db.String(256), nullable=False)
    folder = db.Column(db.String(1024), nullable=False)      # папка назначения (путь на сайте)
    size = db.Column(db.BigInteger, nullable=False)          # полный размер файла в байтах
    chunk_size = db.Column(db.Integer, nullable=False)
    total_chunks = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class Share(db.Model):
    """Публичная ссылка на файл или папку (путь на сайте от корня пользователя)."""
    __tablename__ = 'shares'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    path = db.Column(db.String(1024), nullable=False)
    token = db.Column(db.String(64), unique=True, index=True, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=True)
    allow_download = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
