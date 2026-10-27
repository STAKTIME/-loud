import glob
import io
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta

root = tempfile.mkdtemp(prefix='disk-share-verify-')
proc = None
try:
    os.environ['DATA_DIR'] = os.path.join(root, 'data')
    os.environ['STORAGE_DIR'] = os.path.join(root, 'storage')
    os.environ['SECRET_KEY'] = 'isolated-share-verification'
    os.makedirs(os.environ['DATA_DIR'], exist_ok=True)
    os.makedirs(os.environ['STORAGE_DIR'], exist_ok=True)
    sys.path.insert(0, os.getcwd())
    from app import create_app, db
    from app.models import Album, AlbumItem, Share, User

    storage = os.environ['STORAGE_DIR']
    shared_dir = os.path.join(storage, 'PublicFolder')
    nested_dir = os.path.join(shared_dir, 'Nested')
    os.makedirs(nested_dir)
    with open(os.path.join(shared_dir, 'hello.txt'), 'w', encoding='utf-8') as f:
        f.write('hello shared world')
    with open(os.path.join(nested_dir, 'deep.txt'), 'w', encoding='utf-8') as f:
        f.write('deep shared text')
    with open(os.path.join(shared_dir, '.hidden.txt'), 'w', encoding='utf-8') as f:
        f.write('secret')
    with open(os.path.join(storage, 'outside.txt'), 'w', encoding='utf-8') as f:
        f.write('outside')
    with open(os.path.join(storage, 'photo.jpg'), 'wb') as f:
        f.write(b'fake-image-data')

    app = create_app()
    app.config.update(TESTING=True, SERVER_NAME='localhost')
    with app.app_context():
        user = User(username='ShareVerifier', password_hash='unused', is_admin=True)
        db.session.add(user)
        db.session.flush()
        folder_share = Share(user_id=user.id, path='/PublicFolder', token='verify-folder-token',
                             expires_at=datetime.utcnow() + timedelta(days=1), allow_download=True)
        no_download = Share(user_id=user.id, path='/PublicFolder', token='verify-no-download-token',
                            expires_at=None, allow_download=False)
        file_share = Share(user_id=user.id, path='/PublicFolder/hello.txt', token='verify-file-token',
                           expires_at=None, allow_download=True)
        expired = Share(user_id=user.id, path='/PublicFolder', token='verify-expired-token',
                        expires_at=datetime.utcnow() - timedelta(days=1), allow_download=True)
        album = Album(user_id=user.id, name='Test album')
        db.session.add_all([folder_share, no_download, file_share, expired, album])
        db.session.flush()
        album_item = AlbumItem(album_id=album.id, path='/photo.jpg')
        album_share = Share(user_id=user.id, path='@album:' + str(album.id), token='verify-album-token',
                            expires_at=None, allow_download=True)
        db.session.add_all([album_item, album_share])
        db.session.commit()
        album_item_id = album_item.id
        user_id = user.id

    client = app.test_client()

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        print('PASS:', label)

    def status(path, expected):
        response = client.get(path)
        try:
            check(f'{path} -> {expected}', response.status_code == expected)
            return response.get_data()
        finally:
            response.close()

    folder_page = status('/public/verify-folder-token', 200)
    print('folder page excerpt:', folder_page[:900])
    check('folder interface and entries render', b'PublicFolder' in folder_page and
          b'hello.txt' in folder_page and b'Nested' in folder_page and
          b'.hidden.txt' not in folder_page)
    nested_page = status('/public/verify-folder-token?path=Nested', 200)
    check('nested folder and breadcrumbs render', b'deep.txt' in nested_page and b'Nested' in nested_page)
    child_page = status('/public/verify-folder-token?path=Nested/deep.txt', 200)
    check('nested file opens in public interface', b'deep.txt' in child_page and 'Назад к папке'.encode() in child_page)
    view = client.get('/public/verify-folder-token/view?path=Nested/deep.txt')
    try:
        check('nested file preview returns content', view.status_code == 200 and view.data == b'deep shared text')
    finally:
        view.close()
    status('/public/verify-folder-token?path=../outside.txt', 404)
    status('/public/verify-folder-token?path=%2e%2e%2Foutside.txt', 404)
    status('/public/verify-folder-token?path=/outside.txt', 404)
    status('/public/verify-folder-token?path=.hidden.txt', 404)
    status('/public/verify-file-token?path=Nested%2Fdeep.txt', 404)

    zip_response = client.get('/public/verify-folder-token/download')
    try:
        check('folder download returns ZIP', zip_response.status_code == 200 and zip_response.mimetype == 'application/zip')
        with zipfile.ZipFile(io.BytesIO(zip_response.data)) as archive:
            names = set(archive.namelist())
            check('ZIP has visible nested files only', names == {'hello.txt', 'Nested/deep.txt'})
    finally:
        zip_response.close()
    check('temporary ZIP removed on response close', not glob.glob(os.path.join(tempfile.gettempdir(), 'disk-share-*.zip')))
    status('/public/verify-no-download', 200)
    status('/public/verify-no-download/download', 403)
    status('/public/verify-expired-token', 410)
    status('/public/verify-file-token', 200)
    file_download = client.get('/public/verify-file-token/download')
    try:
        check('file download still works', file_download.status_code == 200 and file_download.data == b'hello shared world')
    finally:
        file_download.close()
    status('/public/verify-album-token', 200)
    status(f'/public/verify-album-token/album/{album_item_id}', 200)

    with client.session_transaction() as sess:
        sess['_user_id'] = str(user_id)
        sess['_fresh'] = True
    created = client.post('/files/share', data={'path': '/PublicFolder/', 'expires_days': '7', 'allow_download': '1'})
    try:
        check('share form accepts folder path', created.status_code == 302)
        redirect_to = created.location
    finally:
        created.close()
    with app.app_context():
        created_share = Share.query.filter_by(path='/PublicFolder').order_by(Share.id.desc()).first()
        check('folder share persisted', created_share is not None)
        created_token = created_share.token
    check('created share redirects back to folder', redirect_to.endswith('/files/?path=/'))
    check('created token is reachable as browser page', client.get('/public/' + created_token).status_code == 200)

    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = os.environ.copy()
    env['HOST'] = '127.0.0.1'
    env['PORT'] = str(port)
    proc = subprocess.Popen([sys.executable, 'main.py'], cwd=os.getcwd(), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        url = f'http://127.0.0.1:{port}/public/verify-folder-token'
        deadline = time.time() + 15
        while time.time() < deadline:
            if proc.poll() is not None:
                err = proc.stderr.read().decode('utf-8', 'replace')
                raise RuntimeError(f'Waitress exited early ({proc.returncode}): {err}')
            try:
                with urllib.request.urlopen(url, timeout=1) as response:
                    body = response.read()
                    check('live Waitress renders shared folder', response.status == 200 and
                          b'PublicFolder' in body and b'hello.txt' in body)
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/public/verify-folder-token/download', timeout=5) as response:
                    data = response.read()
                    check('live HTTP ZIP download works', response.status == 200 and zipfile.is_zipfile(io.BytesIO(data)))
                break
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                time.sleep(0.15)
        else:
            raise TimeoutError('Waitress did not become ready')
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        check('Waitress process stopped', proc.poll() is not None)
        if proc.stderr:
            proc.stderr.close()
        proc = None
finally:
    if proc is not None:
        proc.kill()
        proc.wait()
    shutil.rmtree(root, ignore_errors=True)
    print('isolated test data cleaned:', not os.path.exists(root))
