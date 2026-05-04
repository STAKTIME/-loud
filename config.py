import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'fallback_dev_key_change_in_production')
    SQLALCHEMY_DATABASE_URI = 'sqlite:///' + os.path.join(BASE_DIR, 'cloud_disk.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    UPLOAD_BASE_DIR = os.path.join(BASE_DIR, 'storage')  # физическое хранилище
    MAX_CONTENT_LENGTH = 100 * 1024 * 1024  # 100 MB