"""Файлы хранятся как есть — обычными папками на диске.

Путь «на сайте» (например /Фото/море.jpg) отсчитывается от корня пользователя:
- администратор — от корня хранилища (например D:\\);
- обычный пользователь — от своей папки <хранилище>\\Пользователи\\<имя>.
Папка самой программы и её данные скрыты и недоступны.
"""
import os
import zlib
from datetime import datetime
from flask import current_app, abort
from app import db

BACKSLASH = chr(92)
HIDDEN_NAMES = {'$recycle.bin', 'system volume information', 'recovery', 'config.msi',
                'pagefile.sys', 'swapfile.sys', 'hiberfil.sys', 'dumpstack.log.tmp'}


BAD_NAME_MSG = 'Некорректное имя. Нельзя использовать знаки / ' + BACKSLASH + ' : * ? " &lt; &gt; |'


def check_storage_path(path):
    """(путь, None) если папку можно сделать хранилищем, иначе (None, текст ошибки)."""
    path = (path or '').strip().strip('"')
    if len(path) == 2 and path[1] == ':':
        path += BACKSLASH  # «D:» -> «D:\»
    if not path or not os.path.isabs(path):
        return None, 'Укажите полный путь, например D:' + BACKSLASH + ' или D:' + BACKSLASH + 'Диск'
    path = os.path.abspath(path)
    cfg = current_app.config
    if _inside(path, cfg['APP_DIR']) or _inside(path, cfg['DATA_DIR']):
        return None, 'Хранилище не может быть внутри папки самой программы'
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        return None, f'Не удалось открыть или создать папку: {e.strerror or e}'
    if not os.path.isdir(path):
        return None, 'Это не папка'
    return path, None


def storage_root():
    """Корень хранилища: STORAGE_DIR из .env или путь, выбранный при первом запуске."""
    from app.models import Setting
    root = current_app.config.get('STORAGE_DIR')
    if not root:
        s = db.session.get(Setting, 'storage_root')
        root = s.value if s else None
    return os.path.abspath(root) if root else None


def users_dir():
    return os.path.join(storage_root(), current_app.config['USERS_FOLDER'])


def user_home(user):
    if user.is_admin or getattr(user, 'full_access', False):
        return storage_root()
    return os.path.join(users_dir(), user.username)


def ensure_home(user):
    home = user_home(user)
    os.makedirs(home, exist_ok=True)
    return home

def user_usage(user):
    return folder_size(user_home(user))

def quota_exceeded(user, additional=0):
    return (not user.is_admin and not getattr(user, 'full_access', False) and user.quota_gb is not None and
            user_usage(user) + additional > user.quota_gb * 1024 ** 3)


def quota_exceeded_after_upload(user, additional=0):
    """Check the quota using the actual current contents plus upload size."""
    return (not user.is_admin and not getattr(user, 'full_access', False) and user.quota_gb is not None and
            user_usage(user) + additional > user.quota_gb * 1024 ** 3)


def _norm(p):
    return os.path.normcase(os.path.realpath(p))


def _inside(path, root):
    try:
        return os.path.commonpath([_norm(path), _norm(root)]) == _norm(root)
    except ValueError:  # другой диск
        return False


def is_protected(real):
    """Папка программы и её данные — не показываем и не трогаем."""
    cfg = current_app.config
    return _inside(real, cfg['APP_DIR']) or _inside(real, cfg['DATA_DIR'])


def is_hidden_name(name):
    return name.lower() in HIDDEN_NAMES or name.startswith(('.', '$'))


def bad_name(name):
    return (not name or '/' in name or BACKSLASH in name or name in ('.', '..')
            or any(c in name for c in '<>:"|?*') or name.endswith(('.', ' ')))


def clean_name(name):
    name = (name or '').replace(BACKSLASH, '/').split('/')[-1].strip()
    return '' if bad_name(name) else name


def norm_vpath(vpath, folder=False):
    """Путь на сайте: /a/b (папка — с / на конце). Запрещает .. и пустые части."""
    parts = [p for p in (vpath or '').replace(BACKSLASH, '/').split('/') if p]
    if any(p in ('.', '..') for p in parts):
        raise ValueError('bad path')
    path = '/' + '/'.join(parts)
    if folder and path != '/':
        path += '/'
    return path


def resolve(user, vpath, must_exist=True):
    """Путь на сайте -> настоящий путь на диске (или 404)."""
    try:
        vpath = norm_vpath(vpath)
    except ValueError:
        abort(404)
    home = ensure_home(user)
    real = os.path.join(home, *[p for p in vpath.split('/') if p])
    if not _inside(real, home) or (real != home and is_protected(real)):
        abort(404)
    if must_exist and not os.path.exists(real):
        abort(404)
    return real


def to_vpath(user, real):
    rel = os.path.relpath(real, user_home(user))
    return '/' if rel == '.' else '/' + rel.replace(BACKSLASH, '/')


def is_locked(user, real):
    """Нельзя удалять/переименовывать/переносить: корень, папку «Пользователи» и личные папки."""
    if _norm(real) == _norm(user_home(user)):
        return True
    if user.is_admin or getattr(user, 'full_access', False):
        ud = users_dir()
        return _norm(real) == _norm(ud) or _norm(os.path.dirname(real)) == _norm(ud)
    return False


class Entry:
    """Файл или папка для шаблонов (те же поля, что были у записи в базе)."""
    def __init__(self, user, real, st=None):
        st = st or os.stat(real)
        self.real = real
        self.name = os.path.basename(real.rstrip('\\/')) or real
        self.is_directory = os.path.isdir(real)
        self.size = 0 if self.is_directory else st.st_size
        self.created_at = datetime.fromtimestamp(st.st_mtime)
        vp = to_vpath(user, real)
        self.id = vp
        self.parent_path = norm_vpath(vp.rsplit('/', 1)[0], folder=True)
        self.color = zlib.crc32(self.name.encode('utf-8'))


def list_dir(user, real):
    """Содержимое папки без скрытых/системных и без папки программы."""
    items = []
    try:
        with os.scandir(real) as it:
            for e in it:
                if is_hidden_name(e.name) or is_protected(e.path):
                    continue
                try:
                    items.append(Entry(user, e.path, e.stat()))
                except OSError:
                    continue
    except OSError:
        return None
    items.sort(key=lambda x: (not x.is_directory, x.name.lower()))
    return items


def unique_path(folder, name):
    """«файл.txt» -> «файл (1).txt», если такое имя уже занято."""
    target = os.path.join(folder, name)
    base, ext = os.path.splitext(name)
    n = 1
    while os.path.exists(target):
        target = os.path.join(folder, f'{base} ({n}){ext}')
        n += 1
    return target


def folder_size(real, limit=200000):
    total = 0
    for i, (dirpath, _, files) in enumerate(os.walk(real)):
        if i > limit:
            break
        for f in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return total
