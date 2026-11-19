import os

import click
from flask import Flask
from flask_login import LoginManager
from sqlalchemy import inspect, text
from werkzeug.security import generate_password_hash

from database import db
from models import User


def _ensure_registration_schema(app):
    additions = {
        'organizations': {
            'location': 'VARCHAR(100)',
            'logo_path': 'VARCHAR(255)',
        },
        'users': {
            'phone_number': 'VARCHAR(20)',
            'national_id': 'VARCHAR(30)',
        },
        'applicants': {
            'gender': 'VARCHAR(30)',
            'national_id': 'VARCHAR(30)',
            'existing_debt': 'FLOAT NOT NULL DEFAULT 0',
            'credit_history': 'TEXT',
            'whatsapp_opt_in': 'BOOLEAN NOT NULL DEFAULT 0',
            'supporting_document_path': 'VARCHAR(255)',
        },
        'loan_applications': {
            'supporting_document_path': 'VARCHAR(255)',
            'whatsapp_approval_status': "VARCHAR(30) NOT NULL DEFAULT 'Not sent'",
            'whatsapp_approval_sent_at': 'DATETIME',
            'whatsapp_approval_error': 'VARCHAR(255)',
            'whatsapp_approval_template': 'VARCHAR(100)',
            'credit_history_status': "VARCHAR(30) NOT NULL DEFAULT 'Not checked'",
            'prior_loan_count': 'INTEGER',
            'prior_loans_repaid': 'INTEGER',
            'late_payments_12m': 'INTEGER',
            'max_days_past_due_12m': 'INTEGER',
            'previous_default': 'BOOLEAN',
        },
        'model_versions': {
            'artifact_path': 'VARCHAR(255)',
        },
    }

    with db.engine.begin() as connection:
        inspector = inspect(connection)
        for table_name, columns in additions.items():
            existing_columns = {
                column['name'] for column in inspector.get_columns(table_name)
            }
            for column_name, column_type in columns.items():
                if column_name not in existing_columns:
                    connection.execute(text(
                        f'ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}'
                    ))
                    existing_columns.add(column_name)

        inspector = inspect(connection)
        has_unique_national_id = any(
            constraint.get('column_names') == ['national_id']
            for constraint in inspector.get_unique_constraints('users')
        ) or any(
            index.get('column_names') == ['national_id'] and index.get('unique')
            for index in inspector.get_indexes('users')
        )
        if not has_unique_national_id:
            connection.execute(text(
                'CREATE UNIQUE INDEX uq_users_national_id ON users (national_id)'
            ))

        inspector = inspect(connection)
        has_unique_application_borrower = any(
            constraint.get('column_names') == ['organization_id', 'applicant_id']
            for constraint in inspector.get_unique_constraints('loan_applications')
        ) or any(
            index.get('column_names') == ['organization_id', 'applicant_id']
            and index.get('unique')
            for index in inspector.get_indexes('loan_applications')
        )
        if not has_unique_application_borrower:
            has_duplicate_applications = connection.execute(text(
                'SELECT 1 FROM loan_applications '
                'GROUP BY organization_id, applicant_id '
                'HAVING COUNT(*) > 1 LIMIT 1'
            )).first()
            if has_duplicate_applications:
                app.logger.warning(
                    'Could not add the one-loan-per-borrower index because '
                    'existing loan applications contain duplicate borrowers.'
                )
            else:
                connection.execute(text(
                    'CREATE UNIQUE INDEX uq_loan_applications_one_per_borrower '
                    'ON loan_applications (organization_id, applicant_id)'
                ))

def create_app():
    app = Flask(__name__)
    app.config['SECRET_KEY'] = os.environ.get(
        'SECRET_KEY',
        'development-only-change-this-secret-key'
    )

    instance_dir = os.path.join(os.path.dirname(__file__), 'instance')
    os.makedirs(instance_dir, exist_ok=True)
    db_path = os.environ.get(
        'LOAN_RISK_DATABASE_PATH',
        os.path.join(instance_dir, 'loan_risk_v3_saas.sqlite')
    )
    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['MODEL_ARTIFACT_DIRECTORY'] = os.environ.get(
        'MODEL_ARTIFACT_DIRECTORY',
        os.path.join(instance_dir, 'model_artifacts')
    )
    app.config['SUPPORT_SMTP_HOST'] = os.environ.get('SUPPORT_SMTP_HOST')
    app.config['SUPPORT_SMTP_PORT'] = int(os.environ.get('SUPPORT_SMTP_PORT', '587'))
    app.config['SUPPORT_SMTP_USERNAME'] = os.environ.get('SUPPORT_SMTP_USERNAME')
    app.config['SUPPORT_SMTP_PASSWORD'] = os.environ.get('SUPPORT_SMTP_PASSWORD')
    app.config['SUPPORT_FROM_EMAIL'] = os.environ.get('SUPPORT_FROM_EMAIL')
    app.config['SUPPORT_SMTP_USE_TLS'] = os.environ.get(
        'SUPPORT_SMTP_USE_TLS',
        'true'
    ).lower() == 'true'
    app.config['SUPPORT_SMTP_USE_SSL'] = os.environ.get(
        'SUPPORT_SMTP_USE_SSL',
        'false'
    ).lower() == 'true'
    app.config['WHATSAPP_ACCESS_TOKEN'] = os.environ.get('WHATSAPP_ACCESS_TOKEN')
    app.config['WHATSAPP_PHONE_NUMBER_ID'] = os.environ.get('WHATSAPP_PHONE_NUMBER_ID')
    app.config['WHATSAPP_GRAPH_API_VERSION'] = os.environ.get('WHATSAPP_GRAPH_API_VERSION')
    app.config['WHATSAPP_APPROVAL_TEMPLATE_NAME'] = os.environ.get(
        'WHATSAPP_APPROVAL_TEMPLATE_NAME'
    )
    app.config['WHATSAPP_TEMPLATE_LANGUAGE'] = os.environ.get(
        'WHATSAPP_TEMPLATE_LANGUAGE'
    )
    app.config['WHATSAPP_APPROVAL_TEMPLATES'] = os.environ.get(
        'WHATSAPP_APPROVAL_TEMPLATES'
    )

    db.init_app(app)

    login_manager = LoginManager()
    login_manager.login_view = 'auth.login'
    login_manager.login_message = 'Please log in to access your organization workspace.'
    login_manager.login_message_category = 'info'
    login_manager.init_app(app)

    @login_manager.user_loader
    def load_user(user_id):
        user = db.session.get(User, int(user_id))
        if not user or user.status != 'Active':
            return None
        if user.is_platform_owner:
            return user
        if user.organization and user.organization.verification_status == 'Verified':
            return user
        return None

    from auth import auth as auth_blueprint
    from main import main as main_blueprint

    app.register_blueprint(auth_blueprint)
    app.register_blueprint(main_blueprint)

    with app.app_context():
        db.create_all()
        _ensure_registration_schema(app)

    @app.cli.command('create-platform-owner')
    def create_platform_owner():
        """Create the first platform operator from a trusted local terminal."""
        if User.query.filter_by(role='Platform Owner').first():
            raise click.ClickException('A platform owner account already exists.')

        name = click.prompt('Platform owner name')
        email = click.prompt('Platform owner email').strip().lower()
        if not name.strip() or not email:
            raise click.ClickException('Name and email are required.')
        if User.query.filter_by(email=email).first():
            raise click.ClickException('That email address is already in use.')

        password = click.prompt(
            'Platform owner password',
            hide_input=True,
            confirmation_prompt=True
        )
        if len(password) < 12:
            raise click.ClickException('Use a password with at least 12 characters.')

        db.session.add(User(
            organization_id=None,
            user_code='SYS-001',
            name=name.strip(),
            email=email,
            password=generate_password_hash(password, method='scrypt'),
            role='Platform Owner',
            status='Active'
        ))
        db.session.commit()
        click.echo('Platform owner account created.')

    return app


if __name__ == '__main__':
    create_app().run(debug=True)
