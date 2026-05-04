# Cloud Disk — Облачное хранилище файлов

Flask-приложение для хранения и обмена файлами (аналог Google Drive / Dropbox).

## Технологии

- **Backend**: Python + Flask 3.0
- **База данных**: SQLite через Flask-SQLAlchemy
- **Аутентификация**: Flask-Login + passlib/bcrypt
- **Фронтенд**: Jinja2-шаблоны + Bootstrap
- **Продакшн-сервер**: Gunicorn

## Структура проекта

```
main.py           # Точка входа
config.py         # Конфигурация (SECRET_KEY из ENV, SQLite, пути хранения)
app/
  __init__.py     # Фабрика приложения, инициализация DB и blueprints
  models.py       # Модели: User, FileMetadata, ShareLink
  auth.py         # Blueprint /auth — регистрация, вход, выход
  files.py        # Blueprint /files — просмотр, загрузка, скачивание, удаление, шаринг
  public.py       # Blueprint /public — публичный доступ по токену
templates/        # Jinja2-шаблоны
storage/          # Физическое хранилище файлов (UUID-имена)
cloud_disk.db     # SQLite база данных
```

## Переменные окружения

| Переменная  | Описание                        | Где задаётся    |
|-------------|---------------------------------|-----------------|
| `SECRET_KEY` | Секретный ключ Flask            | Secrets (shared) |

## Запуск

- **Development**: `python main.py` (Flask dev server, порт 5000)
- **Production**: `gunicorn --bind=0.0.0.0:5000 --reuse-port main:app`

## Деплой

Тип деплоя: **VM** (нужно постоянное хранилище — SQLite + папка `storage/`)

## Основные возможности

- Регистрация и авторизация пользователей
- Виртуальная файловая система с папками
- Загрузка / скачивание файлов (до 100 МБ)
- Публичные ссылки с истечением через 7 дней
- Защита от directory traversal
