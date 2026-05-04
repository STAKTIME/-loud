from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_user, logout_user, login_required, current_user
from passlib.hash import bcrypt
from app import db
from app.models import User

auth_bp = Blueprint('auth', __name__)

@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('files.list_files'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if not email or not username or not password:
            flash('Заполните все поля', 'danger')
            return render_template('register.html')
        if User.query.filter_by(email=email).first():
            flash('Пользователь с таким email уже существует', 'danger')
            return render_template('register.html')
        if User.query.filter_by(username=username).first():
            flash('Это имя пользователя уже занято', 'danger')
            return render_template('register.html')
        password_hash = bcrypt.hash(password)
        user = User(email=email, username=username, password_hash=password_hash)
        db.session.add(user)
        db.session.commit()
        flash('Регистрация успешна! Войдите в аккаунт.', 'success')
        return redirect(url_for('auth.login'))
    return render_template('register.html')

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('files.list_files'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = User.query.filter_by(username=username).first()
        if not user or not bcrypt.verify(password, user.password_hash):
            flash('Неверное имя пользователя или пароль', 'danger')
            return render_template('login.html')
        login_user(user)
        return redirect(url_for('files.list_files'))
    return render_template('login.html')

@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('auth.login'))
