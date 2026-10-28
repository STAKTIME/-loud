import shutil
from flask import Flask, redirect, url_for, render_template, request
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_required
from config import Config
from app.csrf import init_csrf

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = 'auth.login'
login_manager.login_message = None  # на странице входа и так всё понятно
login_manager.login_message_category = 'info'

def create_app():
    app = Flask(__name__, template_folder='../templates')
    app.config.from_object(Config)

    db.init_app(app)
    login_manager.init_app(app)
    init_csrf(app)

    from app.models import User

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    from app.auth import auth_bp
    from app.files import files_bp
    from app.public import public_bp
    from app.admin import admin_bp
    from app.chunked import chunked_bp

    app.register_blueprint(auth_bp, url_prefix='/auth')
    app.register_blueprint(files_bp, url_prefix='/files')
    app.register_blueprint(public_bp, url_prefix='/public')
    app.register_blueprint(admin_bp, url_prefix='/admin')
    app.register_blueprint(chunked_bp, url_prefix='/files')

    @app.route('/')
    def index():
        return redirect(url_for('files.list_files'))

    @app.route('/help')
    @login_required
    def help():
        return render_template('help.html')

    # Эти обработчики перекрывают обработчики public_bp, поэтому для /public/
    # отдаём понятную страницу для гостя по ссылке.
    from app.public import public_error

    @app.errorhandler(403)
    def forbidden(e):
        if request.path.startswith('/public/'):
            return public_error(e)
        return render_template('error.html', code=403, text='Этот раздел только для администратора'), 403

    @app.errorhandler(400)
    def bad_request(e):
        text = getattr(e, 'description', None) or 'Некорректный запрос'
        return render_template('error.html', code=400, text=text), 400

    @app.errorhandler(404)
    def not_found(e):
        if request.path.startswith('/public/'):
            return public_error(e)
        return render_template('error.html', code=404, text='Такой страницы нет'), 404

    @app.errorhandler(413)
    def too_large(e):
        mb = app.config['MAX_CONTENT_LENGTH'] // (1024 * 1024)
        return render_template('error.html', code=413, text=f'Файл слишком большой (предел {mb} МБ)'), 413

    @app.template_filter('filesize')
    def filesize(b):
        b = b or 0
        for unit in ('Б', 'КБ', 'МБ', 'ГБ'):
            if b < 1024 or unit == 'ГБ':
                return f'{b:.0f} {unit}' if unit == 'Б' else f'{b:.1f} {unit}'
            b /= 1024

    @app.before_request
    def need_storage():
        # хранилище ещё не выбрано (например, база осталась со старой версии) — сначала выбрать
        from flask import request
        from flask_login import current_user
        from app.storage import storage_root
        if (current_user.is_authenticated and request.endpoint
                and not request.endpoint.startswith(('auth.', 'static', 'admin.storage'))
                and not storage_root()):
            if current_user.is_admin:
                return redirect(url_for('admin.storage'))
            return render_template('error.html', code=503, text='Администратор ещё не выбрал, где хранить файлы'), 503

    @app.context_processor
    def inject_globals():
        from flask_login import current_user
        from app.storage import storage_root, user_home
        ctx = {'disk_name': app.config['DISK_NAME'], 'folder_colors': ['#FFC21A'],
               'disk_total': 0, 'disk_free': 0, 'storage_where': ''}
        # Место на диске, где лежит хранилище
        try:
            if current_user.is_authenticated and storage_root():
                home = user_home(current_user)
                disk = shutil.disk_usage(home)
                ctx.update(disk_total=disk.total, disk_free=disk.free, storage_where=home)
        except Exception:
            pass
        return ctx

    with app.app_context():
        db.create_all()
        # SQLite не изменяет существующие таблицы через create_all().
        # Добавляем новые необязательные поля при обновлении старой базы.
        from sqlalchemy import inspect, text
        inspector = inspect(db.engine)
        if 'users' in inspector.get_table_names():
            columns = {c['name'] for c in inspector.get_columns('users')}
            if 'quota_gb' not in columns:
                with db.engine.begin() as conn:
                    conn.execute(text('ALTER TABLE users ADD COLUMN quota_gb FLOAT'))
            if 'full_access' not in columns:
                with db.engine.begin() as conn:
                    conn.execute(text('ALTER TABLE users ADD COLUMN full_access BOOLEAN NOT NULL DEFAULT 0'))
        if 'shares' in inspector.get_table_names():
            columns = {c['name'] for c in inspector.get_columns('shares')}
            if 'allow_download' not in columns:
                with db.engine.begin() as conn:
                    conn.execute(text('ALTER TABLE shares ADD COLUMN allow_download BOOLEAN NOT NULL DEFAULT 1'))
        # Записи удалённых пользователей (старые версии их не чистили): иначе новый
        # пользователь с тем же id получил бы чужие альбомы, ссылки и историю.
        with db.engine.begin() as conn:
            conn.execute(text('DELETE FROM album_items WHERE album_id NOT IN '
                              '(SELECT a.id FROM albums a JOIN users u ON u.id = a.user_id)'))
            for table in ('albums', 'activities', 'shares'):
                conn.execute(text(f'DELETE FROM {table} WHERE user_id NOT IN (SELECT id FROM users)'))

    return app
