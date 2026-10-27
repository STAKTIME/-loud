import os
import uuid
import shutil
import tempfile
from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file, current_app, jsonify, abort
from flask_login import login_required, current_user
from markupsafe import escape
from app import db
from app.models import TrashItem, Share, Album, AlbumItem, Activity
from app.storage import (resolve, list_dir, to_vpath, norm_vpath, is_locked, clean_name, bad_name,
                         unique_path, folder_size, user_home, is_hidden_name, is_protected, Entry, BAD_NAME_MSG, quota_exceeded, quota_exceeded_after_upload)

files_bp = Blueprint('files', __name__)

UNSAFE_INLINE = {'html', 'htm', 'xhtml', 'shtml', 'js', 'mjs', 'xml'}
SEARCH_LIMIT = 300


def _folder_arg(name='path'):
    try:
        return norm_vpath(request.args.get(name, '/'), folder=True)
    except ValueError:
        abort(404)


def _form_path(name, folder=False):
    try:
        return norm_vpath(request.form.get(name, '/'), folder=folder)
    except ValueError:
        abort(404)


def _go(vpath):
    return redirect(url_for('files.list_files', path=vpath or '/'))


def _trash_dir():
    d = os.path.join(current_app.config['DATA_DIR'], 'trash')
    os.makedirs(d, exist_ok=True)
    return d


def _to_trash(real):
    """Переносит файл/папку в корзину программы и записывает, откуда он."""
    is_dir = os.path.isdir(real)
    size = folder_size(real) if is_dir else os.path.getsize(real)
    item = TrashItem(user_id=current_user.id, name=os.path.basename(real),
                     parent_path=norm_vpath(os.path.dirname(to_vpath(current_user, real)), folder=True),
                     trash_name=uuid.uuid4().hex, is_directory=is_dir, size=size,
                     deleted_at=datetime.utcnow())
    shutil.move(real, os.path.join(_trash_dir(), item.trash_name))
    db.session.add(item)


def _move(src, dest_folder):
    """Перенос с проверками. Возвращает текст ошибки или None."""
    if is_locked(current_user, src):
        return f'«{escape(os.path.basename(src))}» — системная папка, её нельзя переносить'
    if os.path.normcase(os.path.dirname(src)) == os.path.normcase(dest_folder):
        return None
    if os.path.isdir(src) and os.path.normcase(dest_folder + os.sep).startswith(os.path.normcase(src + os.sep)):
        return 'Нельзя переместить папку внутрь самой себя'
    target = os.path.join(dest_folder, os.path.basename(src))
    if os.path.exists(target):
        return f'В папке назначения уже есть «{escape(os.path.basename(src))}»'
    shutil.move(src, target)
    return None

# ── Альбомы и история ────────────────────────────────────────────────────────
IMAGE_EXTS = {'jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'svg'}

def _record(action, path=None):
    db.session.add(Activity(user_id=current_user.id, action=action, path=path))

def _user_images():
    home = resolve(current_user, '/')
    result = []
    for root, dirs, names in os.walk(home):
        dirs[:] = [d for d in dirs if not is_hidden_name(d) and not is_protected(os.path.join(root, d))]
        for name in names:
            if name.rsplit('.', 1)[-1].lower() in IMAGE_EXTS:
                result.append(to_vpath(current_user, os.path.join(root, name)))
    return sorted(result)


def _user_folders():
    home = resolve(current_user, '/')
    result = ['/']
    for root, dirs, _ in os.walk(home):
        dirs[:] = [d for d in dirs if not is_hidden_name(d) and not is_protected(os.path.join(root, d))]
        for name in dirs:
            result.append(to_vpath(current_user, os.path.join(root, name)) + '/')
    return sorted(set(result), key=lambda path: (path.count('/'), path.lower()))

@files_bp.route('/albums')
@login_required
def albums():
    return render_template('albums.html', albums=Album.query.filter_by(user_id=current_user.id).order_by(Album.created_at.desc()).all())

@files_bp.route('/albums/create', methods=['POST'])
@login_required
def create_album():
    name = request.form.get('name', '').strip()
    if bad_name(name):
        flash(BAD_NAME_MSG, 'danger')
    else:
        db.session.add(Album(user_id=current_user.id, name=name))
        db.session.commit()
        flash('Альбом создан', 'success')
    return redirect(url_for('files.albums'))

@files_bp.route('/albums/<int:album_id>/upload', methods=['POST'])
@login_required
def album_upload(album_id):
    album = Album.query.filter_by(id=album_id, user_id=current_user.id).first_or_404()
    try:
        dest_path = norm_vpath(request.form.get('dest_path', '/'), folder=True)
    except ValueError:
        abort(404)
    target_folder = resolve(current_user, dest_path)
    if not os.path.isdir(target_folder):
        abort(404)
    for f in request.files.getlist('photos'):
        name = clean_name(f.filename)
        if not name or name.rsplit('.', 1)[-1].lower() not in IMAGE_EXTS:
            continue
        size = getattr(f, 'content_length', None) or 0
        if not size:
            try:
                f.stream.seek(0, os.SEEK_END)
                size = f.stream.tell()
                f.stream.seek(0)
            except (OSError, ValueError):
                size = 0
        if quota_exceeded_after_upload(current_user, size):
            flash('Превышен лимит места на диске', 'danger')
            continue
        target = unique_path(target_folder, name)
        try:
            f.save(target)
        except OSError:
            flash(f'Не удалось сохранить «{escape(name)}»', 'danger')
            continue
        path = to_vpath(current_user, target)
        db.session.add(AlbumItem(album_id=album.id, path=path))
        _record('upload', path)
    db.session.commit()
    return redirect(url_for('files.album_view', album_id=album.id))

@files_bp.route('/albums/<int:album_id>/share', methods=['POST'])
@login_required
def share_album(album_id):
    album = Album.query.filter_by(id=album_id, user_id=current_user.id).first_or_404()
    days = request.form.get('expires_days', '7')
    try: days = int(days)
    except ValueError: days = 7
    expires = None if days == 0 else datetime.utcnow() + timedelta(days=max(1, days))
    share = Share(user_id=current_user.id, path='@album:' + str(album.id), token=uuid.uuid4().hex, expires_at=expires, allow_download=request.form.get('allow_download') == '1')
    db.session.add(share); db.session.commit()
    flash('Ссылка на альбом создана: ' + url_for('public.browse_shared', token=share.token, _external=True), 'success')
    return redirect(url_for('files.album_view', album_id=album.id))

@files_bp.route('/albums/<int:album_id>')
@login_required
def album_view(album_id):
    album = Album.query.filter_by(id=album_id, user_id=current_user.id).first_or_404()
    items = AlbumItem.query.filter_by(album_id=album.id).all()
    valid = []
    for item in items:
        try:
            if os.path.isfile(resolve(current_user, item.path)):
                valid.append(item)
        except Exception:
            continue
    return render_template('album.html', album=album, paths=[i.path for i in valid], available=_user_images(), folders=_user_folders())

@files_bp.route('/albums/<int:album_id>/add', methods=['POST'])
@login_required
def album_add(album_id):
    album = Album.query.filter_by(id=album_id, user_id=current_user.id).first_or_404()
    selected_paths = request.form.getlist('paths') or request.form.getlist('paths[]')
    for path in selected_paths:
        try:
            real = resolve(current_user, path)
            if os.path.isfile(real) and os.path.splitext(real)[1][1:].lower() in IMAGE_EXTS:
                if not AlbumItem.query.filter_by(album_id=album.id, path=norm_vpath(path)).first():
                    db.session.add(AlbumItem(album_id=album.id, path=norm_vpath(path)))
        except Exception:
            continue
    db.session.commit()
    return redirect(url_for('files.album_view', album_id=album.id))

@files_bp.route('/history')
@login_required
def history():
    return render_template('history.html', activities=Activity.query.filter_by(user_id=current_user.id).order_by(Activity.created_at.desc()).limit(300).all())

# ── Список файлов ─────────────────────────────────────────────────────────────

@files_bp.route('/')
@login_required
def list_files():
    current_path = _folder_arg()
    real = resolve(current_user, current_path)
    if not os.path.isdir(real):
        return _go(norm_vpath(current_path.rsplit('/', 1)[0], folder=True))
    items = list_dir(current_user, real)
    if items is None:
        flash('Нет доступа к этой папке (её закрыла Windows)', 'danger')
        items = []
    parent_path = None
    if current_path != '/':
        parts = current_path.strip('/').split('/')
        parent_path = '/' + '/'.join(parts[:-1]) + '/' if len(parts) > 1 else '/'
    return render_template('index.html', items=items, current_path=current_path, parent_path=parent_path)

# ── Поиск ─────────────────────────────────────────────────────────────────────

@files_bp.route('/search')
@login_required
def search():
    q = request.args.get('q', '').strip()
    if not q:
        return _go('/')
    ql = q.lower()
    folders, files, truncated = [], [], False
    home = resolve(current_user, '/')
    for dirpath, dirnames, filenames in os.walk(home):
        dirnames[:] = [d for d in dirnames
                       if not is_hidden_name(d) and not is_protected(os.path.join(dirpath, d))]
        for name in dirnames + filenames:
            if ql in name.lower():
                try:
                    e = Entry(current_user, os.path.join(dirpath, name))
                except OSError:
                    continue
                (folders if e.is_directory else files).append(e)
                if len(folders) + len(files) >= SEARCH_LIMIT:
                    truncated = True
                    break
        if truncated:
            break
    if truncated:
        flash(f'Найдено больше {SEARCH_LIMIT} — уточните запрос', 'warning')
    return render_template('search.html', files=files, folders=folders, q=q)

# ── Корзина ───────────────────────────────────────────────────────────────────

@files_bp.route('/trash')
@login_required
def trash():
    items = TrashItem.query.filter_by(user_id=current_user.id).order_by(TrashItem.deleted_at.desc()).all()
    return render_template('trash.html', items=items)


def _trash_item():
    item = TrashItem.query.filter_by(id=request.form.get('item_id', type=int), user_id=current_user.id).first()
    if not item:
        flash('Элемент не найден', 'danger')
    return item


@files_bp.route('/restore', methods=['POST'])
@login_required
def restore():
    item = _trash_item()
    if item:
        folder = resolve(current_user, item.parent_path, must_exist=False)
        os.makedirs(folder, exist_ok=True)
        target = unique_path(folder, item.name)
        shutil.move(os.path.join(_trash_dir(), item.trash_name), target)
        db.session.delete(item)
        db.session.commit()
        flash(f'«{escape(os.path.basename(target))}» восстановлен в {escape(item.parent_path)}', 'success')
    return redirect(url_for('files.trash'))


def _wipe(item):
    p = os.path.join(_trash_dir(), item.trash_name)
    if os.path.isdir(p):
        shutil.rmtree(p, ignore_errors=True)
    elif os.path.exists(p):
        os.remove(p)
    db.session.delete(item)


@files_bp.route('/delete-permanent', methods=['POST'])
@login_required
def delete_permanent():
    item = _trash_item()
    if item:
        _wipe(item)
        db.session.commit()
        flash(f'«{escape(item.name)}» удалён навсегда', 'success')
    return redirect(url_for('files.trash'))


@files_bp.route('/empty-trash', methods=['POST'])
@login_required
def empty_trash():
    for item in TrashItem.query.filter_by(user_id=current_user.id).all():
        _wipe(item)
    db.session.commit()
    flash('Корзина очищена', 'success')
    return redirect(url_for('files.trash'))

# ── Новая папка ───────────────────────────────────────────────────────────────

@files_bp.route('/mkdir', methods=['POST'])
@login_required
def mkdir():
    current_path = _folder_arg()
    folder = resolve(current_user, current_path)
    name = request.form.get('folder_name', '').strip()
    if bad_name(name):
        flash(BAD_NAME_MSG, 'danger')
        return _go(current_path)
    try:
        os.mkdir(os.path.join(folder, name))
        flash(f'Папка «{escape(name)}» создана', 'success')
    except FileExistsError:
        flash('Папка с таким именем уже существует', 'danger')
    except OSError as e:
        flash(f'Не удалось создать папку: {escape(e.strerror or e)}', 'danger')
    return _go(current_path)

# ── Загрузка ──────────────────────────────────────────────────────────────────

@files_bp.route('/upload', methods=['POST'])
@login_required
def upload_file():
    current_path = _folder_arg()
    folder = resolve(current_user, current_path)
    count, renamed = 0, []
    for f in request.files.getlist('file'):
        name = clean_name(f.filename)
        if not name:
            continue
        size = getattr(f, 'content_length', None) or 0
        if not size:
            try:
                f.stream.seek(0, os.SEEK_END)
                size = f.stream.tell()
                f.stream.seek(0)
            except (OSError, ValueError):
                size = 0
        if quota_exceeded_after_upload(current_user, size):
            flash('Превышен лимит места на диске', 'danger')
            continue
        target = unique_path(folder, name)
        try:
            f.save(target)
        except OSError as e:
            flash(f'Не удалось сохранить «{escape(name)}»: {escape(e.strerror or e)}', 'danger')
            continue
        count += 1
        _record('upload', to_vpath(current_user, target))
        if os.path.basename(target) != name:
            renamed.append(os.path.basename(target))
    if count:
        flash(f'Загружено файлов: {count}', 'success')
    if renamed:
        flash('Такие имена уже были, сохранено как: ' + ', '.join(escape(r) for r in renamed), 'warning')
    db.session.commit()
    return _go(current_path)

# ── Скачивание и просмотр ─────────────────────────────────────────────────────

def _file_arg():
    real = resolve(current_user, request.args.get('path', ''))
    if not os.path.isfile(real):
        abort(404)
    return real


@files_bp.route('/download')
@login_required
def download_file():
    real = resolve(current_user, request.args.get('path', ''))
    if not os.path.isfile(real) and not os.path.isdir(real):
        abort(404)
    if os.path.isdir(real):
        import zipfile
        archive = tempfile.NamedTemporaryFile(prefix='disk-', suffix='.zip', delete=False)
        archive.close()
        root = os.path.normcase(os.path.realpath(real))
        try:
            with zipfile.ZipFile(archive.name, 'w', compression=zipfile.ZIP_DEFLATED) as zipped:
                for current, dirs, names in os.walk(real, followlinks=False):
                    dirs[:] = [name for name in dirs
                               if not is_hidden_name(name)
                               and not os.path.islink(os.path.join(current, name))
                               and not is_protected(os.path.join(current, name))]
                    for name in names:
                        path = os.path.join(current, name)
                        if is_hidden_name(name) or os.path.islink(path) or is_protected(path):
                            continue
                        try:
                            if os.path.isfile(path) and os.path.commonpath([root, os.path.normcase(os.path.realpath(path))]) == root:
                                zipped.write(path, os.path.relpath(path, real))
                        except (OSError, ValueError):
                            continue
            response = send_file(archive.name, download_name=os.path.basename(real) + '.zip', as_attachment=True)
            response.call_on_close(lambda: os.path.exists(archive.name) and os.remove(archive.name))
            return response
        except Exception:
            try:
                os.remove(archive.name)
            except OSError:
                pass
            raise
    return send_file(real, download_name=os.path.basename(real), as_attachment=True)


@files_bp.route('/preview')
@login_required
def preview_file():
    real = _file_arg()
    name = os.path.basename(real)
    ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    if ext in UNSAFE_INLINE:
        # html/js показываем как текст, чтобы чужая страница не выполнялась внутри диска
        resp = send_file(real, mimetype='text/plain; charset=utf-8', as_attachment=False)
    else:
        resp = send_file(real, download_name=name, as_attachment=False)
    if ext == 'svg':
        resp.headers['Content-Security-Policy'] = 'sandbox'
    return resp

# ── Удаление (в корзину) ──────────────────────────────────────────────────────

@files_bp.route('/delete', methods=['POST'])
@login_required
def delete_item():
    vpath = request.args.get('path', '')
    real = resolve(current_user, vpath)
    back = norm_vpath(to_vpath(current_user, os.path.dirname(real)), folder=True)
    if is_locked(current_user, real):
        flash('Эту папку удалить нельзя', 'danger')
        return _go(back)
    try:
        _to_trash(real)
        db.session.commit()
        flash('Перемещено в корзину', 'success')
    except OSError as e:
        flash(f'Не удалось удалить: {escape(e.strerror or e)}', 'danger')
    return _go(back)


@files_bp.route('/delete-batch', methods=['POST'])
@login_required
def delete_batch():
    count = 0
    for vpath in request.form.getlist('ids[]'):
        real = resolve(current_user, vpath)
        if is_locked(current_user, real):
            continue
        try:
            _to_trash(real)
            count += 1
        except OSError:
            pass
    db.session.commit()
    flash(f'Перемещено в корзину: {count} эл.', 'success')
    return _go(_form_path('return_path', folder=True))

# ── Переименование ────────────────────────────────────────────────────────────

@files_bp.route('/rename', methods=['POST'])
@login_required
def rename_item():
    return_path = _form_path('return_path', folder=True)
    real = resolve(current_user, request.form.get('item_id', ''))
    new_name = request.form.get('new_name', '').strip()
    if bad_name(new_name):
        flash(BAD_NAME_MSG, 'danger')
    elif is_locked(current_user, real):
        flash('Эту папку переименовать нельзя', 'danger')
    else:
        target = os.path.join(os.path.dirname(real), new_name)
        if os.path.exists(target) and os.path.normcase(target) != os.path.normcase(real):
            flash('Элемент с таким именем уже существует', 'danger')
        else:
            try:
                os.rename(real, target)
                flash(f'Переименовано в «{escape(new_name)}»', 'success')
            except OSError as e:
                flash(f'Не удалось переименовать: {escape(e.strerror or e)}', 'danger')
    return _go(return_path)

# ── Копирование ───────────────────────────────────────────────────────────────

def _copy(src, dest_folder):
    target = os.path.join(dest_folder, os.path.basename(src))
    if os.path.exists(target):
        base, ext = (os.path.splitext(os.path.basename(src)) if os.path.isfile(src)
                     else (os.path.basename(src), ''))
        target = unique_path(dest_folder, f'{base} — копия{ext}')
    if os.path.isdir(src):
        if os.path.normcase(dest_folder + os.sep).startswith(os.path.normcase(src + os.sep)):
            raise ValueError('Нельзя копировать папку внутрь самой себя')
        shutil.copytree(src, target)
    else:
        shutil.copy2(src, target)
    return os.path.basename(target)


@files_bp.route('/copy', methods=['POST'])
@login_required
def copy_item():
    return_path = _form_path('return_path', folder=True)
    src = resolve(current_user, request.form.get('item_id', ''))
    try:
        name = _copy(src, os.path.dirname(src))
        flash(f'Создана копия: «{escape(name)}»', 'success')
    except (OSError, ValueError) as e:
        flash(f'Не удалось скопировать: {escape(e)}', 'danger')
    return _go(return_path)


@files_bp.route('/copy-to', methods=['POST'])
@login_required
def copy_to():
    return_path = _form_path('return_path', folder=True)
    src = resolve(current_user, request.form.get('item_id', ''))
    dest = resolve(current_user, _form_path('dest_path', folder=True))
    try:
        name = _copy(src, dest)
        flash(f'Скопировано: «{escape(name)}»', 'success')
    except (OSError, ValueError) as e:
        flash(f'Не удалось скопировать: {escape(e)}', 'danger')
    return _go(return_path)

# ── Список папок для окна «Переместить/Копировать» ───────────────────────────

@files_bp.route('/folders-list')
@login_required
def folders_list():
    """Только вложенные папки указанной (диск может быть большим)."""
    base = _folder_arg()
    real = resolve(current_user, base)
    items = list_dir(current_user, real) or []
    return jsonify([{'name': e.name, 'path': base + e.name + '/'} for e in items if e.is_directory])

# ── Перемещение ───────────────────────────────────────────────────────────────

@files_bp.route('/move', methods=['POST'])
@login_required
def move_item():
    return_path = _form_path('return_path', folder=True)
    src = resolve(current_user, request.form.get('item_id', ''))
    dest = resolve(current_user, _form_path('dest_path', folder=True))
    try:
        error = _move(src, dest)
    except OSError as e:
        error = f'Не удалось переместить: {escape(e.strerror or e)}'
    flash(error or f'«{escape(os.path.basename(src))}» перемещён', 'danger' if error else 'success')
    return _go(return_path)


@files_bp.route('/move-batch', methods=['POST'])
@login_required
def move_batch():
    return_path = _form_path('return_path', folder=True)
    dest = resolve(current_user, _form_path('dest_path', folder=True))
    count = 0
    for vpath in request.form.getlist('ids[]'):
        src = resolve(current_user, vpath)
        try:
            error = _move(src, dest)
        except OSError as e:
            error = escape(e.strerror or e)
        if error:
            flash(error, 'danger')
        else:
            count += 1
    flash(f'Перемещено: {count} эл.', 'success')
    return _go(return_path)

# ── Публичная ссылка ──────────────────────────────────────────────────────────

@files_bp.route('/share', methods=['POST'])
@login_required
def share_file():
    real = resolve(current_user, request.form.get('path', request.args.get('path', '')))
    if not os.path.isfile(real) and not os.path.isdir(real):
        abort(404)
    days = request.form.get('expires_days', '7')
    try:
        days = int(days)
    except ValueError:
        days = 7
    expires = None if days == 0 else datetime.utcnow() + timedelta(days=max(1, days))
    share = Share(user_id=current_user.id, path=to_vpath(current_user, real),
                  token=uuid.uuid4().hex, expires_at=expires,
                  allow_download=request.form.get('allow_download') == '1')
    db.session.add(share)
    db.session.commit()
    url = url_for('public.browse_shared', token=share.token, _external=True)
    flash(f'Ссылка создана: <a href="{url}" target="_blank" style="word-break:break-all">{url}</a>', 'success')
    return _go(norm_vpath(os.path.dirname(share.path), folder=True))

# ── Место на диске ────────────────────────────────────────────────────────────

@files_bp.route('/shares')
@login_required
def shares():
    links = Share.query.filter_by(user_id=current_user.id).order_by(Share.created_at.desc()).all()
    return render_template('shares.html', links=links)

@files_bp.route('/shares/<int:share_id>/delete', methods=['POST'])
@login_required
def delete_share(share_id):
    share = Share.query.filter_by(id=share_id, user_id=current_user.id).first_or_404()
    db.session.delete(share)
    db.session.commit()
    flash('Публичная ссылка отключена', 'success')
    return redirect(url_for('files.shares'))

@files_bp.route('/api/disk-space')
@login_required
def api_disk_space():
    usage = shutil.disk_usage(user_home(current_user))
    return jsonify({'free': usage.free, 'total': usage.total, 'used': usage.used})
