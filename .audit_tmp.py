# -*- coding: utf-8 -*-
"""Полный аудит проекта: изолированная копия, реальный запуск, тесты всех маршрутов."""
import io, os, re, shutil, subprocess, sys, tempfile, time, zipfile
from datetime import datetime, timedelta

root = tempfile.mkdtemp(prefix='disk-audit-')
env = dict(os.environ)
env.update({
    'DATA_DIR':    os.path.join(root, 'data'),
    'STORAGE_DIR': os.path.join(root, 'storage'),
    'SECRET_KEY':  'audit-key',
    'HOST':        '127.0.0.1',
    'PORT':        '0',  # выберем сами
})
os.makedirs(env['DATA_DIR'], exist_ok=True)
os.makedirs(env['STORAGE_DIR'], exist_ok=True)

import socket
with socket.socket() as s:
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
env['PORT'] = str(port)

proc = subprocess.Popen([sys.executable, 'main.py'], cwd=r'D:\dis', env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding='utf-8', errors='replace')

import urllib.request, urllib.error, http.cookiejar

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None

cj = http.cookiejar.CookieJar()
opener_admin  = urllib.request.build_opener(NoRedirect, urllib.request.HTTPCookieProcessor(cj))
cj2 = http.cookiejar.CookieJar()
opener_user   = urllib.request.build_opener(NoRedirect, urllib.request.HTTPCookieProcessor(cj2))
cj3 = http.cookiejar.CookieJar()
opener_anon   = urllib.request.build_opener(NoRedirect, urllib.request.HTTPCookieProcessor(cj3))

BASE = f'http://127.0.0.1:{port}'
results, problems = [], []

def get(op, path, data=None, referer=None):
    from urllib.parse import quote, quote_plus
    path = quote(path, safe='/%?=&+')
    if data is not None:
        body = '&'.join(f'{quote_plus(str(k))}={quote_plus(str(v))}' for k, v in data.items()).encode('utf-8')
        req = urllib.request.Request(BASE + path, data=body, method='POST')
    else:
        req = urllib.request.Request(BASE + path)
    if referer:
        req.add_header('Referer', BASE + quote(referer, safe='/%?=&+'))
    try:
        r = op.open(req, timeout=10)
        return r.getcode(), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()

def post(op, path, data, referer=None):
    return get(op, path, data=data, referer=referer)

def check(label, cond, detail=''):
    results.append((label, bool(cond)))
    print(('PASS ' if cond else 'FAIL ') + label + (('  <-- ' + detail[:400]) if (not cond and detail) else ''))
    if not cond:
        problems.append(label + ((' | ' + detail[:400]) if detail else ''))

# ── ждём запуска ──
import urllib.request as ur
deadline = time.time() + 20
ready = False
while time.time() < deadline:
    if proc.poll() is not None:
        out = proc.stdout.read() if proc.stdout else ''
        print('СЕРВЕР УПАЛ ПРИ ЗАПУСКЕ:\n', out[:3000])
        sys.exit(1)
    try:
        with ur.urlopen(BASE + '/auth/login', timeout=1) as r:
            if r.getcode() == 200:
                ready = True
                break
    except Exception:
        time.sleep(0.2)
check('сервер запустился', ready)
if not ready:
    sys.exit(1)

# ── первый запуск (setup) ──
code, body = get(opener_admin, '/auth/setup')
check('setup доступен', code == 200, str(code))
code, body = get(opener_admin, '/auth/setup', data={
    'username': 'ГлавАдмин', 'password': 'adminpass', 'password2': 'adminpass',
    'storage_root': ''}, referer='/auth/setup')
check('setup создаёт админа', code == 302, f'{code} {body[:200]}')
code, body = get(opener_admin, '/files/')
check('админ вошёл, файлы открываются', code == 200, str(code))

# ── админ: папки и загрузка ──
code, body = get(opener_admin, '/files/?path=/')
check('корень хрананилища пуст', code == 200)
code, body = get(opener_admin, '/files/mkdir?path=/', data={'folder_name': 'Фото'}, referer='/files/')
check('mkdir Фото', code == 302, f'{code} {body[:300]}')
code, body = get(opener_admin, '/files/mkdir?path=/', data={'folder_name': 'Видео'}, referer='/files/')
check('mkdir Видео', code == 302)

# multipart-загрузка
boundary = '----AuditBoundary42'
def multipart(files, extra=None):
    buf = io.BytesIO()
    for name, (fname, content, ctype) in files:
        buf.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{fname}"\r\n'
                  f'Content-Type: {ctype}\r\n\r\n'.encode())
        buf.write(content)
        buf.write(b'\r\n')
    for k, v in (extra or {}).items():
        buf.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    buf.write(f'--{boundary}--\r\n'.encode())
    return buf.getvalue()

jpg = bytes([0xFF, 0xD8, 0xFF, 0xE0]) + b'A' * 200 + bytes([0xFF, 0xD9])
mp4 = bytes([0, 0, 0, 0x18]) + b'ftypmp42' + b'V' * 400
data = multipart([('file', ('фото 1.jpg', jpg, 'image/jpeg')),
                  ('file', ('video 1.mp4', mp4, 'video/mp4'))])
req = urllib.request.Request(BASE + '/files/upload?path=/', data=data, method='POST')
req.add_header('Content-Type', f'multipart/form-data; boundary={boundary}')
try:
    r = opener_admin.open(req, timeout=10)
    code, body = r.getcode(), r.read()
except urllib.error.HTTPError as e:
    code, body = e.code, e.read()
check('загрузка фото+видео в корень', code == 302, f'{code} {body[:300]}')

code, body = get(opener_admin, '/files/?path=/')
check('файлы видны в корне', 'фото 1.jpg'.encode() in body and 'video 1.mp4'.encode() in body, body[:200])

# ── переименование, копирование, перемещение ──
code, body = get(opener_admin, '/files/rename', data={'item_id': '/фото 1.jpg', 'new_name': 'море.jpg', 'return_path': '/'}, referer='/files/')
check('переименование', code == 302, f'{code} {body[:200]}')
code, body = get(opener_admin, '/files/copy', data={'item_id': '/море.jpg', 'return_path': '/'}, referer='/files/')
check('копия рядом', code == 302)
code, body = get(opener_admin, '/files/?path=/')
check('копия создана', 'море — копия.jpg'.encode() in body)
code, body = get(opener_admin, '/files/copy-to', data={'item_id': '/море.jpg', 'dest_path': '/Фото/', 'return_path': '/'}, referer='/files/')
check('copy-to в Фото', code == 302)
code, body = get(opener_admin, '/files/?path=/Фото/')
check('файл в /Фото/', 'море.jpg'.encode() in body)
code, body = get(opener_admin, '/files/move', data={'item_id': '/video 1.mp4', 'dest_path': '/Видео/', 'return_path': '/'}, referer='/files/')
check('перемещение видео', code == 302)
code, body = get(opener_admin, '/files/?path=/Видео/')
check('видео в /Видео/', 'video 1.mp4'.encode() in body)

# путь за пределами
code, body = get(opener_admin, '/files/?path=/../../../etc')
check('path traversal заблокирован', code == 404, str(code))
code, body = get(opener_admin, '/files/preview?path=/../../etc/passwd')
check('traversal preview заблокирован', code == 404, str(code))

# ── альбомы ──
code, body = get(opener_admin, '/files/albums')
check('страница альбомов', code == 200)
code, body = get(opener_admin, '/files/albums/create', data={'name': 'Море 2024'}, referer='/files/albums')
check('создание альбома', code == 302, f'{code} {body[:300]}')
code, body = get(opener_admin, '/files/albums')
m = re.search(r'/files/albums/(\d+)', body.decode('utf-8'))
aid = m.group(1) if m else '1'
check('альбом в списке', m is not None, body[:300])
code, body = get(opener_admin, f'/files/albums/{aid}')
check('открытие альбома', code == 200)

# добавить существующее фото
code, body = get(opener_admin, f'/files/albums/{aid}/add', data={'paths': '/Фото/море.jpg'}, referer=f'/files/albums/{aid}')
check('добавление фото из диска', code == 302, f'{code} {body[:300]}')
code, body = get(opener_admin, f'/files/albums/{aid}')
check('фото в альбоме', 'море.jpg'.encode() in body)

# загрузка в альбом
data = multipart([('photos', ('новое.jpg', jpg, 'image/jpeg'))], {'dest_path': '/Фото/'})
req = urllib.request.Request(BASE + f'/files/albums/{aid}/upload', data=data, method='POST')
req.add_header('Content-Type', f'multipart/form-data; boundary={boundary}')
try:
    r = opener_admin.open(req, timeout=10)
    code, body = r.getcode(), r.read()
except urllib.error.HTTPError as e:
    code, body = e.code, e.read()
check('загрузка фото через альбом', code == 302, f'{code} {body[:300]}')
code, body = get(opener_admin, f'/files/albums/{aid}')
check('новое фото в альбоме', 'новое.jpg'.encode() in body)

# видео в альбом — должно НЕ добавляться (только фото)
data = multipart([('photos', ('film.mp4', mp4, 'video/mp4'))], {'dest_path': '/Фото/'})
req = urllib.request.Request(BASE + f'/files/albums/{aid}/upload', data=data, method='POST')
req.add_header('Content-Type', f'multipart/form-data; boundary={boundary}')
try:
    r = opener_admin.open(req, timeout=10)
    code, body = r.getcode(), r.read()
except urllib.error.HTTPError as e:
    code, body = e.code, e.read()
code2, body2 = get(opener_admin, f'/files/albums/{aid}')
check('видео в альбом не добавляется (только фото)', 'film.mp4'.encode() not in body2)

# ── пользователь ──
code, body = get(opener_admin, '/admin/users')
check('страница пользователей', code == 200)
code, body = get(opener_admin, '/admin/users/add', data={'username': 'Мама', 'password': 'mamapass'}, referer='/admin/users')
check('добавление пользователя', code == 302, f'{code} {body[:300]}')
code, body = get(opener_admin, '/admin/users/add', data={'username': 'Мама2', 'password': 'mamapass'}, referer='/admin/users')
code2, body2 = get(opener_admin, '/admin/users')
check('дубликат пароля отклонён', code == 302 and 'Мама2'.encode() not in body2)

# логин обычного пользователя
code, body = get(opener_user, '/auth/login', data={'password': 'mamapass'}, referer='/auth/login')
check('пользователь вошёл', code == 302, f'{code} {body[:200]}')
code, body = get(opener_user, '/files/')
check('пользователь видит только свою папку', code == 200 and 'море.jpg'.encode() not in body)
code, body = get(opener_user, '/files/?path=/../')
check('пользователь не выходит за свою папку', code == 404, str(code))
code, body = get(opener_user, '/admin/users')
check('пользователь не видит админку', code in (302, 403), str(code))

# корзина
code, body = get(opener_admin, '/files/?path=/')
code, body = get(opener_admin, '/files/delete?path=/море — копия.jpg', data={'x': '1'}, referer='/files/')
check('удаление в корзину', code == 302, f'{code} {body[:200]}')
code, body = get(opener_admin, '/files/trash')
check('корзина содержит файл', 'море — копия.jpg'.encode() in body)
code, body = get(opener_admin, '/files/empty-trash', data={'x': '1'}, referer='/files/trash')
check('очистка корзины', code == 302)

# ссылки
code, body = get(opener_admin, '/files/share', data={'path': '/Фото/море.jpg', 'expires_days': '7', 'allow_download': '1'}, referer='/files/?path=/Фото/')
check('создание ссылки', code == 302, f'{code} {body[:300]}')
code, body = get(opener_admin, '/files/shares')
m = re.search(r'href="(http://[^"]*?|/public/[0-9a-f]+)"', body.decode('utf-8'))
check('список ссылок', code == 200)
m2 = re.search(r'/public/([0-9a-f]{32})', body.decode('utf-8'))
token = m2.group(1) if m2 else ''
check('токен найден в списке', bool(token), body[:500])
if token:
    code, body = get(opener_anon, f'/public/{token}')
    check('публичная ссылка открывается', code == 200, str(code))
    code, body = get(opener_anon, f'/public/{token}/download')
    check('скачивание по ссылке', code == 200 and body.startswith(bytes([0xFF, 0xD8])), str(code))

# история — перед этим обновляем логин админа (после сброса сессии его не существует)
code, body = get(opener_admin, '/auth/login', data={'password': 'adminpass'}, referer='/auth/login')
code, body = get(opener_admin, '/files/history')
check('история открывается', code == 200, f'{code} {body[:200]}')

# поиск
code, body = get(opener_admin, '/files/search?q=море')
check('поиск находит файл', code == 200 and 'море.jpg'.encode() in body, f'{code} {body[:300]}')

# сброс настроек: сейчас в коде НЕТ проверки пароля — любое POST админа сбрасывает всё
# проверяем фактическое поведение: неверный пароль не запрашивается вовсе
code, body = get(opener_admin, '/admin/reset-setup', data={'admin_password': 'WRONG'}, referer='/files/')
check('БАГ: сброс НЕ спрашивает пароль (достаточно одного клика)', code == 302 and b'/auth/setup' in body, f'{code} {body[:300]}')

print()
print('=' * 60)
print(f'ИТОГО: {sum(1 for _, ok in results if ok)}/{len(results)} пройдено')
if problems:
    print('ПРОВАЛЫ:')
    for p in problems:
        print('  -', p)

proc.terminate()
try:
    proc.wait(timeout=8)
except subprocess.TimeoutExpired:
    proc.kill()
shutil.rmtree(root, ignore_errors=True)
print('тестовые данные удалены:', not os.path.exists(root))
