"""Защита форм от CSRF: запрос, отправленный с чужого сайта от имени
вошедшего пользователя, не содержит секретного токена и отклоняется.

Схема «synchronizer token»: при входе в сессию кладём случайный токен,
каждая форма и каждый изменяющий POST-запрос обязаны его прислать.
"""
import hmac
from functools import wraps
from flask import abort, request, session
from markupsafe import Markup

CSRF_FIELD = '_csrf'          # имя поля в HTML-формах
CSRF_HEADER = 'X-CSRF-Token'  # заголовок для fetch/XHR
CSRF_SESSION_KEY = '_csrf_token'
MAX_AGE = 60 * 60 * 12        # токен живёт 12 часов


def get_token():
    """Текущий токен сессии (создаётся при первом обращении)."""
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        from secrets import token_hex
        token = token_hex(32)
        session[CSRF_SESSION_KEY] = token
    return token


def valid_token():
    """True, если в запросе есть правильный токен (поле формы или заголовок)."""
    sent = request.form.get(CSRF_FIELD) or request.headers.get(CSRF_HEADER, '')
    expected = session.get(CSRF_SESSION_KEY, '')
    return bool(sent) and bool(expected) and hmac.compare_digest(sent, expected)


def rotate_token():
    """Новый токен: после входа/выхода и сброса настроек, чтобы старые страницы не работали."""
    from secrets import token_hex
    session[CSRF_SESSION_KEY] = token_hex(32)
    return session[CSRF_SESSION_KEY]


def csrf_protect(view):
    """Декоратор: проверяет токен у всех изменяющих запросов (POST/PUT/PATCH/DELETE)."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if request.method in ('GET', 'HEAD', 'OPTIONS'):
            return view(*args, **kwargs)
        if not valid_token():
            abort(400, description='Запрос отклонён: неверный защитный код формы (CSRF). '
                                   'Обновите страницу и попробуйте ещё раз.')
        return view(*args, **kwargs)
    return wrapper


def csrf_field():
    """Скрытое поле для шаблонов: {{ csrf_field() }}."""
    return Markup(f'<input type="hidden" name="{CSRF_FIELD}" value="{get_token()}">')


def init_csrf(app):
    """Общая проверка перед каждым запросом + функции для шаблонов."""
    from flask_login import current_user

    @app.before_request
    def _check_csrf():
        # Защита нужна только изменяющим запросам вошедшего пользователя:
        # страницы входа и первого запуска от чужого сайта не представляют угрозы
        # (никто ещё не вошёл), публичные ссылки — только GET.
        if request.method in ('GET', 'HEAD', 'OPTIONS'):
            return None
        if not current_user.is_authenticated:
            return None
        if request.blueprint == 'public':
            return None
        if not valid_token():
            abort(400, description='Запрос отклонён: неверный защитный код формы (CSRF). '
                                   'Обновите страницу и попробуйте ещё раз.')
        return None

    @app.context_processor
    def _inject_csrf():
        return {'csrf_token': get_token, 'csrf_field': csrf_field, 'csrf_header': CSRF_HEADER}

    # GET-запросы со сторонних сайтов не должны попадать в топ фреймов
    # (clickjacking поверх наших форм — смежная угроза с CSRF)
    @app.after_request
    def _frame_options(response):
        response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
        response.headers.setdefault('Referrer-Policy', 'same-origin')
        return response
