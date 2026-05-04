import os
import uuid
import shutil as sh
from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file, current_app, jsonify
from flask_login import login_required, current_user
from app import db
from app.models import FileMetadata, ShareLink

files_bp = Blueprint('files', __name__)

def normalize_path(path):
    if '..' in path:
        raise ValueError('Invalid path')
    if not path.startswith('/'):
        path = '/' + path
    if path != '/' and not path.endswith('/'):
        path += '/'
    return path

def get_user_storage_path(user_id, storage_name):
    base = os.path.join(current_app.config['UPLOAD_BASE_DIR'], str(user_id))
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, storage_name)

def _parse_file_path(path):
    if '..' in path:
        raise ValueError
    if not path.startswith('/'):
        path = '/' + path
    parts = path.rsplit('/', 1)
    name = parts[1]
    parent = parts[0] + '/' if parts[0] else '/'
    parent = normalize_path(parent)
    return name, parent

# ── List files ──────────────────────────────────────────────────────────────

@files_bp.route('/')
@login_required
def list_files():
    current_path = request.args.get('path', '/')
    try:
        current_path = normalize_path(current_path)
    except ValueError:
        flash('Некорректный путь', 'danger')
        return redirect(url_for('files.list_files'))
    items = FileMetadata.query.filter_by(
        user_id=current_user.id,
        parent_path=current_path,
        is_deleted=False
    ).order_by(FileMetadata.is_directory.desc(), FileMetadata.name).all()
    parent_path = None
    if current_path != '/':
        parts = current_path.strip('/').split('/')
        parent_path = '/' + '/'.join(parts[:-1]) + '/' if len(parts) > 1 else '/'
    return render_template('index.html', items=items, current_path=current_path, parent_path=parent_path)

# ── Search ───────────────────────────────────────────────────────────────────

@files_bp.route('/search')
@login_required
def search():
    q = request.args.get('q', '').strip()
    if not q:
        return redirect(url_for('files.list_files'))
    folders = FileMetadata.query.filter(
        FileMetadata.user_id == current_user.id,
        FileMetadata.name.ilike(f'%{q}%'),
        FileMetadata.is_directory == True,
        FileMetadata.is_deleted == False
    ).order_by(FileMetadata.name).all()
    files = FileMetadata.query.filter(
        FileMetadata.user_id == current_user.id,
        FileMetadata.name.ilike(f'%{q}%'),
        FileMetadata.is_directory == False,
        FileMetadata.is_deleted == False
    ).order_by(FileMetadata.name).all()
    return render_template('search.html', files=files, folders=folders, q=q)

# ── Trash ────────────────────────────────────────────────────────────────────

@files_bp.route('/trash')
@login_required
def trash():
    items = FileMetadata.query.filter_by(
        user_id=current_user.id,
        is_deleted=True
    ).order_by(FileMetadata.deleted_at.desc()).all()
    return render_template('trash.html', items=items)

@files_bp.route('/restore', methods=['POST'])
@login_required
def restore():
    item_id = request.form.get('item_id')
    item = FileMetadata.query.filter_by(id=item_id, user_id=current_user.id).first()
    if not item:
        flash('Элемент не найден', 'danger')
        return redirect(url_for('files.trash'))
    item.is_deleted = False
    item.deleted_at = None
    db.session.commit()
    flash(f'«{item.name}» восстановлен', 'success')
    return redirect(url_for('files.trash'))

@files_bp.route('/delete-permanent', methods=['POST'])
@login_required
def delete_permanent():
    item_id = request.form.get('item_id')
    item = FileMetadata.query.filter_by(id=item_id, user_id=current_user.id, is_deleted=True).first()
    if not item:
        flash('Элемент не найден', 'danger')
        return redirect(url_for('files.trash'))
    if not item.is_directory:
        fp = get_user_storage_path(current_user.id, item.storage_name)
        if os.path.exists(fp):
            os.remove(fp)
    db.session.delete(item)
    db.session.commit()
    flash(f'«{item.name}» удалён навсегда', 'success')
    return redirect(url_for('files.trash'))

@files_bp.route('/empty-trash', methods=['POST'])
@login_required
def empty_trash():
    items = FileMetadata.query.filter_by(user_id=current_user.id, is_deleted=True).all()
    for item in items:
        if not item.is_directory:
            fp = get_user_storage_path(current_user.id, item.storage_name)
            if os.path.exists(fp):
                os.remove(fp)
        db.session.delete(item)
    db.session.commit()
    flash('Корзина очищена', 'success')
    return redirect(url_for('files.trash'))

# ── Mkdir ─────────────────────────────────────────────────────────────────────

@files_bp.route('/mkdir', methods=['POST'])
@login_required
def mkdir():
    current_path = request.args.get('path', '/')
    try:
        current_path = normalize_path(current_path)
    except ValueError:
        flash('Некорректный путь', 'danger')
        return redirect(url_for('files.list_files'))
    folder_name = request.form.get('folder_name', '').strip()
    if not folder_name:
        flash('Введите имя папки', 'danger')
        return redirect(url_for('files.list_files', path=current_path))
    exists = FileMetadata.query.filter_by(
        user_id=current_user.id, parent_path=current_path,
        name=folder_name, is_directory=True, is_deleted=False
    ).first()
    if exists:
        flash('Папка с таким именем уже существует', 'danger')
        return redirect(url_for('files.list_files', path=current_path))
    folder = FileMetadata(
        user_id=current_user.id, parent_path=current_path,
        name=folder_name, storage_name=str(uuid.uuid4()),
        size=0, is_directory=True, is_deleted=False
    )
    db.session.add(folder)
    db.session.commit()
    flash(f'Папка «{folder_name}» создана', 'success')
    return redirect(url_for('files.list_files', path=current_path))

# ── Upload ────────────────────────────────────────────────────────────────────

@files_bp.route('/upload', methods=['POST'])
@login_required
def upload_file():
    current_path = request.args.get('path', '/')
    try:
        current_path = normalize_path(current_path)
    except ValueError:
        flash('Некорректный путь', 'danger')
        return redirect(url_for('files.list_files'))
    if 'file' not in request.files:
        flash('Файл не выбран', 'danger')
        return redirect(url_for('files.list_files', path=current_path))
    uploaded = request.files.getlist('file')
    count = 0
    for file in uploaded:
        if file.filename == '':
            continue
        exists = FileMetadata.query.filter_by(
            user_id=current_user.id, parent_path=current_path,
            name=file.filename, is_deleted=False
        ).first()
        if exists:
            flash(f'Файл «{file.filename}» уже существует', 'warning')
            continue
        storage_name = f"{uuid.uuid4()}_{file.filename}"
        file_path = get_user_storage_path(current_user.id, storage_name)
        file.save(file_path)
        size = os.path.getsize(file_path)
        metadata = FileMetadata(
            user_id=current_user.id, parent_path=current_path,
            name=file.filename, storage_name=storage_name,
            size=size, is_directory=False, is_deleted=False
        )
        db.session.add(metadata)
        count += 1
    db.session.commit()
    if count:
        flash(f'Загружено файлов: {count}', 'success')
    return redirect(url_for('files.list_files', path=current_path))

# ── Download ──────────────────────────────────────────────────────────────────

@files_bp.route('/download')
@login_required
def download_file():
    path = request.args.get('path', '')
    if not path or path.endswith('/'):
        flash('Укажите путь к файлу', 'danger')
        return redirect(url_for('files.list_files'))
    try:
        name, parent = _parse_file_path(path)
    except Exception:
        flash('Некорректный путь', 'danger')
        return redirect(url_for('files.list_files'))
    file_record = FileMetadata.query.filter_by(
        user_id=current_user.id, parent_path=parent, name=name,
        is_directory=False, is_deleted=False
    ).first()
    if not file_record:
        flash('Файл не найден', 'danger')
        return redirect(url_for('files.list_files', path=parent))
    file_path = get_user_storage_path(current_user.id, file_record.storage_name)
    if not os.path.exists(file_path):
        flash('Файл отсутствует на сервере', 'danger')
        return redirect(url_for('files.list_files', path=parent))
    return send_file(file_path, download_name=file_record.name, as_attachment=True)

# ── Preview ───────────────────────────────────────────────────────────────────

@files_bp.route('/preview')
@login_required
def preview_file():
    path = request.args.get('path', '')
    if not path or path.endswith('/'):
        return 'Файл не указан', 400
    try:
        name, parent = _parse_file_path(path)
    except Exception:
        return 'Некорректный путь', 400
    file_record = FileMetadata.query.filter_by(
        user_id=current_user.id, parent_path=parent, name=name,
        is_directory=False, is_deleted=False
    ).first()
    if not file_record:
        return 'Файл не найден', 404
    file_path = get_user_storage_path(current_user.id, file_record.storage_name)
    if not os.path.exists(file_path):
        return 'Файл отсутствует на сервере', 404
    return send_file(file_path, download_name=file_record.name, as_attachment=False)

# ── Delete (soft) ─────────────────────────────────────────────────────────────

@files_bp.route('/delete', methods=['POST'])
@login_required
def delete_item():
    path = request.args.get('path', '')
    if not path:
        flash('Укажите путь для удаления', 'danger')
        return redirect(url_for('files.list_files'))
    try:
        if path.endswith('/'):
            current_path = normalize_path(path)
            parts = current_path.strip('/').split('/')
            parent = '/' + '/'.join(parts[:-1]) + '/' if len(parts) > 1 else '/'
            folder_name = parts[-1]
            folder = FileMetadata.query.filter_by(
                user_id=current_user.id, parent_path=parent,
                name=folder_name, is_directory=True, is_deleted=False
            ).first()
            if folder:
                folder.is_deleted = True
                folder.deleted_at = datetime.utcnow()
                # Also soft-delete all children
                children = FileMetadata.query.filter(
                    FileMetadata.user_id == current_user.id,
                    FileMetadata.parent_path.like(current_path + '%'),
                    FileMetadata.is_deleted == False
                ).all()
                for child in children:
                    child.is_deleted = True
                    child.deleted_at = datetime.utcnow()
            return_path = parent
        else:
            name, parent = _parse_file_path(path)
            file_record = FileMetadata.query.filter_by(
                user_id=current_user.id, parent_path=parent,
                name=name, is_directory=False, is_deleted=False
            ).first()
            if file_record:
                file_record.is_deleted = True
                file_record.deleted_at = datetime.utcnow()
            return_path = parent
    except ValueError:
        flash('Некорректный путь', 'danger')
        return redirect(url_for('files.list_files'))

    db.session.commit()
    flash('Перемещено в корзину', 'success')
    return redirect(url_for('files.list_files', path=return_path))

# ── Rename ────────────────────────────────────────────────────────────────────

@files_bp.route('/rename', methods=['POST'])
@login_required
def rename_item():
    item_id = request.form.get('item_id')
    new_name = request.form.get('new_name', '').strip()
    return_path = request.form.get('return_path', '/')
    if not new_name:
        flash('Введите новое имя', 'danger')
        return redirect(url_for('files.list_files', path=return_path))
    item = FileMetadata.query.filter_by(id=item_id, user_id=current_user.id, is_deleted=False).first()
    if not item:
        flash('Элемент не найден', 'danger')
        return redirect(url_for('files.list_files', path=return_path))
    old_name = item.name
    # Check name conflict
    conflict = FileMetadata.query.filter_by(
        user_id=current_user.id, parent_path=item.parent_path,
        name=new_name, is_deleted=False
    ).filter(FileMetadata.id != item.id).first()
    if conflict:
        flash('Элемент с таким именем уже существует', 'danger')
        return redirect(url_for('files.list_files', path=return_path))
    if item.is_directory:
        old_path = item.parent_path + old_name + '/'
        new_path = item.parent_path + new_name + '/'
        # Update all children paths
        children = FileMetadata.query.filter(
            FileMetadata.user_id == current_user.id,
            FileMetadata.parent_path.like(old_path + '%')
        ).all()
        for child in children:
            child.parent_path = new_path + child.parent_path[len(old_path):]
        # Also direct children
        direct = FileMetadata.query.filter_by(
            user_id=current_user.id, parent_path=old_path
        ).all()
        for child in direct:
            child.parent_path = new_path
    item.name = new_name
    db.session.commit()
    flash(f'Переименовано в «{new_name}»', 'success')
    return redirect(url_for('files.list_files', path=return_path))

# ── Copy ──────────────────────────────────────────────────────────────────────

@files_bp.route('/copy', methods=['POST'])
@login_required
def copy_item():
    item_id = request.form.get('item_id')
    return_path = request.form.get('return_path', '/')
    item = FileMetadata.query.filter_by(id=item_id, user_id=current_user.id, is_deleted=False).first()
    if not item or item.is_directory:
        flash('Можно копировать только файлы', 'danger')
        return redirect(url_for('files.list_files', path=return_path))
    # Generate unique copy name
    base, ext = (item.name.rsplit('.', 1) if '.' in item.name else (item.name, ''))
    copy_name = f"{base} — копия{'.' + ext if ext else ''}"
    counter = 1
    while FileMetadata.query.filter_by(
        user_id=current_user.id, parent_path=item.parent_path,
        name=copy_name, is_deleted=False
    ).first():
        copy_name = f"{base} — копия {counter}{'.' + ext if ext else ''}"
        counter += 1
    # Copy physical file
    src = get_user_storage_path(current_user.id, item.storage_name)
    new_storage_name = f"{uuid.uuid4()}_{copy_name}"
    dst = get_user_storage_path(current_user.id, new_storage_name)
    if os.path.exists(src):
        sh.copy2(src, dst)
        size = os.path.getsize(dst)
    else:
        size = 0
    new_item = FileMetadata(
        user_id=current_user.id, parent_path=item.parent_path,
        name=copy_name, storage_name=new_storage_name,
        size=size, is_directory=False, is_deleted=False
    )
    db.session.add(new_item)
    db.session.commit()
    flash(f'Создана копия: «{copy_name}»', 'success')
    return redirect(url_for('files.list_files', path=return_path))

# ── Copy-to (with destination) ────────────────────────────────────────────────

def _copy_folder(user_id, item, dest_path, new_name):
    new_folder = FileMetadata(
        user_id=user_id, parent_path=dest_path, name=new_name,
        storage_name=str(uuid.uuid4()), size=0, is_directory=True, is_deleted=False
    )
    db.session.add(new_folder)
    db.session.flush()
    old_path = item.parent_path + item.name + '/'
    new_path = dest_path + new_name + '/'
    for child in FileMetadata.query.filter_by(user_id=user_id, parent_path=old_path, is_deleted=False).all():
        if child.is_directory:
            _copy_folder(user_id, child, new_path, child.name)
        else:
            src = get_user_storage_path(user_id, child.storage_name)
            new_sn = f"{uuid.uuid4()}_{child.name}"
            dst = get_user_storage_path(user_id, new_sn)
            size = 0
            if os.path.exists(src):
                sh.copy2(src, dst)
                size = os.path.getsize(dst)
            db.session.add(FileMetadata(
                user_id=user_id, parent_path=new_path, name=child.name,
                storage_name=new_sn, size=size, is_directory=False, is_deleted=False
            ))

@files_bp.route('/copy-to', methods=['POST'])
@login_required
def copy_to():
    item_id    = request.form.get('item_id')
    dest_path  = request.form.get('dest_path', '/')
    return_path = request.form.get('return_path', '/')
    try:
        dest_path = normalize_path(dest_path)
    except ValueError:
        flash('Некорректный путь', 'danger')
        return redirect(url_for('files.list_files', path=return_path))
    item = FileMetadata.query.filter_by(id=item_id, user_id=current_user.id, is_deleted=False).first()
    if not item:
        flash('Элемент не найден', 'danger')
        return redirect(url_for('files.list_files', path=return_path))
    copy_name = item.name
    if FileMetadata.query.filter_by(user_id=current_user.id, parent_path=dest_path, name=copy_name, is_deleted=False).first():
        if item.is_directory:
            base, ext = copy_name, ''
        else:
            parts = copy_name.rsplit('.', 1)
            base, ext = (parts[0], '.' + parts[1]) if len(parts) == 2 else (copy_name, '')
        copy_name = f"{base} — копия{ext}"
        counter = 1
        while FileMetadata.query.filter_by(user_id=current_user.id, parent_path=dest_path, name=copy_name, is_deleted=False).first():
            copy_name = f"{base} — копия {counter}{ext}"
            counter += 1
    if item.is_directory:
        _copy_folder(current_user.id, item, dest_path, copy_name)
    else:
        src = get_user_storage_path(current_user.id, item.storage_name)
        new_sn = f"{uuid.uuid4()}_{copy_name}"
        dst = get_user_storage_path(current_user.id, new_sn)
        size = 0
        if os.path.exists(src):
            sh.copy2(src, dst); size = os.path.getsize(dst)
        db.session.add(FileMetadata(
            user_id=current_user.id, parent_path=dest_path, name=copy_name,
            storage_name=new_sn, size=size, is_directory=False, is_deleted=False
        ))
    db.session.commit()
    flash(f'Скопировано: «{copy_name}»', 'success')
    return redirect(url_for('files.list_files', path=return_path))

# ── Folders list (for move picker) ───────────────────────────────────────────

@files_bp.route('/folders-list')
@login_required
def folders_list():
    folders = FileMetadata.query.filter_by(
        user_id=current_user.id, is_directory=True, is_deleted=False
    ).order_by(FileMetadata.parent_path, FileMetadata.name).all()
    result = [{'id': f.id, 'name': f.name, 'path': f.parent_path + f.name + '/'}
              for f in folders]
    result.insert(0, {'id': 0, 'name': 'Файлы (корень)', 'path': '/'})
    return jsonify(result)

# ── Move ──────────────────────────────────────────────────────────────────────

@files_bp.route('/move', methods=['POST'])
@login_required
def move_item():
    item_id = request.form.get('item_id')
    dest_path = request.form.get('dest_path', '/')
    return_path = request.form.get('return_path', '/')
    try:
        dest_path = normalize_path(dest_path)
    except ValueError:
        flash('Некорректный путь назначения', 'danger')
        return redirect(url_for('files.list_files', path=return_path))
    item = FileMetadata.query.filter_by(
        id=item_id, user_id=current_user.id, is_deleted=False
    ).first()
    if not item:
        flash('Элемент не найден', 'danger')
        return redirect(url_for('files.list_files', path=return_path))
    if item.parent_path == dest_path:
        flash('Элемент уже находится в этой папке', 'warning')
        return redirect(url_for('files.list_files', path=return_path))
    # Prevent moving a folder into itself
    if item.is_directory:
        item_full = item.parent_path + item.name + '/'
        if dest_path.startswith(item_full):
            flash('Нельзя переместить папку внутрь самой себя', 'danger')
            return redirect(url_for('files.list_files', path=return_path))
    # Name conflict check
    conflict = FileMetadata.query.filter_by(
        user_id=current_user.id, parent_path=dest_path,
        name=item.name, is_deleted=False
    ).first()
    if conflict:
        flash(f'В папке назначения уже есть элемент «{item.name}»', 'danger')
        return redirect(url_for('files.list_files', path=return_path))
    if item.is_directory:
        old_path = item.parent_path + item.name + '/'
        new_path = dest_path + item.name + '/'
        # Update all nested children
        children = FileMetadata.query.filter(
            FileMetadata.user_id == current_user.id,
            FileMetadata.parent_path.like(old_path + '%')
        ).all()
        for child in children:
            child.parent_path = new_path + child.parent_path[len(old_path):]
        direct = FileMetadata.query.filter_by(
            user_id=current_user.id, parent_path=old_path
        ).all()
        for child in direct:
            child.parent_path = new_path
    item.parent_path = dest_path
    db.session.commit()
    flash(f'«{item.name}» перемещён', 'success')
    return redirect(url_for('files.list_files', path=return_path))

# ── Share ─────────────────────────────────────────────────────────────────────

@files_bp.route('/share', methods=['POST'])
@login_required
def share_file():
    path = request.args.get('path', '')
    if not path or path.endswith('/'):
        flash('Можно поделиться только файлом', 'danger')
        return redirect(url_for('files.list_files'))
    try:
        name, parent = _parse_file_path(path)
    except Exception:
        flash('Некорректный путь', 'danger')
        return redirect(url_for('files.list_files'))
    file_record = FileMetadata.query.filter_by(
        user_id=current_user.id, parent_path=parent, name=name,
        is_directory=False, is_deleted=False
    ).first()
    if not file_record:
        flash('Файл не найден', 'danger')
        return redirect(url_for('files.list_files', path=parent))
    token = str(uuid.uuid4())
    expires_at = datetime.utcnow() + timedelta(days=7)
    share = ShareLink(file_id=file_record.id, token=token, expires_at=expires_at)
    db.session.add(share)
    db.session.commit()
    share_url = url_for('public.download_shared', token=token, _external=True)
    flash(f'Ссылка создана: <a href="{share_url}" target="_blank" style="word-break:break-all">{share_url}</a>', 'success')
    return redirect(url_for('files.list_files', path=parent))

# ── Batch delete ──────────────────────────────────────────────────────────────

@files_bp.route('/delete-batch', methods=['POST'])
@login_required
def delete_batch():
    ids = request.form.getlist('ids[]')
    count = 0
    for item_id in ids:
        item = FileMetadata.query.filter_by(id=item_id, user_id=current_user.id, is_deleted=False).first()
        if item:
            item.is_deleted = True
            item.deleted_at = datetime.utcnow()
            count += 1
    db.session.commit()
    flash(f'Перемещено в корзину: {count} эл.', 'success')
    return_path = request.form.get('return_path', '/')
    return redirect(url_for('files.list_files', path=return_path))

# ── Batch move ────────────────────────────────────────────────────────────────

@files_bp.route('/move-batch', methods=['POST'])
@login_required
def move_batch():
    ids = request.form.getlist('ids[]')
    dest_path = request.form.get('dest_path', '/')
    return_path = request.form.get('return_path', '/')
    try:
        dest_path = normalize_path(dest_path)
    except ValueError:
        flash('Некорректный путь', 'danger')
        return redirect(url_for('files.list_files', path=return_path))
    count = 0
    for item_id in ids:
        item = FileMetadata.query.filter_by(id=item_id, user_id=current_user.id, is_deleted=False).first()
        if not item or item.parent_path == dest_path:
            continue
        if item.is_directory:
            old_path = item.parent_path + item.name + '/'
            new_path = dest_path + item.name + '/'
            if dest_path.startswith(old_path):
                continue
            children = FileMetadata.query.filter(
                FileMetadata.user_id == current_user.id,
                FileMetadata.parent_path.like(old_path + '%')
            ).all()
            for child in children:
                child.parent_path = new_path + child.parent_path[len(old_path):]
            for child in FileMetadata.query.filter_by(user_id=current_user.id, parent_path=old_path).all():
                child.parent_path = new_path
        item.parent_path = dest_path
        count += 1
    db.session.commit()
    flash(f'Перемещено: {count} эл.', 'success')
    return redirect(url_for('files.list_files', path=return_path))

# ── Disk space API ────────────────────────────────────────────────────────────

@files_bp.route('/api/disk-space')
@login_required
def api_disk_space():
    usage = sh.disk_usage('/')
    return jsonify({'free': usage.free, 'total': usage.total, 'used': usage.used})
