import os
import shutil
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from datetime import datetime
from flask_login import current_user
from markupsafe import escape
from app import db
from app.auth import admin_required, check_new_password, pw_hash
from app.models import User, Setting, TrashItem, Share, Album, AlbumItem, Activity
from app.storage import storage_root, users_dir, bad_name, check_storage_path, BAD_NAME_MSG

admin_bp = Blueprint('admin', __name__)

def _get_user(user_id):
    user = db.session.get(User, user_id)
    if not user:
        flash('Пользователь не найден', 'danger')
    return user

def _admins_count():
    return User.query.filter_by(is_admin=True).count()


def _access_from_form():
    """(полный доступ, лимит ГБ, текст ошибки) из полей access и quota_gb."""
    if request.form.get('access') == 'full':
        return True, None, None
    raw = request.form.get('quota_gb', '').strip().replace(',', '.')
    try:
        quota = float(raw)
    except ValueError:
        return False, None, 'Укажите, сколько ГБ выделить пользователю (числом)'
    if quota <= 0:
        return False, None, 'Лимит должен быть больше 0 ГБ'
    return False, quota, None


def delete_user_data(user_id):
    """Альбомы, ссылки, история и корзина пользователя — чтобы новому с тем же id ничего не досталось."""
    album_ids = [a.id for a in Album.query.filter_by(user_id=user_id).all()]
    if album_ids:
        AlbumItem.query.filter(AlbumItem.album_id.in_(album_ids)).delete(synchronize_session=False)
    Album.query.filter_by(user_id=user_id).delete()
    Activity.query.filter_by(user_id=user_id).delete()
    Share.query.filter_by(user_id=user_id).delete()
    for t in TrashItem.query.filter_by(user_id=user_id).all():
        p = os.path.join(current_app.config['DATA_DIR'], 'trash', t.trash_name)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.exists(p):
            os.remove(p)
        db.session.delete(t)

@admin_bp.route('/users')
@admin_required
def users():
    all_users = User.query.order_by(User.is_admin.desc(), User.username).all()
    usage, counts = {}, {}
    for u in all_users:  # размер считаем только у личных папок, у админа — весь диск
        if u.is_admin or u.full_access:
            continue
        folder = os.path.join(users_dir(), u.username)
        total = n = 0
        for dirpath, _, files in os.walk(folder):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                    n += 1
                except OSError:
                    pass
        usage[u.id], counts[u.id] = total, n
    return render_template('admin.html', users=all_users, usage=usage, counts=counts,
                           users_dir=users_dir())

@admin_bp.route('/users/add', methods=['POST'])
@admin_required
def add_user():
    name = request.form.get('username', '').strip()
    password = request.form.get('password', '')
    if bad_name(name):
        flash('Имя станет названием папки. ' + BAD_NAME_MSG, 'danger')
        return redirect(url_for('admin.users'))
    if any(u.username.casefold() == name.casefold() for u in User.query.all()):
        flash(f'Пользователь «{escape(name)}» уже есть (имена Windows не различают регистр)', 'danger')
        return redirect(url_for('admin.users'))
    error = check_new_password(password)
    if error:
        flash(error, 'danger')
        return redirect(url_for('admin.users'))
    full_access, quota, error = _access_from_form()
    if error:
        flash(error, 'danger')
        return redirect(url_for('admin.users'))
    user = User(username=name, password_hash=pw_hash.hash(password),
                is_admin=False, full_access=full_access, quota_gb=quota)
    db.session.add(user)
    db.session.commit()
    os.makedirs(os.path.join(users_dir(), name), exist_ok=True)
    where = ('полный доступ ко всему хранилищу' if user.full_access
             else f'лимит {quota:g} ГБ, его папка: {escape(os.path.join(users_dir(), name))}')
    flash(f'Пользователь «{escape(name)}» добавлен, пароль <b class="pw-shown">{escape(password)}</b>, {where}', 'success')
    return redirect(url_for('admin.users'))

@admin_bp.route('/users/<int:user_id>/password', methods=['POST'])
@admin_required
def set_password(user_id):
    user = _get_user(user_id)
    if not user:
        return redirect(url_for('admin.users'))
    password = request.form.get('password', '')
    error = check_new_password(password, exclude_id=user.id)
    if error:
        flash(error, 'danger')
        return redirect(url_for('admin.users'))
    user.password_hash = pw_hash.hash(password)
    db.session.commit()
    flash(f'Новый пароль для «{escape(user.username)}»: <b class="pw-shown">{escape(password)}</b>', 'success')
    return redirect(url_for('admin.users'))

@admin_bp.route('/users/<int:user_id>/rename', methods=['POST'])
@admin_required
def rename_user(user_id):
    user = _get_user(user_id)
    if not user:
        return redirect(url_for('admin.users'))
    name = request.form.get('username', '').strip()
    old = os.path.join(users_dir(), user.username)
    new = os.path.join(users_dir(), name)
    if bad_name(name):
        flash(BAD_NAME_MSG, 'danger')
    elif any(u.id != user.id and u.username.casefold() == name.casefold() for u in User.query.all()):
        flash(f'Имя «{escape(name)}» уже занято', 'danger')
    elif os.path.exists(new) and os.path.normcase(new) != os.path.normcase(old):
        flash(f'Папка «{escape(new)}» уже существует — выберите другое имя', 'danger')
    else:
        try:
            if os.path.isdir(old):
                os.rename(old, new)  # папка пользователя переименовывается вместе с ним
        except OSError as e:
            flash(f'Не удалось переименовать папку: {escape(e.strerror or e)}', 'danger')
            return redirect(url_for('admin.users'))
        user.username = name
        db.session.commit()
        flash(f'Имя изменено на «{escape(name)}»', 'success')
    return redirect(url_for('admin.users'))

@admin_bp.route('/users/<int:user_id>/access', methods=['POST'])
@admin_required
def set_access(user_id):
    user = _get_user(user_id)
    if not user:
        return redirect(url_for('admin.users'))
    if user.is_admin:
        flash('У администратора всегда полный доступ', 'warning')
        return redirect(url_for('admin.users'))
    full_access, quota, error = _access_from_form()
    if error:
        flash(error, 'danger')
        return redirect(url_for('admin.users'))
    user.full_access, user.quota_gb = full_access, quota
    db.session.commit()
    if not full_access:
        os.makedirs(os.path.join(users_dir(), user.username), exist_ok=True)
    flash(f'«{escape(user.username)}»: ' + ('полный доступ ко всему хранилищу' if full_access
                                           else f'личная папка, лимит {quota:g} ГБ'), 'success')
    return redirect(url_for('admin.users'))

@admin_bp.route('/users/<int:user_id>/toggle-admin', methods=['POST'])
@admin_required
def toggle_admin(user_id):
    user = _get_user(user_id)
    if not user:
        return redirect(url_for('admin.users'))
    if user.is_admin and _admins_count() <= 1:
        flash('Нельзя снять права с единственного администратора', 'danger')
        return redirect(url_for('admin.users'))
    user.is_admin = not user.is_admin
    db.session.commit()
    flash(f'«{escape(user.username)}» теперь {"администратор" if user.is_admin else "обычный пользователь"}', 'success')
    return redirect(url_for('admin.users'))

@admin_bp.route('/users/<int:user_id>/delete', methods=['POST'])
@admin_required
def delete_user(user_id):
    user = _get_user(user_id)
    if not user:
        return redirect(url_for('admin.users'))
    if user.id == current_user.id:
        flash('Нельзя удалить самого себя', 'danger')
        return redirect(url_for('admin.users'))
    name = user.username
    folder = os.path.join(users_dir(), name)
    # его папка уходит в корзину того, кто удалил — её можно вернуть
    if os.path.isdir(folder):
        from app.files import _trash_dir
        import uuid
        item = TrashItem(user_id=current_user.id, name=name,
                         parent_path='/' + current_app.config['USERS_FOLDER'] + '/',
                         trash_name=uuid.uuid4().hex, is_directory=True, size=0)
        shutil.move(folder, os.path.join(_trash_dir(), item.trash_name))
        db.session.add(item)
    delete_user_data(user.id)
    db.session.delete(user)
    db.session.commit()
    flash(f'Пользователь «{escape(name)}» удалён. Его папка — в вашей корзине, её можно вернуть.', 'success')
    return redirect(url_for('admin.users'))

@admin_bp.route('/reset-setup', methods=['POST'])
@admin_required
def reset_setup():
    """Return to first-run setup without deleting files in the storage folder."""
    import sqlite3
    from flask_login import logout_user
    if not pw_hash.verify(request.form.get('password', ''), current_user.password_hash):
        flash('Неверный пароль администратора — настройки не сброшены', 'danger')
        return redirect(request.referrer or url_for('files.list_files'))
    database_path = current_app.config['SQLALCHEMY_DATABASE_URI'].removeprefix('sqlite:///')
    backup_path = database_path + '.backup-' + datetime.utcnow().strftime('%Y%m%d%H%M%S')
    os.makedirs(os.path.dirname(database_path), exist_ok=True)
    source = sqlite3.connect(database_path)
    backup = sqlite3.connect(backup_path)
    try:
        source.backup(backup)
    finally:
        backup.close()
        source.close()
    db.session.remove()
    db.drop_all()
    db.create_all()
    db.session.commit()
    shutil.rmtree(os.path.join(current_app.config['DATA_DIR'], 'trash'), ignore_errors=True)
    from app.csrf import rotate_token
    logout_user()
    rotate_token()  # сессия сброшена — прежний токен недействителен
    flash('Настройки сброшены. Файлы в хранилище не удалены.', 'success')
    return redirect(url_for('auth.setup'))


@admin_bp.route('/storage', methods=['GET', 'POST'])
@admin_required
def storage():
    """Где хранятся файлы: папка или целый диск."""
    from_env = bool(current_app.config.get('STORAGE_DIR'))
    if request.method == 'POST' and not from_env:
        path, error = check_storage_path(request.form.get('storage_root', ''))
        if error:
            flash(error, 'danger')
        else:
            s = db.session.get(Setting, 'storage_root') or Setting(key='storage_root')
            s.value = path
            db.session.add(s)
            db.session.commit()
            os.makedirs(users_dir(), exist_ok=True)
            flash(f'Хранилище: {escape(path)}', 'success')
            return redirect(url_for('files.list_files'))
    return render_template('storage.html', current=storage_root(), from_env=from_env,
                           suggested=storage_root() or default_storage())


def default_storage():
    """Предложение по умолчанию — корень диска, где лежит программа (например D:)."""
    drive = os.path.splitdrive(current_app.config['APP_DIR'])[0]
    return drive + os.sep if drive else os.path.join(current_app.config['DATA_DIR'], 'storage')
