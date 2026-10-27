import os
import tempfile
import zipfile
from datetime import datetime
from flask import Blueprint, abort, make_response, render_template, request, send_file
from werkzeug.exceptions import NotFound
from app import db
from app.models import AlbumItem, Share, User
from app.storage import Entry, is_hidden_name, is_protected, list_dir, norm_vpath, resolve

public_bp = Blueprint('public', __name__)

PUBLIC_ERRORS = {
    404: ('ссылка не найдена', 'bi-link-45deg',
          'Такой ссылки нет',
          'Возможно, владелец её отключил или в адресе опечатка.',
          'Проверьте адрес целиком — ссылка длинная и чувствительна к символам.'),
    410: ('ссылка истекла', 'bi-hourglass-bottom',
          'Срок действия ссылки истёк',
          'Эта ссылка была временной и больше не работает.',
          'Попросите владельца создать новую ссылку.'),
    403: ('нет доступа', 'bi-shield-lock',
          'Доступ закрыт',
          'Владелец ссылки запретил скачивание этих файлов.',
          'Просмотр может быть доступен, а скачивание — нет.'),
}


@public_bp.app_errorhandler(404)
@public_bp.app_errorhandler(410)
@public_bp.app_errorhandler(403)
def public_error(error):
    """Понятная страница для тех, кто открыл ссылку без входа на диск."""
    if not request.path.startswith('/public/'):
        return error
    code = error.code if getattr(error, 'code', None) in PUBLIC_ERRORS else 404
    page, icon, title, text, hint = PUBLIC_ERRORS[code]
    response = make_response(render_template('error_public.html', icon=icon, title=title,
                                             text=text, hint=hint), code)
    response.headers['X-Robots-Tag'] = 'noindex, nofollow'
    return response


@public_bp.after_request
def _no_index(response):
    """Общие ссылки не должны попадать в поисковые системы."""
    response.headers.setdefault('X-Robots-Tag', 'noindex, nofollow')
    response.headers.setdefault('Referrer-Policy', 'no-referrer')
    return response


def _share_and_root(token):
    share = Share.query.filter_by(token=token).first_or_404()
    if share.expires_at and share.expires_at < datetime.utcnow():
        abort(410)
    user = db.session.get(User, share.user_id)
    if not user or share.path.startswith('@album:'):
        abort(404)
    try:
        root = resolve(user, share.path)
    except NotFound:
        abort(404)
    if not os.path.isfile(root) and not os.path.isdir(root):
        abort(404)
    return share, user, root


def _resolve_shared_path(user, root, root_vpath, requested=''):
    """Resolve a path only within the shared root file or folder."""
    if os.path.isfile(root):
        if requested:
            abort(404)
        return root, ''
    try:
        relative = norm_vpath(requested).strip('/') if requested else ''
        target_vpath = root_vpath.rstrip('/')
        if relative:
            target_vpath += '/' + relative
        real = resolve(user, target_vpath)
    except (NotFound, ValueError):
        abort(404)
    root_real = os.path.normcase(os.path.realpath(root))
    target_real = os.path.normcase(os.path.realpath(real))
    try:
        if os.path.commonpath([root_real, target_real]) != root_real:
            abort(404)
    except ValueError:
        abort(404)
    return real, relative


def _shared_context(token, path=''):
    share, user, root = _share_and_root(token)
    real, relative = _resolve_shared_path(user, root, share.path, path)
    if not os.path.isfile(real) and not os.path.isdir(real):
        abort(404)
    if any(is_hidden_name(part) for part in relative.split('/') if part):
        abort(404)
    if os.path.basename(real) and is_hidden_name(os.path.basename(real)):
        abort(404)
    if is_protected(real):
        abort(404)
    return share, user, root, real, relative


def _shared_entries(user, real):
    """List only visible files and folders inside a shared directory."""
    entries = list_dir(user, real)
    if entries is None:
        return []
    return [entry for entry in entries
            if not is_hidden_name(entry.name) and not is_protected(entry.real)]


def _zip_shared_folder(folder, archive_path):
    """Create an archive without following symlinks or including protected paths."""
    root_real = os.path.normcase(os.path.realpath(folder))
    with zipfile.ZipFile(archive_path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for current, dirs, files in os.walk(folder, followlinks=False):
            current_real = os.path.normcase(os.path.realpath(current))
            try:
                if os.path.commonpath([root_real, current_real]) != root_real:
                    dirs[:] = []
                    continue
            except ValueError:
                dirs[:] = []
                continue
            dirs[:] = [name for name in dirs
                       if not is_hidden_name(name)
                       and not os.path.islink(os.path.join(current, name))
                       and not is_protected(os.path.join(current, name))]
            for name in files:
                path = os.path.join(current, name)
                if is_hidden_name(name) or os.path.islink(path) or is_protected(path):
                    continue
                try:
                    if not os.path.isfile(path):
                        continue
                    relative = os.path.relpath(path, folder)
                    archive.write(path, relative)
                except OSError:
                    continue


@public_bp.route('/<token>/album/<int:item_id>')
def album_image(token, item_id):
    share = Share.query.filter_by(token=token).first_or_404()
    if share.expires_at and share.expires_at < datetime.utcnow():
        abort(410)
    if not share.path.startswith('@album:'):
        abort(404)
    album_id = int(share.path.split(':', 1)[1])
    from app.models import Album
    album = db.session.get(Album, album_id)
    if not album or album.user_id != share.user_id:
        abort(404)
    item = AlbumItem.query.filter_by(id=item_id, album_id=album_id).first_or_404()
    user = db.session.get(User, share.user_id)
    if not user:
        abort(404)
    try:
        real = resolve(user, item.path)
    except NotFound:
        abort(404)
    if not os.path.isfile(real):
        abort(404)
    response = send_file(real, as_attachment=False)
    if real.lower().endswith('.svg'):
        response.headers['Content-Security-Policy'] = 'sandbox'  # скрипты внутри SVG не выполняются
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@public_bp.route('/<token>/album/<int:item_id>/download')
def album_image_download(token, item_id):
    share = Share.query.filter_by(token=token).first_or_404()
    if share.expires_at and share.expires_at < datetime.utcnow():
        abort(410)
    if not share.path.startswith('@album:'):
        abort(404)
    if not share.allow_download:
        abort(403)
    album_id = int(share.path.split(':', 1)[1])
    from app.models import Album
    album = db.session.get(Album, album_id)
    if not album or album.user_id != share.user_id:
        abort(404)
    item = AlbumItem.query.filter_by(id=item_id, album_id=album_id).first_or_404()
    user = db.session.get(User, share.user_id)
    if not user:
        abort(404)
    try:
        real = resolve(user, item.path)
    except NotFound:
        abort(404)
    if not os.path.isfile(real):
        abort(404)
    return send_file(real, download_name=os.path.basename(real), as_attachment=True)


@public_bp.route('/<token>')
def browse_shared(token):
    path = request.args.get('path', '')
    if path == '':
        share = Share.query.filter_by(token=token).first_or_404()
        if share.expires_at and share.expires_at < datetime.utcnow():
            abort(410)
        if share.path.startswith('@album:'):
            from app.models import Album
            album = db.session.get(Album, int(share.path.split(':', 1)[1]))
            if not album or album.user_id != share.user_id:
                abort(404)
            items = AlbumItem.query.filter_by(album_id=album.id).all()
            return render_template('public_album.html', album=album, items=items,
                                   allow_download=share.allow_download, token=token)
    share, user, root, real, relative = _shared_context(token, path)
    if os.path.isfile(real):
        parent = relative.rsplit('/', 1)[0] if '/' in relative else ''
        return render_template('public_share.html', share=share, token=token,
                               root=root, root_name=os.path.basename(os.path.normpath(root)),
                               current=Entry(user, real), relative=relative,
                               parent=parent, items=[], is_file=True)
    items = _shared_entries(user, real)
    return render_template('public_share.html', share=share, token=token,
                           root=root, root_name=os.path.basename(os.path.normpath(root)),
                           current=real, relative=relative,
                           parent=relative.rsplit('/', 1)[0] if '/' in relative else '',
                           items=items, is_file=False)


@public_bp.route('/<token>/view')
def view_shared(token):
    share, user, root, real, _ = _shared_context(token, request.args.get('path', ''))
    if not os.path.isfile(real):
        abort(404)
    name = os.path.basename(real)
    ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    if ext in {'html', 'htm', 'xhtml', 'shtml', 'js', 'mjs', 'xml'}:
        response = send_file(real, mimetype='text/plain; charset=utf-8', as_attachment=False)
    else:
        response = send_file(real, download_name=name, as_attachment=False)
    if ext == 'svg':
        response.headers['Content-Security-Policy'] = 'sandbox'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


def _remove_archive(path):
    try:
        os.remove(path)
    except OSError:
        pass


@public_bp.route('/<token>/download')
def download_shared(token):
    share, user, root, real, relative = _shared_context(token, request.args.get('path', ''))
    if not share.allow_download:
        abort(403)
    if os.path.isfile(real):
        return send_file(real, download_name=os.path.basename(real), as_attachment=True)

    temp = tempfile.NamedTemporaryFile(prefix='disk-share-', suffix='.zip', delete=False)
    archive_path = temp.name
    temp.close()
    try:
        _zip_shared_folder(real, archive_path)
        folder_name = os.path.basename(os.path.normpath(real)) or 'shared-folder'
        response = send_file(archive_path, mimetype='application/zip',
                             as_attachment=True, download_name=folder_name + '.zip')
    except Exception:
        _remove_archive(archive_path)
        raise
    response.call_on_close(lambda: _remove_archive(archive_path))
    return response
