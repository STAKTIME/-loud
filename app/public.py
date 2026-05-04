from datetime import datetime
import os
from flask import Blueprint, flash, send_file, current_app
from app import db
from app.models import ShareLink

public_bp = Blueprint('public', __name__)

def get_user_storage_path(user_id, storage_name):
    base = os.path.join(current_app.config['UPLOAD_BASE_DIR'], str(user_id))
    return os.path.join(base, storage_name)

@public_bp.route('/<token>')
def download_shared(token):
    share = ShareLink.query.filter_by(token=token).first()
    if not share:
        return 'Ссылка недействительна', 404
    if share.expires_at and share.expires_at < datetime.utcnow():
        db.session.delete(share)
        db.session.commit()
        return 'Срок действия ссылки истёк', 410
    file_record = share.file
    file_path = get_user_storage_path(file_record.user_id, file_record.storage_name)
    if not os.path.exists(file_path):
        return 'Файл не найден на сервере', 404
    return send_file(file_path, download_name=file_record.name, as_attachment=True)