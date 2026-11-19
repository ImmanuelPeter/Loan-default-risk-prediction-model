from datetime import datetime, timezone
from urllib.parse import urlsplit

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash

from database import db
from models import AuditLog, User


auth = Blueprint('auth', __name__)


def log_auth_event(action, target_record, details, user=None):
    entry = AuditLog(
        organization_id=user.organization_id if user else None,
        user_id=user.id if user else None,
        user_code=user.user_code if user else 'GUEST',
        username=user.name if user else 'Visitor',
        action=action,
        target_record=target_record,
        details=details,
        ip_address=request.remote_addr or '127.0.0.1'
    )
    db.session.add(entry)
    db.session.commit()


@auth.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        destination = 'main.platform_admin' if current_user.is_platform_owner else 'main.profile'
        return redirect(url_for(destination))

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        remember = bool(request.form.get('remember'))
        user = User.query.filter_by(email=email).first()

        if not user or not check_password_hash(user.password, password):
            log_auth_event('Failed login attempt', email, 'Invalid credentials provided')
            flash('Invalid email or password. Please verify and try again.', 'danger')
            return render_template('login.html', email=email)

        if not user.is_platform_owner:
            if not user.organization or user.organization.verification_status == 'Pending Approval':
                log_auth_event(
                    'Login blocked',
                    email,
                    'Organization is awaiting platform approval',
                    user
                )
                flash('Your organization registration is waiting for platform approval.', 'warning')
                return render_template('login.html', email=email)
            if user.organization.verification_status != 'Verified':
                log_auth_event(
                    'Login blocked',
                    email,
                    f'Organization status is {user.organization.verification_status}',
                    user
                )
                flash('Your organization is suspended. Contact platform support.', 'warning')
                return render_template('login.html', email=email)
        if user.status != 'Active':
            log_auth_event(
                'Login blocked',
                email,
                f'Account status is {user.status}',
                user
            )
            flash(
                f'This account is {user.status.lower()}. Contact your organization administrator.',
                'warning'
            )
            return render_template('login.html', email=email)

        user.last_login = datetime.now(timezone.utc)
        db.session.commit()
        login_user(user, remember=remember)
        log_auth_event('User login', user.user_code, f'{user.name} logged in ({user.role})', user)
        flash(f'Welcome back, {user.name}!', 'success')

        next_page = request.args.get('next')
        if next_page and _is_safe_redirect_target(next_page):
            return redirect(next_page)
        destination = 'main.platform_admin' if user.is_platform_owner else 'main.profile'
        return redirect(url_for(destination))

    return render_template('login.html')


@auth.route('/signup', methods=['GET', 'POST'])
def signup():
    return redirect(url_for('auth.login'))


def _is_safe_redirect_target(target):
    parsed_target = urlsplit(target)
    return (
        parsed_target.scheme == ''
        and parsed_target.netloc == ''
        and parsed_target.path.startswith('/')
        and not parsed_target.path.startswith('//')
        and '\\' not in parsed_target.path
    )


@auth.route('/logout')
@login_required
def logout():
    log_auth_event('User logout', current_user.user_code, f'{current_user.name} logged out', current_user)
    logout_user()
    flash('You have been logged out securely.', 'info')
    return redirect(url_for('main.index'))
