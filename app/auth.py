import os
import time
from datetime import datetime
from functools import wraps
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, current_app
from flask_login import login_user, logout_user, login_required, current_user
import bcrypt
from app import db
from app.models import User, Setting
from app.storage import bad_name, check_storage_path, BAD_NAME_MSG

auth_bp = Blueprint('auth', __name__)


class pw_hash:
    """bcrypt-хэши паролей (rounds=10 — быстрая проверка при переборе пользователей)."""
    @staticmethod
    def hash(password):
        return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt(rounds=10)).decode('ascii')

    @staticmethod
    def verify(password, hashed):
        try:
            return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('ascii'))
        except ValueError:
            return False

MIN_PASSWORD_LEN = 4
MAX_FAILS = 5            # неверных паролей подряд с одного адреса…
LOCK_SECONDS = 10 * 60   # …и вход с него закрыт на 10 минут
_fails = {}              # ip -> (кол-во ошибок, время последней)

# ── Пароли ────────────────────────────────────────────────────────────────────

def find_user_by_password(password, exclude_id=None):
    """Пароль — это и логин: ищем пользователя, чей хэш подходит."""
    for user in User.query.all():
        if user.id != exclude_id and pw_hash.verify(password, user.password_hash):
            return user
    return None

def check_new_password(password, exclude_id=None):
    """Текст ошибки или None, если пароль можно выдать."""
    if len(password) < MIN_PASSWORD_LEN:
        return f'Пароль должен быть не короче {MIN_PASSWORD_LEN} символов'
    if find_user_by_password(password, exclude_id):
        return 'Этот пароль уже занят другим пользователем — попросите человека выбрать другой'
    return None

def admin_required(view):
    @wraps(view)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapper

# ── Защита от подбора ─────────────────────────────────────────────────────────

def _locked_for(ip):
    count, last = _fails.get(ip, (0, 0))
    left = LOCK_SECONDS - (time.time() - last)
    if count >= MAX_FAILS and left > 0:
        return int(left // 60) + 1
    if left <= 0:
        _fails.pop(ip, None)
    return 0

def _register_fail(ip):
    count, _ = _fails.get(ip, (0, 0))
    _fails[ip] = (count + 1, time.time())

# ── Маршруты ──────────────────────────────────────────────────────────────────

@auth_bp.route('/setup', methods=['GET', 'POST'])
def setup():
    """Первый запуск: пользователей ещё нет — создаём администратора."""
    if User.query.count():
        return redirect(url_for('auth.login'))
    if request.method == 'POST':
        name = request.form.get('username', '').strip() or 'Админ'
        password = request.form.get('password', '')
        root_in = request.form.get('storage_root', '')
        page = lambda: render_template('setup.html', username=name, storage=root_in, **_storage_ctx())
        if bad_name(name):
            flash('Имя станет названием папки. ' + BAD_NAME_MSG, 'danger')
            return page()
        if password != request.form.get('password2', ''):
            flash('Пароли не совпадают', 'danger')
            return page()
        error = check_new_password(password)
        if error:
            flash(error, 'danger')
            return page()
        if not current_app.config.get('STORAGE_DIR'):
            root, error = check_storage_path(root_in)
            if error:
                flash(error, 'danger')
                return page()
            db.session.add(Setting(key='storage_root', value=root))
        user = User(username=name, password_hash=pw_hash.hash(password), is_admin=True, last_login=datetime.utcnow())
        db.session.add(user)
        db.session.commit()
        from app.storage import users_dir
        os.makedirs(users_dir(), exist_ok=True)
        login_user(user, remember=True)
        flash('Диск готов! Добавьте пользователей в разделе «Пользователи».', 'success')
        return redirect(url_for('files.list_files'))
    from app.admin import default_storage
    return render_template('setup.html', username='', storage=default_storage(), **_storage_ctx())


def _storage_ctx():
    return {'storage_from_env': current_app.config.get('STORAGE_DIR')}

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if not User.query.count():
        return redirect(url_for('auth.setup'))
    if current_user.is_authenticated:
        return redirect(url_for('files.list_files'))
    if request.method == 'POST':
        ip = request.remote_addr or '?'
        minutes = _locked_for(ip)
        if minutes:
            flash(f'Слишком много неверных попыток. Попробуйте через {minutes} мин.', 'danger')
            return render_template('login.html')
        password = request.form.get('password', '')
        user = find_user_by_password(password) if password else None
        if not user:
            _register_fail(ip)
            flash('Неверный пароль', 'danger')
            return render_template('login.html')
        _fails.pop(ip, None)
        user.last_login = datetime.utcnow()
        db.session.commit()
        login_user(user, remember=True)
        nxt = request.args.get('next', '')
        if nxt.startswith('/') and not nxt.startswith('//'):
            return redirect(nxt)
        return redirect(url_for('files.list_files'))
    return render_template('login.html')

@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('auth.login'))
