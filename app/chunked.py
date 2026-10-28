"""Загрузка больших файлов по частям (chunked upload) с докачкой.

Схема:
1. POST /files/chunked/start  — клиент сообщает имя, размер файла и размер
   части. Сервер создаёт сессию загрузки и папку data/chunked/<id>/.
2. POST /files/chunked/<id>/<номер>  — бинарные данные одной части,
   сохраняются как data/chunked/<id>/<номер>. Повторная отправка принятой
   части — просто перезапись (идемпотентно), поэтому повторы при обрыве
   безопасны.
3. GET  /files/chunked/<id>/status  — какие части уже есть: клиент
   продолжает только с недостающих (докачка).
4. POST /files/chunked/<id>/complete  — все части в сборе: сервер склеивает
   их в один файл в папку пользователя, записывает в историю и удаляет
   временные данные.
5. POST /files/chunked/<id>/abort  — отменить и стереть принятые части.

Сессии живут в базе (UploadSession), поэтому докачка переживает перезапуск
сервера и закрытие вкладки: список незавершённых загрузок виден через
GET /files/chunked/pending.
"""
import os
import shutil
import threading
from datetime import datetime
from flask import Blueprint, request, current_app, jsonify, abort
from flask_login import login_required, current_user
from markupsafe import escape

from app import db
from app.models import UploadSession
from app.storage import resolve, clean_name, unique_path, to_vpath, quota_exceeded

chunked_bp = Blueprint('chunked', __name__)

MIN_CHUNK = 512 * 1024            # 512 КБ
MAX_CHUNK = 64 * 1024 * 1024      # 64 МБ
MAX_CONCURRENT_CHUNK_SIZE = 64 * 1024 * 1024  # один запрос с частью не больше этого
SESSION_TTL_DAYS = 7              # старые незавершённые загрузки чистим через неделю

# Склейка больших файлов идёт с диска на диск — блокируем сессию,
# чтобы «complete» дважды не собрал один файл.
_complete_locks = {}
_complete_locks_guard = threading.Lock()


def _sessions_dir():
    d = os.path.join(current_app.config['DATA_DIR'], 'chunked')
    os.makedirs(d, exist_ok=True)
    return d


def _session_dir(upload_id):
    return os.path.join(_sessions_dir(), upload_id)


def _my_session(upload_id):
    """Сессия загрузки текущего пользователя или 404."""
    s = db.session.get(UploadSession, upload_id)
    if not s or s.user_id != current_user.id:
        abort(404)
    return s


def _assembled_size(upload_id):
    """Сколько байт уже фактически лежит в частях (по файлам на диске)."""
    d = _session_dir(upload_id)
    total = 0
    try:
        with os.scandir(d) as it:
            for entry in it:
                try:
                    total += entry.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _cleanup_old_sessions():
    """Удаляем протухшие сессии (и их файлы) при обращении к списку."""
    from datetime import datetime, timedelta
    cutoff = datetime.utcnow() - timedelta(days=SESSION_TTL_DAYS)
    old = UploadSession.query.filter(UploadSession.updated_at < cutoff).all()
    for s in old:
        shutil.rmtree(_session_dir(s.id), ignore_errors=True)
        db.session.delete(s)
    if old:
        db.session.commit()


@chunked_bp.route('/chunked/pending', methods=['GET'])
@login_required
def pending_uploads():
    """Незавершённые загрузки текущего пользователя — для предложения докачать."""
    _cleanup_old_sessions()
    rows = []
    for s in UploadSession.query.filter_by(user_id=current_user.id) \
            .order_by(UploadSession.created_at.desc()).all():
        rows.append({
            'id': s.id,
            'filename': s.filename,
            'folder': s.folder,
            'size': s.size,
            'chunk_size': s.chunk_size,
            'total_chunks': s.total_chunks,
            'received_chunks': _received_chunks(s.id),
        })
    return jsonify(rows)


def _received_chunks(upload_id):
    """Номера частей, файлы которых уже лежат на диске."""
    d = _session_dir(upload_id)
    got = []
    try:
        with os.scandir(d) as it:
            for entry in it:
                if entry.is_file():
                    try:
                        got.append(int(entry.name))
                    except ValueError:
                        continue
    except OSError:
        pass
    return sorted(got)


@chunked_bp.route('/chunked/start', methods=['POST'])
@login_required
def start():
    """Создать сессию загрузки. Ожидает JSON: filename, size, chunk_size, folder."""
    data = request.get_json(silent=True) or {}
    name = clean_name(data.get('filename') or '')
    if not name:
        return jsonify({'error': 'Некорректное имя файла'}), 400
    try:
        size = int(data.get('size') or 0)
        chunk_size = int(data.get('chunk_size') or 0)
    except (TypeError, ValueError):
        return jsonify({'error': 'Некорректные параметры'}), 400
    if size <= 0:
        return jsonify({'error': 'Пустой файл загружать по частям не нужно'}), 400
    if not (MIN_CHUNK <= chunk_size <= MAX_CHUNK):
        return jsonify({'error': f'Размер части должен быть от {MIN_CHUNK // 1024} КБ до {MAX_CHUNK // 1024 ** 2} МБ'}), 400

    folder = data.get('folder') or '/'
    try:
        resolve(current_user, folder)  # проверяет существование и права
    except Exception:
        return jsonify({'error': 'Папка назначения недоступна'}), 404

    if quota_exceeded(current_user, size):
        return jsonify({'error': 'Превышен лимит места на диске'}), 403

    import secrets
    upload_id = secrets.token_hex(16)
    os.makedirs(_session_dir(upload_id), exist_ok=True)
    db.session.add(UploadSession(id=upload_id, user_id=current_user.id, filename=name,
                                 folder=folder, size=size, chunk_size=chunk_size,
                                 total_chunks=(size + chunk_size - 1) // chunk_size))
    db.session.commit()
    return jsonify({'id': upload_id, 'total_chunks': (size + chunk_size - 1) // chunk_size})


@chunked_bp.route('/chunked/<upload_id>/<int:number>', methods=['PUT', 'POST'])
@login_required
def upload_chunk(upload_id, number):
    """Принять одну часть (тело запроса — сырые байты части)."""
    s = _my_session(upload_id)
    if not (0 <= number < s.total_chunks):
        return jsonify({'error': 'Номер части вне диапазона'}), 400
    blob = request.get_data(cache=False)
    if blob is None:
        blob = b''
    expected = min(s.chunk_size, s.size - number * s.chunk_size)
    if len(blob) != expected:
        return jsonify({'error': f'Размер части неверный: получено {len(blob)}, ожидалось {expected}'}), 400
    if quota_exceeded(current_user, len(blob)):
        return jsonify({'error': 'Превышен лимит места на диске'}), 403

    tmp = os.path.join(_session_dir(upload_id), f'.tmp{number}')
    dst = os.path.join(_session_dir(upload_id), str(number))
    try:
        with open(tmp, 'wb') as f:
            f.write(blob)
        os.replace(tmp, dst)
    except OSError as e:
        return jsonify({'error': f'Не удалось сохранить часть: {escape(e.strerror or e)}'}), 500
    s.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True, 'chunk': number,
                    'received': len(_received_chunks(upload_id)), 'total': s.total_chunks})


@chunked_bp.route('/chunked/<upload_id>/status', methods=['GET'])
@login_required
def status(upload_id):
    s = _my_session(upload_id)
    return jsonify({'id': s.id, 'filename': s.filename, 'size': s.size,
                    'chunk_size': s.chunk_size, 'total_chunks': s.total_chunks,
                    'received_chunks': _received_chunks(s.id)})


@chunked_bp.route('/chunked/<upload_id>/complete', methods=['POST'])
@login_required
def complete(upload_id):
    """Склеить части в готовый файл в папку пользователя."""
    s = _my_session(upload_id)
    d = _session_dir(upload_id)

    have = set(_received_chunks(upload_id))
    missing = [i for i in range(s.total_chunks) if i not in have]
    if missing:
        # Полный список: для гигантских файлов это тысячи номеров, но это
        # просто числа — пусть клиент досылает всё за один заход.
        return jsonify({'error': 'Не хватает частей', 'missing_chunks': missing}), 400

    with _complete_locks_guard:
        lock = _complete_locks.setdefault(upload_id, threading.Lock())
    if not lock.acquire(blocking=False):
        return jsonify({'error': 'Файл уже собирается'}), 409
    try:
        folder = resolve(current_user, s.folder)
        target = unique_path(folder, s.filename)
        tmp = target + '.part'
        received = 0
        try:
            with open(tmp, 'wb') as out:
                for i in range(s.total_chunks):
                    with open(os.path.join(d, str(i)), 'rb') as part:
                        copied = shutil.copyfileobj(part, out, length=1024 * 1024)
                        received += copied if copied is not None else 0
            if os.path.getsize(tmp) != s.size:
                os.remove(tmp)
                return jsonify({'error': 'Размер собранного файла не совпал — загрузку придётся начать заново'}), 500
            os.replace(tmp, target)
        except OSError as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return jsonify({'error': f'Не удалось собрать файл: {escape(e.strerror or e)}'}), 500

        # Готово: история, чистка временной папки и записи.
        from app.files import _record
        vpath = to_vpath(current_user, target)
        _record('upload', vpath)
        db.session.delete(s)
        db.session.commit()
        shutil.rmtree(d, ignore_errors=True)
        return jsonify({'ok': True, 'filename': os.path.basename(target), 'path': vpath})
    finally:
        lock.release()
        with _complete_locks_guard:
            _complete_locks.pop(upload_id, None)


@chunked_bp.route('/chunked/<upload_id>/abort', methods=['POST'])
@login_required
def abort_upload(upload_id):
    """Отменить загрузку и стереть принятые части."""
    s = _my_session(upload_id)
    shutil.rmtree(_session_dir(s.id), ignore_errors=True)
    db.session.delete(s)
    db.session.commit()
    return jsonify({'ok': True})
