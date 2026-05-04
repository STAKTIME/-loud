import shutil
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from config import Config

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = 'auth.login'
login_manager.login_message = 'Пожалуйста, войдите для доступа к этой странице.'

def create_app():
    app = Flask(__name__, template_folder='../templates')
    app.config.from_object(Config)

    db.init_app(app)
    login_manager.init_app(app)

    from app.models import User

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from app.auth import auth_bp
    from app.files import files_bp
    from app.public import public_bp

    app.register_blueprint(auth_bp, url_prefix='/auth')
    app.register_blueprint(files_bp, url_prefix='/files')
    app.register_blueprint(public_bp, url_prefix='/public')

    from flask import redirect, url_for
    @app.route('/')
    def index():
        return redirect(url_for('files.list_files'))

    @app.context_processor
    def inject_globals():
        from flask_login import current_user
        ctx = {}
        # Real disk space
        try:
            disk = shutil.disk_usage('/')
            ctx['disk_total'] = disk.total
            ctx['disk_free'] = disk.free
            ctx['disk_used_sys'] = disk.used
        except Exception:
            ctx['disk_total'] = 0
            ctx['disk_free'] = 0
            ctx['disk_used_sys'] = 0

        # User storage used (sum of all non-deleted files)
        if current_user.is_authenticated:
            from sqlalchemy import func
            from app.models import FileMetadata
            used = db.session.query(func.sum(FileMetadata.size)).filter_by(
                user_id=current_user.id, is_directory=False, is_deleted=False
            ).scalar() or 0
            ctx['storage_used'] = used
        else:
            ctx['storage_used'] = 0
        return ctx

    with app.app_context():
        db.create_all()
        # Migrate existing databases: add new columns if missing
        from sqlalchemy import text
        with db.engine.connect() as conn:
            for col, defn in [
                ('is_deleted', 'BOOLEAN NOT NULL DEFAULT 0'),
                ('deleted_at', 'DATETIME'),
            ]:
                try:
                    conn.execute(text(f'ALTER TABLE files ADD COLUMN {col} {defn}'))
                    conn.commit()
                except Exception:
                    pass

    return app
