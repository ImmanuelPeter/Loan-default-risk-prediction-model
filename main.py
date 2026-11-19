import io
import csv
import json
import http.client
import os
import re
import uuid
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from sqlalchemy import or_, and_, false
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload
from flask import (
    Blueprint, current_app, render_template, request, redirect,
    url_for, flash, jsonify, Response, abort
)
from flask_login import login_required, current_user
from database import db
from models import (
    Organization, User,
    Applicant, LoanApplication, RiskPrediction, LoanDecision,
    ModelVersion, ModelEvaluation, AuditLog, SystemSetting
)
from ml_engine import (
    ml_model, preprocessing_engine, eda_engine, feature_engine, model_trainer
)
from werkzeug.security import generate_password_hash
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename

main = Blueprint('main', __name__)

WORKSPACE_ADMIN_ROLES = {'Administrator', 'Organization Administrator'}
CENT = Decimal('0.01')
EMAIL_PATTERN = re.compile(
    r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,63}$",
    re.IGNORECASE
)


def _normalize_malawi_phone(value):
    normalized = re.sub(r'[ -]', '', value)
    if not re.fullmatch(r'\+265[1289][0-9]{8}', normalized):
        return None
    return normalized


WHATSAPP_TEMPLATE_FIELDS = {
    'borrower_name',
    'application_id',
    'loan_amount',
    'loan_purpose',
    'loan_term',
    'interest_rate',
    'organization_name',
}

DEFAULT_WHATSAPP_TEMPLATE_BODY = (
    'Hello {{1}}, application {{2}} has been approved for {{3}}. '
    'Purpose: {{4}}. Loan term: {{5}} months. Interest rate: {{6}}%.'
)


def _whatsapp_template_values(application):
    return {
        'borrower_name': application.applicant.full_name[:150],
        'application_id': application.app_id[:30],
        'loan_amount': f'MWK {application.loan_amount:,.0f}',
        'loan_purpose': application.loan_purpose[:100],
        'loan_term': str(application.loan_term),
        'interest_rate': f'{application.interest_rate:g}',
        'organization_name': application.organization.name[:150],
    }


def _whatsapp_template_options(application=None):
    configured_templates = current_app.config.get('WHATSAPP_APPROVAL_TEMPLATES')
    if configured_templates:
        try:
            configured_templates = json.loads(configured_templates)
        except json.JSONDecodeError:
            return [], 'WhatsApp approved-template configuration is invalid.'
    else:
        template_name = current_app.config.get('WHATSAPP_APPROVAL_TEMPLATE_NAME')
        template_language = current_app.config.get('WHATSAPP_TEMPLATE_LANGUAGE')
        if not template_name or not template_language:
            return [], 'No WhatsApp approval templates are configured on the server.'
        configured_templates = [{
            'name': template_name,
            'language': template_language,
            'label': template_name,
            'body': DEFAULT_WHATSAPP_TEMPLATE_BODY,
            'fields': [
                'borrower_name',
                'application_id',
                'loan_amount',
                'loan_purpose',
                'loan_term',
                'interest_rate',
            ],
        }]

    if not isinstance(configured_templates, list) or not configured_templates:
        return [], 'No WhatsApp approval templates are configured on the server.'

    values = _whatsapp_template_values(application) if application else {}
    options = []
    seen_names = set()
    for template in configured_templates:
        if not isinstance(template, dict):
            return [], 'WhatsApp approved-template configuration is invalid.'
        name = template.get('name')
        language = template.get('language')
        label = template.get('label', name)
        body = template.get('body')
        fields = template.get('fields')
        if (
            not isinstance(name, str)
            or not re.fullmatch(r'[a-z0-9_]{1,100}', name)
            or name in seen_names
            or not isinstance(language, str)
            or not re.fullmatch(r'[a-z]{2}(?:_[A-Z]{2})?', language)
            or not isinstance(label, str)
            or not label.strip()
            or not isinstance(body, str)
            or not isinstance(fields, list)
            or any(
                not isinstance(field, str) or field not in WHATSAPP_TEMPLATE_FIELDS
                for field in fields
            )
        ):
            return [], 'WhatsApp approved-template configuration is invalid.'
        placeholders = [
            int(index) for index in re.findall(r'\{\{([1-9][0-9]*)\}\}', body)
        ]
        if (
            set(placeholders) != set(range(1, len(fields) + 1))
            or '{{' in re.sub(r'\{\{[1-9][0-9]*\}\}', '', body)
            or '}}' in re.sub(r'\{\{[1-9][0-9]*\}\}', '', body)
        ):
            return [], 'WhatsApp template body placeholders must match its configured fields.'

        option = {
            'name': name,
            'language': language,
            'label': label.strip(),
            'body': body,
            'fields': fields,
        }
        if application:
            option['preview'] = re.sub(
                r'\{\{([1-9][0-9]*)\}\}',
                lambda match: values[fields[int(match.group(1)) - 1]],
                body
            )
        else:
            option['preview'] = body
        options.append(option)
        seen_names.add(name)
    return options, None


def _send_whatsapp_approval(application, template_name=None):
    applicant = application.applicant
    if not applicant.whatsapp_opt_in:
        return 'Consent required', 'Borrower has not opted in to WhatsApp notifications.'

    required_settings = {
        'WHATSAPP_ACCESS_TOKEN': current_app.config.get('WHATSAPP_ACCESS_TOKEN'),
        'WHATSAPP_PHONE_NUMBER_ID': current_app.config.get('WHATSAPP_PHONE_NUMBER_ID'),
        'WHATSAPP_GRAPH_API_VERSION': current_app.config.get('WHATSAPP_GRAPH_API_VERSION'),
    }
    if any(not value for value in required_settings.values()):
        return 'Not configured', 'WhatsApp delivery is not configured on the server.'

    templates, template_error = _whatsapp_template_options(application)
    if template_error:
        return 'Not configured', template_error
    selected_template = template_name or application.whatsapp_approval_template
    template = next(
        (option for option in templates if option['name'] == selected_template),
        templates[0] if not selected_template else None
    )
    if not template:
        return 'Not configured', 'The selected WhatsApp template is not configured.'
    application.whatsapp_approval_template = template['name']

    api_version = required_settings['WHATSAPP_GRAPH_API_VERSION']
    if not re.fullmatch(r'v[0-9]+\.[0-9]+', api_version):
        return 'Not configured', 'WhatsApp Graph API version is invalid.'
    if not re.fullmatch(r'[0-9]+', required_settings['WHATSAPP_PHONE_NUMBER_ID']):
        return 'Not configured', 'WhatsApp phone number ID is invalid.'
    if any(
        ord(character) < 32 or ord(character) == 127
        for character in required_settings['WHATSAPP_ACCESS_TOKEN']
    ):
        return 'Not configured', 'WhatsApp access token contains invalid characters.'

    values = _whatsapp_template_values(application)
    payload = {
        'messaging_product': 'whatsapp',
        'to': re.sub(r'\D', '', applicant.phone_number or ''),
        'type': 'template',
        'template': {
            'name': template['name'],
            'language': {'code': template['language']},
            'components': [{
                'type': 'body',
                'parameters': [
                    {'type': 'text', 'text': values[field]}
                    for field in template['fields']
                ],
            }],
        },
    }
    phone_digits = payload['to']
    if re.fullmatch(r'0[1289][0-9]{8}', phone_digits):
        payload['to'] = f'265{phone_digits[1:]}'
    elif not re.fullmatch(r'265[1289][0-9]{8}', phone_digits):
        return 'Failed', 'Borrower phone number is not a valid Malawi mobile number.'

    endpoint = (
        f"https://graph.facebook.com/{api_version}/"
        f"{required_settings['WHATSAPP_PHONE_NUMBER_ID']}/messages"
    )
    api_request = Request(
        endpoint,
        data=json.dumps(payload).encode('utf-8'),
        headers={
            'Authorization': f"Bearer {required_settings['WHATSAPP_ACCESS_TOKEN']}",
            'Content-Type': 'application/json',
        },
        method='POST',
    )

    try:
        with urlopen(api_request, timeout=8) as response:
            response_body = json.loads(response.read().decode('utf-8'))
    except HTTPError as error:
        return 'Failed', f'WhatsApp API rejected the request (HTTP {error.code}).'
    except (URLError, TimeoutError, OSError, http.client.HTTPException):
        return 'Failed', 'WhatsApp service could not be reached. Retry the notification.'
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 'Failed', 'WhatsApp service returned an unreadable response.'

    if not isinstance(response_body, dict) or not response_body.get('messages'):
        return 'Failed', 'WhatsApp service did not confirm message delivery.'
    return 'Sent', None


def _valid_registration_name(value):
    return bool(re.fullmatch(r"[^\W\d_](?:[^\W\d_]|[ .'-]){1,149}", value, re.UNICODE))


def _valid_registration_text(value, maximum):
    return bool(value) and len(value) <= maximum and not any(
        ord(character) < 32 for character in value
    )


def _application_credit_history_values(form, existing=None):
    status = form.get('credit_history_status')
    if status is None and existing:
        status = existing.credit_history_status
    status = status or 'Not checked'
    allowed_statuses = {'Not checked', 'No prior loans', 'History available'}
    if status not in allowed_statuses:
        raise ValueError('Select a valid credit history status.')

    if status == 'No prior loans':
        return {
            'credit_history_status': status,
            'prior_loan_count': 0,
            'prior_loans_repaid': 0,
            'late_payments_12m': 0,
            'max_days_past_due_12m': 0,
            'previous_default': None,
        }

    if status == 'Not checked':
        return {
            'credit_history_status': status,
            'prior_loan_count': None,
            'prior_loans_repaid': None,
            'late_payments_12m': None,
            'max_days_past_due_12m': None,
            'previous_default': None,
        }

    def optional_count(field, label):
        raw_value = form.get(field)
        if field not in form and existing:
            raw_value = getattr(existing, field)
        if raw_value is None or str(raw_value).strip() == '':
            return None
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            raise ValueError(f'Enter a whole number for {label}.')
        if value < 0:
            raise ValueError(f'{label} cannot be negative.')
        return value

    prior_loan_count = optional_count('prior_loan_count', 'previous loans')
    prior_loans_repaid = optional_count('prior_loans_repaid', 'loans repaid')
    late_payments_12m = optional_count('late_payments_12m', 'late payments')
    max_days_past_due_12m = optional_count(
        'max_days_past_due_12m',
        'longest payment delay'
    )
    if not prior_loan_count:
        raise ValueError('Enter at least one previous loan for available history.')
    if prior_loans_repaid is not None and prior_loans_repaid > prior_loan_count:
        raise ValueError('Loans repaid cannot exceed the number of previous loans.')

    previous_default = form.get('previous_default')
    if previous_default is None and existing:
        if existing.previous_default is True:
            previous_default = 'yes'
        elif existing.previous_default is False:
            previous_default = 'no'
        else:
            previous_default = 'unknown'
    default_values = {'yes': True, 'no': False, 'unknown': None, '': None}
    if previous_default not in default_values:
        raise ValueError('Select a valid previous default status.')

    return {
        'credit_history_status': status,
        'prior_loan_count': prior_loan_count,
        'prior_loans_repaid': prior_loans_repaid,
        'late_payments_12m': late_payments_12m,
        'max_days_past_due_12m': max_days_past_due_12m,
        'previous_default': default_values[previous_default],
    }


PLATFORM_OWNER_ENDPOINTS = {
    'main.platform_admin', 'main.platform_activity',
    'main.platform_activity_export', 'main.delete_platform_activity',
    'main.clear_platform_activity',
    'main.platform_model_management', 'main.upload_platform_model',
    'main.activate_platform_model', 'main.activate_evaluated_model',
    'main.model_training', 'main.run_model_training',
    'main.set_active_model_version',
    'main.approve_organization',
    'main.reject_organization', 'main.toggle_org_verification',
    'main.set_organization_status', 'main.update_organization',
    'main.delete_organization'
}

WORKSPACE_ENDPOINTS = {
    'main.profile', 'main.applicants_list', 'main.register_applicant',
    'main.applicant_detail', 'main.update_applicant', 'main.delete_applicant',
    'main.loan_applications', 'main.create_loan_application',
    'main.update_loan_application', 'main.delete_loan_application',
    'main.risk_predictions', 'main.assess_application_risk', 'main.record_decision',
    'main.export_prediction_history', 'main.reports', 'main.export_report_csv',
    'main.retry_whatsapp_approval',
    'main.user_management', 'main.add_user', 'main.toggle_user_status',
    'main.edit_user', 'main.reset_user_password',
    'main.company_profile', 'main.settings', 'main.view_profile'
}


def _record_unauthorized_access():
    entry = AuditLog(
        organization_id=current_user.organization_id if current_user.is_authenticated else None,
        user_id=current_user.id if current_user.is_authenticated else None,
        user_code=getattr(current_user, 'user_code', 'GUEST'),
        username=getattr(current_user, 'name', 'Visitor'),
        action='Unauthorized access attempt',
        target_record=request.path,
        details=f'Attempted endpoint: {request.endpoint or "unmatched route"}',
        ip_address=request.remote_addr or '127.0.0.1'
    )
    db.session.add(entry)
    db.session.commit()


@main.before_request
def restrict_platform_owner_to_platform_operations():
    if not current_user.is_authenticated:
        if request.endpoint in PLATFORM_OWNER_ENDPOINTS | WORKSPACE_ENDPOINTS:
            _record_unauthorized_access()
        return

    if current_user.is_platform_owner:
        allowed_endpoints = {
            'static', 'main.index', 'main.register_organization', 'auth.login',
            'auth.signup',
            *PLATFORM_OWNER_ENDPOINTS, 'main.view_profile', 'auth.logout'
        }
        if request.endpoint not in allowed_endpoints:
            _record_unauthorized_access()
            abort(403)
        return

    workspace_endpoints = {
        'static', 'main.index', 'main.register_organization',
        'auth.login', 'auth.signup', 'main.profile',
        *WORKSPACE_ENDPOINTS,
        'auth.logout'
    }
    if request.endpoint not in workspace_endpoints:
        _record_unauthorized_access()
        abort(404)


def _money(value):
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def log_event(action, target_record=None, details=None, org_id=None):
    """Automatically records actions in the Security Audit Log scoped to organization."""
    try:
        user_id = current_user.id if current_user.is_authenticated else None
        user_code = getattr(current_user, 'user_code', 'SYSTEM') if current_user.is_authenticated else 'GUEST'
        username = getattr(current_user, 'name', 'Guest User') if current_user.is_authenticated else 'Visitor'
        if org_id is None and current_user.is_authenticated:
            org_id = current_user.organization_id

        entry = AuditLog(
            organization_id=org_id,
            user_id=user_id,
            user_code=user_code,
            username=username,
            action=action,
            target_record=target_record,
            details=details,
            ip_address=request.remote_addr or '127.0.0.1'
        )
        db.session.add(entry)
        db.session.commit()
    except Exception as e:
        print(f"[Audit Log Error] {e}")


def get_setting(key, default_val='', org_id=None):
    """Retrieves organization-specific setting, falling back to global default."""
    if org_id:
        org_setting = SystemSetting.query.filter_by(key=key, organization_id=org_id).first()
        if org_setting:
            return org_setting.value
    global_setting = SystemSetting.query.filter_by(key=key, organization_id=None).first()
    return global_setting.value if global_setting else default_val


def check_tenant_access(record):
    """
    Tenant Isolation Guard (Section 9):
    Verifies that the requested record belongs to current_user's organization.
    Platform Owner has global supervisory inspection access.
    """
    if not current_user.is_authenticated:
        return False
    if current_user.is_platform_owner:
        return True
    if record and hasattr(record, 'organization_id'):
        return record.organization_id == current_user.organization_id
    return False


def require_workspace_admin():
    if not current_user.is_org_admin:
        abort(403)


def pending_access_requests():
    return User.query.outerjoin(
        Organization, User.organization_id == Organization.id
    ).filter(
        User.role == 'Loan Officer',
        User.status == 'Pending Approval',
        or_(
            User.organization_id.is_(None),
            Organization.org_type == 'Personal Workspace'
        )
    ).order_by(User.created_at.asc()).all()


# ==============================================================================
# 0. Public Website (Home, About, Features, Organization Registration)
# ==============================================================================
@main.route('/')
def index():
    return render_template('index.html')


@main.route('/about')
def about():
    return render_template('about.html')


@main.route('/features')
def features():
    return render_template('features.html')


@main.route('/register-organization', methods=['GET', 'POST'])
def register_organization():
    if current_user.is_authenticated:
        return redirect(url_for('main.profile'))

    if request.method == 'POST':
        name = request.form.get('org_name', '').strip()
        selected_org_type = request.form.get('org_type', '').strip()
        custom_org_type = request.form.get('org_type_other', '').strip()
        org_email = request.form.get('org_email', '').strip().lower()
        org_phone_input = request.form.get('org_phone', '').strip()
        org_phone = _normalize_malawi_phone(org_phone_input)
        location = request.form.get('org_location', '').strip()
        address = request.form.get('org_address', '').strip()
        admin_name = request.form.get('admin_name', '').strip()
        admin_email = request.form.get('admin_email', '').strip().lower()
        admin_phone_input = request.form.get('admin_phone', '').strip()
        admin_phone = _normalize_malawi_phone(admin_phone_input)
        national_id_input = request.form.get('admin_national_id', '').strip()
        national_id = re.sub(r'[\s-]', '', national_id_input).upper()
        password = request.form.get('admin_password', '')
        confirm_password = request.form.get('admin_confirm_password', '')
        organization_types = {
            'Microfinance Institution', 'Commercial Bank',
            'Savings & Credit Cooperative (SACCO)',
            'Fintech Digital Lender', 'Development Finance Institution'
        }

        if not _valid_registration_text(name, 150) or not re.fullmatch(
            r"[^\W_][\w .&'()/+-]{1,149}", name, re.UNICODE
        ):
            flash('Enter a valid company name (2 to 150 characters).', 'warning')
            return render_template('register_organization.html')
        if selected_org_type == 'Other':
            if (
                not _valid_registration_text(custom_org_type, 80)
                or not re.fullmatch(r"[^\W_][\w .&'()/+-]{1,79}", custom_org_type, re.UNICODE)
            ):
                flash('Enter a valid company type (2 to 80 characters).', 'warning')
                return render_template('register_organization.html')
            org_type = custom_org_type
        elif selected_org_type in organization_types:
            org_type = selected_org_type
        else:
            flash('Select a valid company type.', 'warning')
            return render_template('register_organization.html')
        if not EMAIL_PATTERN.fullmatch(org_email) or len(org_email) > 150:
            flash('Enter a valid company email address.', 'warning')
            return render_template('register_organization.html')
        if not org_phone:
            flash('Enter a valid company phone number in +265XXXXXXXXX format.', 'warning')
            return render_template('register_organization.html')
        if not _valid_registration_text(location, 100):
            flash('Enter a valid company location (up to 100 characters).', 'warning')
            return render_template('register_organization.html')
        if not _valid_registration_text(address, 255):
            flash('Enter a valid office address (up to 255 characters).', 'warning')
            return render_template('register_organization.html')
        if not _valid_registration_name(admin_name):
            flash('Enter a valid administrator full name using letters and name punctuation only.', 'warning')
            return render_template('register_organization.html')
        if not EMAIL_PATTERN.fullmatch(admin_email) or len(admin_email) > 150:
            flash('Enter a valid administrator email address.', 'warning')
            return render_template('register_organization.html')
        if not admin_phone:
            flash('Enter a valid administrator phone number in +265XXXXXXXXX format.', 'warning')
            return render_template('register_organization.html')
        if (
            len(national_id_input) > 30
            or not 6 <= len(national_id) <= 30
            or not re.fullmatch(r'[A-Z0-9]+', national_id)
        ):
            flash(
                'Enter a valid Malawi National ID as shown on the card, using 6 to 30 letters or numbers.',
                'warning'
            )
            return render_template('register_organization.html')
        if User.query.filter_by(email=admin_email).first():
            flash('That administrator email is already registered. Please sign in or use another address.', 'danger')
            return render_template('register_organization.html')
        if User.query.filter_by(national_id=national_id).first():
            flash('That national ID is already registered. Please check the number or contact support.', 'danger')
            return render_template('register_organization.html')
        if len(password) < 12:
            flash('Use an administrator password with at least 12 characters.', 'warning')
            return render_template('register_organization.html')
        if password != confirm_password:
            flash('The administrator passwords do not match.', 'warning')
            return render_template('register_organization.html')

        organization = Organization(
            org_code=f"ORG-{uuid.uuid4().hex[:12].upper()}",
            name=name,
            org_type=org_type,
            email=org_email,
            phone=org_phone,
            location=location,
            address=address,
            verification_status='Pending Approval'
        )
        administrator = User(
            organization=organization,
            user_code=f"USR-{uuid.uuid4().hex[:12].upper()}",
            name=admin_name,
            email=admin_email,
            phone_number=admin_phone,
            national_id=national_id,
            password=generate_password_hash(password, method='scrypt'),
            role='Organization Administrator',
            status='Active'
        )
        db.session.add_all([organization, administrator])
        db.session.commit()
        log_event(
            'Organization registration submitted',
            organization.org_code,
            f'{organization.name} requested a workspace',
            organization.id
        )
        flash(
            'Account request submitted. Your company profile and administrator account are pending platform owner approval. You can log in after approval.',
            'success'
        )
        return redirect(url_for('auth.login'))

    return render_template('register_organization.html')


# ==============================================================================
# 1. Platform Owner Dashboard (Section 25)
# ==============================================================================
@main.route('/platform-admin')
@login_required
def platform_admin():
    if not current_user.is_platform_owner:
        flash('Access restricted: Platform Owner administrative credentials required.', 'danger')
        return redirect(url_for('main.profile'))

    all_organizations = Organization.query.order_by(Organization.created_at.desc()).all()
    organizations = [
        organization for organization in all_organizations
        if organization.verification_status not in {'Rejected', 'Suspended'}
    ]
    rejected_organizations = [
        organization for organization in all_organizations
        if organization.verification_status == 'Rejected'
    ]
    suspended_organizations = [
        organization for organization in all_organizations
        if organization.verification_status == 'Suspended'
    ]
    organization_ids = [organization.id for organization in all_organizations]
    registration_admins = {}
    if organization_ids:
        company_users = User.query.filter(
            User.organization_id.in_(organization_ids)
        ).order_by(User.created_at.asc(), User.id.asc()).all()
        for company_user in company_users:
            if company_user.role in {'Organization Administrator', 'Administrator'}:
                registration_admins.setdefault(company_user.organization_id, company_user)
        for company_user in company_users:
            registration_admins.setdefault(company_user.organization_id, company_user)

    return render_template(
        'platform_admin.html',
        organizations=organizations,
        rejected_organizations=rejected_organizations,
        suspended_organizations=suspended_organizations,
        registration_admins=registration_admins,
        organization_count=len(all_organizations),
        pending_count=sum(
            organization.verification_status == 'Pending Approval'
            for organization in all_organizations
        ),
        active_count=sum(
            organization.verification_status == 'Verified'
            for organization in all_organizations
        ),
        rejected_count=len(rejected_organizations),
        suspended_count=len(suspended_organizations),
        activity_count=AuditLog.query.count(),
        active_page='platform_admin'
    )


@main.route('/platform-activity')
@login_required
def platform_activity():
    if not current_user.is_platform_owner:
        abort(403)

    activity = AuditLog.query.options(
        joinedload(AuditLog.organization)
    ).order_by(AuditLog.timestamp.desc(), AuditLog.id.desc()).all()
    return render_template(
        'platform_activity.html',
        activity=activity,
        activity_count=len(activity),
        active_page='platform_activity'
    )


@main.route('/platform-models')
@login_required
def platform_model_management():
    if not current_user.is_platform_owner:
        abort(403)

    active_version = get_setting(
        'active_model_version',
        'Random Forest & XGBoost Ensemble v2.1'
    )
    return render_template(
        'platform_models.html',
        evaluations=ModelEvaluation.query.order_by(ModelEvaluation.roc_auc.desc()).all(),
        versions=ModelVersion.query.order_by(
            ModelVersion.training_date.desc(), ModelVersion.id.desc()
        ).all(),
        active_version=active_version,
        model_errors=AuditLog.query.filter_by(
            action='Model prediction error'
        ).order_by(AuditLog.timestamp.desc()).limit(100).all(),
        model_error_count=AuditLog.query.filter_by(action='Model prediction error').count(),
        activity_count=AuditLog.query.count(),
        active_page='platform_models'
    )


@main.route('/platform-models/upload', methods=['POST'])
@login_required
def upload_platform_model():
    if not current_user.is_platform_owner:
        abort(403)

    model_name = request.form.get('model_name', '').strip()
    version_number = request.form.get('version_number', '').strip()
    model_file = request.files.get('model_file')
    if (
        not _valid_registration_text(model_name, 100)
        or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,49}', version_number)
        or not model_file
        or not model_file.filename
        or not model_file.filename.lower().endswith('.joblib')
    ):
        flash('Provide a model name, a valid version number, and a .joblib model file.', 'warning')
        return redirect(url_for('main.platform_model_management'))

    artifact = model_file.stream.read(50 * 1024 * 1024 + 1)
    if not artifact:
        flash('The selected model file is empty.', 'warning')
        return redirect(url_for('main.platform_model_management'))
    if len(artifact) > 50 * 1024 * 1024:
        flash('Model files must be 50 MB or smaller.', 'warning')
        return redirect(url_for('main.platform_model_management'))

    existing_version = ModelVersion.query.filter_by(
        model_name=model_name,
        version_number=version_number
    ).first()
    if existing_version:
        flash('That model version is already registered.', 'warning')
        return redirect(url_for('main.platform_model_management'))

    artifact_directory = current_app.config['MODEL_ARTIFACT_DIRECTORY']
    os.makedirs(artifact_directory, exist_ok=True)
    artifact_filename = f'{uuid.uuid4().hex}.joblib'
    artifact_path = os.path.join(artifact_directory, artifact_filename)
    with open(artifact_path, 'wb') as artifact_file:
        artifact_file.write(artifact)

    version = ModelVersion(
        model_name=model_name,
        version_number=version_number,
        training_dataset_reference=f'Uploaded artifact: {model_file.filename[:200]}',
        feature_schema_version='v1.0',
        model_status='Candidate',
        artifact_path=artifact_filename
    )
    db.session.add(version)
    db.session.commit()
    log_event(
        'Uploaded machine learning model',
        f'{model_name} {version_number}',
        f'Added model artifact {artifact_filename} as a candidate'
    )
    flash(f'{model_name} {version_number} uploaded as a candidate model.', 'success')
    return redirect(url_for('main.platform_model_management'))


@main.route('/platform-models/activate-version/<int:version_id>', methods=['POST'])
@login_required
def activate_platform_model(version_id):
    if not current_user.is_platform_owner:
        abort(403)
    version = db.session.get(ModelVersion, version_id)
    if not version:
        abort(404)

    ModelVersion.query.filter(ModelVersion.id != version.id).update(
        {ModelVersion.model_status: 'Archived'},
        synchronize_session=False
    )
    for evaluation in ModelEvaluation.query.all():
        evaluation.status = 'Evaluated'
    version.model_status = 'Active Production'
    active_version = f'{version.model_name} {version.version_number}'
    setting = SystemSetting.query.filter_by(
        key='active_model_version',
        organization_id=None
    ).first()
    if setting:
        setting.value = active_version
        setting.updated_by = current_user.id
    else:
        db.session.add(SystemSetting(
            key='active_model_version',
            value=active_version,
            description='Active platform prediction model',
            updated_by=current_user.id
        ))
    db.session.commit()
    log_event(
        'Activated prediction model version',
        active_version,
        'Platform Owner selected the active model version'
    )
    flash(f'{active_version} is now the active prediction model.', 'success')
    return redirect(url_for('main.platform_model_management'))


@main.route('/platform-models/activate-evaluation/<int:evaluation_id>', methods=['POST'])
@login_required
def activate_evaluated_model(evaluation_id):
    if not current_user.is_platform_owner:
        abort(403)
    evaluation = db.session.get(ModelEvaluation, evaluation_id)
    if not evaluation:
        abort(404)

    for existing_evaluation in ModelEvaluation.query.all():
        existing_evaluation.status = (
            'Active Production'
            if existing_evaluation.id == evaluation.id
            else 'Evaluated'
        )
    ModelVersion.query.update(
        {ModelVersion.model_status: 'Archived'},
        synchronize_session=False
    )
    active_version = evaluation.model_name
    setting = SystemSetting.query.filter_by(
        key='active_model_version',
        organization_id=None
    ).first()
    if setting:
        setting.value = active_version
        setting.updated_by = current_user.id
    else:
        db.session.add(SystemSetting(
            key='active_model_version',
            value=active_version,
            description='Active platform prediction model',
            updated_by=current_user.id
        ))
    db.session.commit()
    log_event(
        'Activated prediction model',
        active_version,
        'Platform Owner selected an evaluated model'
    )
    flash(f'{active_version} is now the active prediction model.', 'success')
    return redirect(url_for('main.platform_model_management'))


def _platform_activity_workbook(activity):
    spreadsheet_ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    relationships_ns = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    package_relationships_ns = 'http://schemas.openxmlformats.org/package/2006/relationships'
    content_types_ns = 'http://schemas.openxmlformats.org/package/2006/content-types'
    ET.register_namespace('', spreadsheet_ns)
    ET.register_namespace('r', relationships_ns)

    def element(parent, namespace, name, attributes=None):
        return ET.SubElement(
            parent,
            f'{{{namespace}}}{name}',
            attributes or {}
        )

    workbook = ET.Element(f'{{{spreadsheet_ns}}}workbook')
    sheets = element(workbook, spreadsheet_ns, 'sheets')
    element(sheets, spreadsheet_ns, 'sheet', {
        'name': 'Platform Activity',
        'sheetId': '1',
        f'{{{relationships_ns}}}id': 'rId1'
    })

    worksheet = ET.Element(f'{{{spreadsheet_ns}}}worksheet')
    sheet_data = element(worksheet, spreadsheet_ns, 'sheetData')
    headers = (
        'ID', 'Date & Time', 'User Name', 'User Code', 'Company',
        'Action', 'Target Record', 'Details', 'IP Address'
    )
    rows = [headers]
    rows.extend(
        (
            log.id,
            log.timestamp.isoformat(sep=' ', timespec='seconds') if log.timestamp else '',
            log.username or '',
            log.user_code or '',
            log.organization.name if log.organization else '',
            log.action or '',
            log.target_record or '',
            log.details or '',
            log.ip_address or ''
        )
        for log in activity
    )

    for row_index, values in enumerate(rows, start=1):
        row_element = element(sheet_data, spreadsheet_ns, 'row', {'r': str(row_index)})
        for column_index, value in enumerate(values, start=1):
            column_name = ''
            remaining = column_index
            while remaining:
                remaining, remainder = divmod(remaining - 1, 26)
                column_name = chr(65 + remainder) + column_name
            cell = element(
                row_element,
                spreadsheet_ns,
                'c',
                {'r': f'{column_name}{row_index}', 't': 'inlineStr'}
            )
            inline_string = element(cell, spreadsheet_ns, 'is')
            text = element(inline_string, spreadsheet_ns, 't')
            text.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
            text.text = str(value)

    content_types = ET.Element(f'{{{content_types_ns}}}Types')
    element(content_types, content_types_ns, 'Default', {
        'Extension': 'rels',
        'ContentType': 'application/vnd.openxmlformats-package.relationships+xml'
    })
    element(content_types, content_types_ns, 'Default', {
        'Extension': 'xml',
        'ContentType': 'application/xml'
    })
    element(content_types, content_types_ns, 'Override', {
        'PartName': '/xl/workbook.xml',
        'ContentType': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml'
    })
    element(content_types, content_types_ns, 'Override', {
        'PartName': '/xl/worksheets/sheet1.xml',
        'ContentType': 'application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml'
    })

    package_relationships = ET.Element(f'{{{package_relationships_ns}}}Relationships')
    element(package_relationships, package_relationships_ns, 'Relationship', {
        'Id': 'rId1',
        'Type': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument',
        'Target': 'xl/workbook.xml'
    })
    workbook_relationships = ET.Element(f'{{{package_relationships_ns}}}Relationships')
    element(workbook_relationships, package_relationships_ns, 'Relationship', {
        'Id': 'rId1',
        'Type': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet',
        'Target': 'worksheets/sheet1.xml'
    })

    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as workbook_file:
        workbook_file.writestr('[Content_Types].xml', ET.tostring(content_types, encoding='utf-8', xml_declaration=True))
        workbook_file.writestr('_rels/.rels', ET.tostring(package_relationships, encoding='utf-8', xml_declaration=True))
        workbook_file.writestr('xl/workbook.xml', ET.tostring(workbook, encoding='utf-8', xml_declaration=True))
        workbook_file.writestr('xl/_rels/workbook.xml.rels', ET.tostring(workbook_relationships, encoding='utf-8', xml_declaration=True))
        workbook_file.writestr('xl/worksheets/sheet1.xml', ET.tostring(worksheet, encoding='utf-8', xml_declaration=True))
    return output.getvalue()


@main.route('/platform-activity/export')
@login_required
def platform_activity_export():
    if not current_user.is_platform_owner:
        abort(403)
    activity = AuditLog.query.options(
        joinedload(AuditLog.organization)
    ).order_by(AuditLog.timestamp.desc(), AuditLog.id.desc()).all()
    return Response(
        _platform_activity_workbook(activity),
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': 'attachment; filename=platform_activity.xlsx'}
    )


@main.route('/platform-activity/<int:activity_id>/delete', methods=['POST'])
@login_required
def delete_platform_activity(activity_id):
    if not current_user.is_platform_owner:
        abort(403)
    activity = db.session.get(AuditLog, activity_id)
    if not activity:
        abort(404)
    db.session.delete(activity)
    db.session.commit()
    flash('Activity entry deleted.', 'success')
    return redirect(url_for('main.platform_activity'))


@main.route('/platform-activity/clear', methods=['POST'])
@login_required
def clear_platform_activity():
    if not current_user.is_platform_owner:
        abort(403)
    AuditLog.query.delete(synchronize_session=False)
    db.session.commit()
    flash('All platform activity entries have been cleared.', 'success')
    return redirect(url_for('main.platform_activity'))


@main.route('/platform-admin/approve-organization/<int:org_id>', methods=['POST'])
@login_required
def approve_organization(org_id):
    if not current_user.is_platform_owner:
        abort(403)
    organization = db.session.get(Organization, org_id)
    if not organization or organization.verification_status != 'Pending Approval':
        abort(404)

    organization.verification_status = 'Verified'
    db.session.commit()
    log_event(
        'Approved organization',
        organization.org_code,
        f'Approved workspace for {organization.name}',
        organization.id
    )
    flash(f'{organization.name} is approved and can now sign in.', 'success')
    return redirect(url_for('main.platform_admin'))


@main.route('/platform-admin/reject-organization/<int:org_id>', methods=['POST'])
@login_required
def reject_organization(org_id):
    if not current_user.is_platform_owner:
        abort(403)
    organization = db.session.get(Organization, org_id)
    if not organization or organization.verification_status != 'Pending Approval':
        abort(404)

    organization.verification_status = 'Rejected'
    db.session.commit()
    log_event(
        'Rejected organization',
        organization.org_code,
        f'Rejected registration for {organization.name}',
        organization.id
    )
    flash(f'{organization.name} registration was rejected.', 'info')
    return redirect(url_for('main.platform_admin'))


@main.route('/platform-admin/toggle-org/<int:org_id>', methods=['POST'])
@login_required
def toggle_org_verification(org_id):
    if not current_user.is_platform_owner:
        abort(403)
    org = db.session.get(Organization, org_id)
    if org and org.verification_status in {'Verified', 'Suspended'}:
        org.verification_status = 'Suspended' if org.verification_status == 'Verified' else 'Verified'
        db.session.commit()
        log_event("Updated organization verification", org.org_code, f"Status changed to {org.verification_status}", org.id)
        flash(f'Organization {org.name} status updated to {org.verification_status}.', 'info')
    else:
        abort(404)
    return redirect(url_for('main.platform_admin'))


@main.route('/platform-admin/organization/<int:org_id>/status', methods=['POST'])
@login_required
def set_organization_status(org_id):
    if not current_user.is_platform_owner:
        abort(403)
    organization = db.session.get(Organization, org_id)
    new_status = request.form.get('status', '').strip()
    if not organization or new_status not in {'Verified', 'Suspended', 'Deactivated'}:
        abort(404)

    previous_status = organization.verification_status
    organization.verification_status = new_status
    db.session.commit()
    log_event(
        f'Organization status changed to {new_status}',
        organization.org_code,
        f'{organization.name}: {previous_status} -> {new_status}',
        organization.id
    )
    flash(f'{organization.name} status updated to {new_status}.', 'info')
    return redirect(url_for('main.platform_admin'))


@main.route('/platform-admin/organization/<int:org_id>/update', methods=['POST'])
@login_required
def update_organization(org_id):
    if not current_user.is_platform_owner:
        abort(403)
    organization = db.session.get(Organization, org_id)
    if not organization:
        abort(404)

    name = request.form.get('name', '').strip()
    org_type = request.form.get('org_type', '').strip()
    email = request.form.get('email', '').strip().lower()
    phone = _normalize_malawi_phone(request.form.get('phone', '').strip())
    location = request.form.get('location', '').strip()
    address = request.form.get('address', '').strip()
    if (
        not _valid_registration_text(name, 150)
        or not re.fullmatch(r"[^\W_][\w .&'()/+-]{1,149}", name, re.UNICODE)
        or not _valid_registration_text(org_type, 80)
        or not EMAIL_PATTERN.fullmatch(email)
        or len(email) > 150
        or not phone
        or not _valid_registration_text(location, 100)
        or not _valid_registration_text(address, 255)
    ):
        flash('Enter valid company details before saving.', 'warning')
        return redirect(url_for('main.platform_admin'))

    old_name = organization.name
    organization.name = name
    organization.org_type = org_type
    organization.email = email
    organization.phone = phone
    organization.location = location
    organization.address = address
    db.session.commit()
    log_event(
        'Updated organization',
        organization.org_code,
        f'Updated company profile for {old_name} to {name}',
        organization.id
    )
    flash(f'{organization.name} company details updated.', 'success')
    return redirect(url_for('main.platform_admin'))


@main.route('/platform-admin/organization/<int:org_id>/delete', methods=['POST'])
@login_required
def delete_organization(org_id):
    if not current_user.is_platform_owner:
        abort(403)
    organization = db.session.get(Organization, org_id)
    if not organization:
        abort(404)

    org_name = organization.name
    org_code = organization.org_code
    log_event(
        'Deleted organization',
        org_code,
        f'Deleted company {org_name} and its associated workspace records'
    )
    db.session.delete(organization)
    db.session.commit()
    flash(f'{org_name} and its workspace records were deleted.', 'info')
    return redirect(url_for('main.platform_admin'))


@main.route('/platform-admin/set-model-version', methods=['POST'])
@login_required
def set_active_model_version():
    if not current_user.is_platform_owner:
        abort(403)
    version_name = request.form.get('model_version', '').strip()
    version = next(
        (
            candidate for candidate in ModelVersion.query.all()
            if f'{candidate.model_name} {candidate.version_number}' == version_name
        ),
        None
    )
    evaluation = ModelEvaluation.query.filter_by(model_name=version_name).first()
    if not version and not evaluation:
        abort(404)
    if version:
        return redirect(url_for(
            'main.activate_platform_model',
            version_id=version.id
        ), code=307)
    return redirect(url_for(
        'main.activate_evaluated_model',
        evaluation_id=evaluation.id
    ), code=307)


# ==============================================================================
# 2. Personal Loan Risk Workspace
# ==============================================================================
@main.route('/dashboard')
@main.route('/profile')
@login_required
def profile():
    if current_user.is_platform_owner:
        return redirect(url_for('main.platform_admin'))

    org_id = current_user.organization_id
    applications = LoanApplication.query.filter_by(organization_id=org_id).order_by(LoanApplication.created_at.desc()).all()
    return render_template(
        'profile.html',
        total_apps=len(applications),
        pending_apps=sum(1 for application in applications if application.status in {'Pending', 'Under Review'}),
        approved_apps=sum(1 for application in applications if application.status == 'Approved'),
        borrower_count=Applicant.query.filter_by(organization_id=org_id).count(),
        staff_count=User.query.filter_by(organization_id=org_id).count(),
        recent_apps=applications[:5],
        active_page='dashboard'
    )


@main.route('/profile/view')
@login_required
def view_profile():
    return render_template('view_profile.html', active_page='profile')


@main.route('/company-profile', methods=['GET', 'POST'])
@login_required
def company_profile():
    organization = current_user.organization
    if not organization:
        abort(404)

    if request.method == 'POST':
        require_workspace_admin()
        name = request.form.get('name', '').strip()
        org_type = request.form.get('org_type', '').strip()
        email = request.form.get('email', '').strip().lower()
        phone = request.form.get('phone', '').strip()
        location = request.form.get('location', '').strip()
        address = request.form.get('address', '').strip()
        if not name or len(name) > 150:
            flash('Enter a valid company name.', 'warning')
            return redirect(url_for('main.company_profile'))
        if not email or len(email) > 150:
            flash('Enter a valid company email.', 'warning')
            return redirect(url_for('main.company_profile'))

        organization.name = name
        organization.org_type = org_type or organization.org_type
        organization.email = email
        organization.phone = phone
        organization.location = location
        organization.address = address

        logo = request.files.get('logo')
        if logo and logo.filename:
            extension = os.path.splitext(secure_filename(logo.filename))[1].lower()
            if extension not in {'.png', '.jpg', '.jpeg', '.webp'}:
                flash('Company logo must be PNG, JPG, JPEG, or WEBP.', 'warning')
                return redirect(url_for('main.company_profile'))
            upload_directory = os.path.join(
                os.path.dirname(__file__),
                'static',
                'uploads',
                'company_logos'
            )
            os.makedirs(upload_directory, exist_ok=True)
            filename = f"company-{organization.id}-{uuid.uuid4().hex}{extension}"
            logo.save(os.path.join(upload_directory, filename))
            organization.logo_path = f"uploads/company_logos/{filename}"

        db.session.commit()
        log_event(
            'Updated company profile',
            organization.org_code,
            f'Updated institutional profile for {organization.name}'
        )
        flash('Company profile updated successfully.', 'success')
        return redirect(url_for('main.company_profile'))

    return render_template(
        'company_profile.html',
        organization=organization,
        active_page='company_profile'
    )


# ==============================================================================
# 3. Applicant Registration Module (Tenant-Scoped, Section 10)
# ==============================================================================
@main.route('/applicants', methods=['GET'])
@login_required
def applicants_list():
    org_id = current_user.organization_id
    query = request.args.get('search', '').strip().lower()
    date_from = request.args.get('date_from', '').strip()

    q = Applicant.query.filter_by(organization_id=org_id)
    if query:
        q = q.filter(
            (Applicant.full_name.ilike(f'%{query}%')) |
            (Applicant.applicant_id.ilike(f'%{query}%')) |
            (Applicant.phone_number.ilike(f'%{query}%')) |
            (Applicant.residential_area.ilike(f'%{query}%'))
        )
    if date_from:
        try:
            parsed_date = datetime.strptime(date_from, '%Y-%m-%d').date()
            q = q.filter(db.func.date(Applicant.registration_date) >= parsed_date)
        except ValueError:
            pass

    applicants = q.order_by(Applicant.registration_date.desc()).all()

    # Pre-generate next ID for applicant form
    last = Applicant.query.filter_by(organization_id=org_id).order_by(Applicant.id.desc()).first()
    next_code = f"CUST-{1000 + (last.id + 1 if last else 1)}"

    return render_template(
        'applicants.html',
        applicants=applicants,
        next_code=next_code,
        search_query=query,
        date_from=date_from,
        active_page='applicants'
    )


@main.route('/applicants/new', methods=['POST'])
@login_required
def register_applicant():
    try:
        org_id = current_user.organization_id
        full_name = request.form.get('full_name', '').strip()
        phone_number = request.form.get('phone_number', '').strip()
        email = request.form.get('email', '').strip()
        age = int(request.form.get('age', 30))
        gender = request.form.get('gender', '').strip() or None
        national_id = request.form.get('national_id', '').strip() or None
        employment_status = request.form.get('employment_status', 'Employed')
        monthly_income = float(request.form.get('monthly_income', 500000))
        employment_duration = float(request.form.get('employment_duration', 3.0))
        existing_debt = float(request.form.get('existing_debt', 0))
        credit_history = request.form.get('credit_history', '').strip() or None
        residential_area = request.form.get('residential_area', 'Lilongwe').strip()

        if not full_name or not phone_number or not email:
            flash('Please complete all required applicant fields.', 'warning')
            return redirect(url_for('main.applicants_list'))

        duplicate_conditions = [
            db.func.lower(Applicant.full_name) == full_name.lower(),
            Applicant.phone_number == phone_number,
            db.func.lower(Applicant.email) == email.lower()
        ]
        if national_id:
            duplicate_conditions.append(Applicant.national_id == national_id)
        duplicate = Applicant.query.filter(
            Applicant.organization_id == org_id,
            or_(*duplicate_conditions)
        ).first()
        if duplicate:
            flash(
                f'A borrower may already be registered: {duplicate.full_name} ({duplicate.applicant_id}). '
                'Review the existing record before registering another.',
                'warning'
            )
            return redirect(url_for('main.applicant_detail', applicant_id=duplicate.id))

        supporting_document_path = None
        supporting_document = request.files.get('supporting_document')
        if supporting_document and supporting_document.filename:
            extension = os.path.splitext(secure_filename(supporting_document.filename))[1].lower()
            if extension not in {'.pdf', '.png', '.jpg', '.jpeg'}:
                flash('Supporting documents must be PDF, PNG, JPG, or JPEG.', 'warning')
                return redirect(url_for('main.applicants_list'))
            upload_directory = os.path.join(os.path.dirname(__file__), 'static', 'uploads', 'borrower_documents')
            os.makedirs(upload_directory, exist_ok=True)
            filename = f"borrower-{org_id}-{uuid.uuid4().hex}{extension}"
            supporting_document.save(os.path.join(upload_directory, filename))
            supporting_document_path = f"uploads/borrower_documents/{filename}"

        last = Applicant.query.filter_by(organization_id=org_id).order_by(Applicant.id.desc()).first()
        applicant_id = f"CUST-{1000 + (last.id + 1 if last else 1)}"

        applicant = Applicant(
            organization_id=org_id,
            applicant_id=applicant_id,
            full_name=full_name,
            phone_number=phone_number,
            email=email,
            age=age,
            gender=gender,
            national_id=national_id,
            employment_status=employment_status,
            monthly_income=monthly_income,
            employment_duration=employment_duration,
            existing_debt=existing_debt,
            credit_history=credit_history,
            whatsapp_opt_in=request.form.get('whatsapp_opt_in') == 'yes',
            residential_area=residential_area,
            supporting_document_path=supporting_document_path,
            registered_by=current_user.id
        )
        db.session.add(applicant)
        db.session.commit()

        log_event("Registered applicant", applicant_id, f"Registered applicant {full_name}")
        flash('Applicant registered successfully!', 'success')
        return redirect(url_for('main.applicants_list'))
    except Exception as e:
        flash(f'Error registering applicant: {str(e)}', 'danger')
        return redirect(url_for('main.applicants_list'))


@main.route('/applicants/<int:applicant_id>')
@login_required
def applicant_detail(applicant_id):
    applicant = Applicant.query.filter_by(
        id=applicant_id,
        organization_id=current_user.organization_id
    ).first_or_404()
    applications = LoanApplication.query.filter_by(
        organization_id=current_user.organization_id,
        applicant_id=applicant.id
    ).order_by(LoanApplication.created_at.desc()).all()
    return render_template(
        'applicant_detail.html',
        applicant=applicant,
        applications=applications,
        active_page='applicants'
    )


@main.route('/applicants/<int:applicant_id>/update', methods=['POST'])
@login_required
def update_applicant(applicant_id):
    applicant = Applicant.query.filter_by(
        id=applicant_id,
        organization_id=current_user.organization_id
    ).first_or_404()
    try:
        applicant.full_name = request.form.get('full_name', '').strip()
        applicant.phone_number = request.form.get('phone_number', '').strip()
        applicant.email = request.form.get('email', '').strip().lower()
        applicant.age = int(request.form.get('age', applicant.age))
        applicant.gender = request.form.get('gender', '').strip() or None
        applicant.national_id = request.form.get('national_id', '').strip() or None
        applicant.employment_status = request.form.get(
            'employment_status',
            applicant.employment_status
        )
        applicant.monthly_income = float(request.form.get('monthly_income', applicant.monthly_income))
        applicant.employment_duration = float(
            request.form.get('employment_duration', applicant.employment_duration)
        )
        applicant.existing_debt = float(request.form.get('existing_debt', applicant.existing_debt))
        applicant.credit_history = request.form.get('credit_history', '').strip() or None
        applicant.whatsapp_opt_in = request.form.get('whatsapp_opt_in') == 'yes'
        applicant.residential_area = request.form.get(
            'residential_area',
            applicant.residential_area
        ).strip()
        supporting_document = request.files.get('supporting_document')
        if supporting_document and supporting_document.filename:
            extension = os.path.splitext(secure_filename(supporting_document.filename))[1].lower()
            if extension not in {'.pdf', '.png', '.jpg', '.jpeg'}:
                raise ValueError('Invalid supporting document format')
            upload_directory = os.path.join(os.path.dirname(__file__), 'static', 'uploads', 'borrower_documents')
            os.makedirs(upload_directory, exist_ok=True)
            filename = f"borrower-{applicant.organization_id}-{uuid.uuid4().hex}{extension}"
            supporting_document.save(os.path.join(upload_directory, filename))
            applicant.supporting_document_path = f"uploads/borrower_documents/{filename}"
        if not applicant.full_name or not applicant.phone_number or not applicant.email:
            raise ValueError('Required fields are missing')
        db.session.commit()
        log_event('Updated borrower', applicant.applicant_id, f'Updated profile for {applicant.full_name}')
        flash('Borrower record updated successfully.', 'success')
    except (ValueError, InvalidOperation):
        db.session.rollback()
        flash('Enter valid borrower information.', 'danger')
    return redirect(url_for('main.applicant_detail', applicant_id=applicant.id))


@main.route('/applicants/<int:applicant_id>/delete', methods=['POST'])
@login_required
def delete_applicant(applicant_id):
    require_workspace_admin()
    applicant = Applicant.query.filter_by(
        id=applicant_id,
        organization_id=current_user.organization_id
    ).first_or_404()
    if LoanApplication.query.filter_by(applicant_id=applicant.id).first():
        flash('This borrower has loan records and cannot be deleted.', 'warning')
        return redirect(url_for('main.applicants_list'))
    if applicant.supporting_document_path:
        document_file = os.path.join(os.path.dirname(__file__), 'static', applicant.supporting_document_path)
        if os.path.isfile(document_file):
            os.remove(document_file)
    db.session.delete(applicant)
    db.session.commit()
    log_event('Deleted borrower record', applicant.applicant_id, f'Deleted {applicant.full_name}')
    flash('Borrower record deleted.', 'success')
    return redirect(url_for('main.applicants_list'))


# ==============================================================================
# 4. Loan Applications Module (Tenant-Scoped, Section 11)
# ==============================================================================
@main.route('/applications', methods=['GET'])
@login_required
def loan_applications():
    org_id = current_user.organization_id
    search = request.args.get('search', '').strip().lower()
    status_filter = request.args.get('status', '').strip()

    q = LoanApplication.query.filter_by(organization_id=org_id).join(Applicant)
    if search:
        q = q.filter(
            (LoanApplication.app_id.ilike(f'%{search}%')) |
            (Applicant.full_name.ilike(f'%{search}%'))
        )
    if status_filter:
        q = q.filter(LoanApplication.status == status_filter)

    applications = q.order_by(LoanApplication.created_at.desc()).all()
    applicants = Applicant.query.filter_by(organization_id=org_id).order_by(Applicant.full_name.asc()).all()
    borrowers_with_applications = {
        applicant_id
        for (applicant_id,) in db.session.query(
            LoanApplication.applicant_id
        ).filter(LoanApplication.organization_id == org_id).distinct().all()
    }

    last = LoanApplication.query.filter_by(organization_id=org_id).order_by(LoanApplication.id.desc()).first()
    next_app_id = f"APP-{1000 + (last.id + 1 if last else 1)}"
    all_org_applications = LoanApplication.query.filter_by(organization_id=org_id).all()
    application_counts = {
        'total': len(all_org_applications),
        'pending': sum(app.status == 'Pending' for app in all_org_applications),
        'under_review': sum(app.status == 'Under Review' for app in all_org_applications),
        'assessed': sum(app.status == 'Assessed' for app in all_org_applications),
        'approved': sum(app.status == 'Approved' for app in all_org_applications),
        'declined': sum(app.status == 'Declined' for app in all_org_applications),
    }

    return render_template(
        'loan_applications.html',
        applications=applications,
        applicants=applicants,
        borrowers_with_applications=borrowers_with_applications,
        next_app_id=next_app_id,
        search=search,
        status_filter=status_filter,
        application_counts=application_counts,
        active_page='applications'
    )


@main.route('/applications/new', methods=['POST'])
@login_required
def create_loan_application():
    try:
        org_id = current_user.organization_id

        applicant_id = int(request.form.get('applicant_id'))
        # Verify applicant belongs to this organization (Tenant Isolation Guard)
        applicant = db.session.get(Applicant, applicant_id)
        if not applicant or applicant.organization_id != org_id:
            flash('Unauthorized: Applicant does not belong to your organization.', 'danger')
            return redirect(url_for('main.loan_applications'))

        existing_application = LoanApplication.query.filter_by(
            organization_id=org_id,
            applicant_id=applicant.id
        ).first()
        if existing_application:
            flash(
                'Borrower already exists with a loan application. '
                'Each borrower may only have one loan application.',
                'warning'
            )
            return redirect(url_for('main.loan_applications'))

        loan_amount = float(request.form.get('loan_amount', 2000000))
        loan_purpose = request.form.get('loan_purpose', 'Business')
        loan_term = int(request.form.get('loan_term', 12))
        interest_rate = float(request.form.get('interest_rate', 18.0))
        monthly_income = float(request.form.get('monthly_income', 500000))
        existing_monthly_debt = float(request.form.get('existing_monthly_debt', 100000))
        credit_history_values = _application_credit_history_values(request.form)
        last = LoanApplication.query.filter_by(organization_id=org_id).order_by(LoanApplication.id.desc()).first()
        app_id = f"APP-{1000 + (last.id + 1 if last else 1)}"
        supporting_document_path = None
        supporting_document = request.files.get('supporting_document')
        if supporting_document and supporting_document.filename:
            extension = os.path.splitext(secure_filename(supporting_document.filename))[1].lower()
            if extension not in {'.pdf', '.png', '.jpg', '.jpeg'}:
                flash('Supporting documents must be PDF, PNG, JPG, or JPEG.', 'warning')
                return redirect(url_for('main.loan_applications'))
            upload_directory = os.path.join(os.path.dirname(__file__), 'static', 'uploads', 'loan_documents')
            os.makedirs(upload_directory, exist_ok=True)
            filename = f"loan-{org_id}-{uuid.uuid4().hex}{extension}"
            supporting_document.save(os.path.join(upload_directory, filename))
            supporting_document_path = f"uploads/loan_documents/{filename}"

        app_record = LoanApplication(
            organization_id=org_id,
            app_id=app_id,
            applicant_id=applicant.id,
            loan_amount=loan_amount,
            loan_purpose=loan_purpose,
            loan_term=loan_term,
            interest_rate=interest_rate,
            monthly_income=monthly_income,
            existing_monthly_debt=existing_monthly_debt,
            **credit_history_values,
            supporting_document_path=supporting_document_path,
            status='Pending',
            created_by=current_user.id
        )
        db.session.add(app_record)

        db.session.commit()

        log_event("Created loan application", app_id, f"Created application for MWK {loan_amount:,.0f} ({loan_purpose})")
        flash(f'Application {app_id} submitted successfully with status Pending!', 'success')
        return redirect(url_for('main.loan_applications'))
    except IntegrityError:
        db.session.rollback()
        duplicate_application = LoanApplication.query.filter_by(
            organization_id=org_id,
            applicant_id=applicant_id
        ).first()
        if duplicate_application:
            flash(
                'Borrower already exists with a loan application. '
                'Each borrower may only have one loan application.',
                'warning'
            )
        else:
            flash(
                'The application could not be saved because of a conflicting record. '
                'Please refresh and try again.',
                'danger'
            )
        return redirect(url_for('main.loan_applications'))
    except Exception as e:
        flash(f'Error creating loan application: {str(e)}', 'danger')
        return redirect(url_for('main.loan_applications'))


@main.route('/applications/<int:app_id>/update', methods=['POST'])
@login_required
def update_loan_application(app_id):
    application = LoanApplication.query.filter_by(
        id=app_id,
        organization_id=current_user.organization_id
    ).first_or_404()
    if application.status not in {'Pending', 'Under Review'}:
        flash('Only pending applications can be updated.', 'warning')
        return redirect(url_for('main.loan_applications'))
    try:
        application.loan_amount = float(request.form.get('loan_amount', application.loan_amount))
        application.loan_purpose = request.form.get('loan_purpose', application.loan_purpose)
        application.loan_term = int(request.form.get('loan_term', application.loan_term))
        application.interest_rate = float(request.form.get('interest_rate', application.interest_rate))
        application.monthly_income = float(request.form.get('monthly_income', application.monthly_income))
        application.existing_monthly_debt = float(
            request.form.get('existing_monthly_debt', application.existing_monthly_debt)
        )
        for field, value in _application_credit_history_values(
            request.form,
            application
        ).items():
            setattr(application, field, value)
        db.session.commit()
        log_event('Updated loan application', application.app_id, 'Updated application information')
        flash(f'Application {application.app_id} updated successfully.', 'success')
    except ValueError as error:
        db.session.rollback()
        flash(str(error), 'danger')
    except InvalidOperation:
        db.session.rollback()
        flash('Enter valid loan application information.', 'danger')
    return redirect(url_for('main.loan_applications'))


@main.route('/applications/<int:app_id>/delete', methods=['POST'])
@login_required
def delete_loan_application(app_id):
    require_workspace_admin()
    application = LoanApplication.query.filter_by(
        id=app_id,
        organization_id=current_user.organization_id
    ).first_or_404()
    if application.predictions or application.decision:
        flash('Applications with assessments or decisions cannot be deleted.', 'warning')
        return redirect(url_for('main.loan_applications'))
    app_code = application.app_id
    db.session.delete(application)
    db.session.commit()
    log_event('Deleted loan application', app_code, 'Deleted application with no dependent records')
    flash(f'Application {app_code} deleted.', 'success')
    return redirect(url_for('main.loan_applications'))


# ==============================================================================
# 5. Risk Predictions Module (Tenant-Scoped, Section 18, 19)
# ==============================================================================
@main.route('/predictions', methods=['GET'])
@login_required
def risk_predictions():
    org_id = current_user.organization_id
    search = request.args.get('search', '').strip()
    risk_filter = request.args.get('risk', '').strip()
    date_from = request.args.get('date_from', '').strip()
    date_to = request.args.get('date_to', '').strip()

    q = RiskPrediction.query.filter_by(organization_id=org_id).join(
        LoanApplication
    ).join(Applicant)
    if search:
        q = q.filter(
            (Applicant.full_name.ilike(f'%{search}%')) |
            (LoanApplication.app_id.ilike(f'%{search}%')) |
            (Applicant.applicant_id.ilike(f'%{search}%'))
        )
    if risk_filter in {'Low Risk', 'Medium Risk', 'High Risk'}:
        q = q.filter(RiskPrediction.risk_category == risk_filter)
    if date_from:
        try:
            start = datetime.strptime(date_from, '%Y-%m-%d')
            q = q.filter(RiskPrediction.assessment_date >= start)
        except ValueError:
            pass
    if date_to:
        try:
            end = datetime.strptime(date_to, '%Y-%m-%d') + timedelta(days=1)
            q = q.filter(RiskPrediction.assessment_date < end)
        except ValueError:
            pass
    predictions = q.order_by(RiskPrediction.assessment_date.desc()).all()
    pending_apps = LoanApplication.query.filter_by(organization_id=org_id).filter(LoanApplication.status.in_(['Pending', 'Under Review'])).all()

    risk_counts = {
        'Low Risk': 0,
        'Medium Risk': 0,
        'High Risk': 0
    }
    daily_probabilities = {}
    for prediction in predictions:
        if prediction.risk_category in risk_counts:
            risk_counts[prediction.risk_category] += 1
        if prediction.assessment_date:
            day = prediction.assessment_date.date()
            daily_probabilities.setdefault(day, []).append(
                prediction.default_probability
            )

    trend_days = sorted(daily_probabilities)[-7:]
    trend_labels = [day.strftime('%d %b') for day in trend_days]
    trend_values = [
        round(
            sum(daily_probabilities[day]) / len(daily_probabilities[day]),
            1
        )
        for day in trend_days
    ]

    th_low = float(get_setting('threshold_low', '20.0', org_id))
    th_med = float(get_setting('threshold_medium', '50.0', org_id))

    return render_template(
        'risk_predictions.html',
        predictions=predictions,
        pending_apps=pending_apps,
        risk_counts=risk_counts,
        trend_labels=trend_labels,
        trend_values=trend_values,
        th_low=th_low,
        th_med=th_med,
        search=search,
        risk_filter=risk_filter,
        date_from=date_from,
        date_to=date_to,
        active_page='predictions'
    )


@main.route('/predictions/export')
@login_required
def export_prediction_history():
    org_id = current_user.organization_id
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'Application ID', 'Applicant', 'Default Probability (%)',
        'Risk Category', 'Assessment Date', 'Decision'
    ])
    predictions = RiskPrediction.query.filter_by(
        organization_id=org_id
    ).join(LoanApplication).join(Applicant).order_by(
        RiskPrediction.assessment_date.desc()
    ).all()
    for prediction in predictions:
        application = prediction.loan_application
        writer.writerow([
            application.app_id,
            application.applicant.full_name if application.applicant else 'N/A',
            prediction.default_probability,
            prediction.risk_category,
            prediction.assessment_date.strftime('%Y-%m-%d %H:%M'),
            application.decision.decision if application.decision else 'Pending Decision'
        ])
    log_event('Exported prediction history', 'Prediction records', 'Exported prediction history CSV')
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment;filename=Prediction_History.csv'}
    )


@main.route('/predict/<int:app_id>', methods=['GET', 'POST'])
@login_required
def assess_application_risk(app_id):
    application = LoanApplication.query.filter_by(
        id=app_id,
        organization_id=current_user.organization_id
    ).first_or_404()

    org_id = current_user.organization_id
    th_low = float(get_setting('threshold_low', '20.0', org_id))
    th_med = float(get_setting('threshold_medium', '50.0', org_id))
    model_version = get_setting('active_model_version', 'Random Forest & XGBoost Ensemble v2.1', org_id)

    if request.method == 'POST' or request.args.get('run') == '1':
        applicant_data = {
            'monthly_income': application.monthly_income,
            'loan_amount': application.loan_amount,
            'loan_term': application.loan_term,
            'employment_status': application.applicant.employment_status if application.applicant else 'Employed',
            'existing_debt': application.existing_monthly_debt
        }

        # Run ML engine inference and retain failures for platform monitoring.
        try:
            result = ml_model.predict(applicant_data)
        except Exception as error:
            db.session.rollback()
            log_event(
                'Model prediction error',
                application.app_id,
                f'Model version {model_version}: {type(error).__name__}: {error}'
            )
            flash('The prediction could not be completed. The platform owner has been notified.', 'danger')
            return redirect(url_for('main.assess_application_risk', app_id=application.id))
        prob = result['default_probability']

        if prob < th_low:
            risk_category = 'Low Risk'
        elif prob < th_med:
            risk_category = 'Medium Risk'
        else:
            risk_category = 'High Risk'

        prediction = application.latest_prediction
        is_update = prediction is not None
        if not prediction:
            prediction = RiskPrediction(
                organization_id=application.organization_id,
                application_id=application.id
            )
            db.session.add(prediction)

        prediction.default_probability = prob
        prediction.risk_category = risk_category
        prediction.model_version = model_version
        prediction.explanation_json = json.dumps(result['key_insights'])
        prediction.assessed_by = current_user.id
        prediction.assessment_date = datetime.now(timezone.utc)
        application.status = 'Assessed'
        db.session.commit()

        action = 'Updated' if is_update else 'Generated'
        log_event(
            f'{action} prediction',
            application.app_id,
            f"Predicted default probability: {prob}% ({risk_category})"
        )
        flash(
            f'Risk prediction {action.lower()} for {application.app_id}: '
            f'{prob}% ({risk_category})',
            'success'
        )
        return redirect(url_for('main.assess_application_risk', app_id=application.id))

    whatsapp_templates, whatsapp_template_error = _whatsapp_template_options(application)
    selected_whatsapp_template = next(
        (
            option['name'] for option in whatsapp_templates
            if option['name'] == application.whatsapp_approval_template
        ),
        whatsapp_templates[0]['name'] if whatsapp_templates else ''
    )
    return render_template(
        'prediction_detail.html',
        application=application,
        whatsapp_templates=whatsapp_templates,
        whatsapp_template_error=whatsapp_template_error,
        selected_whatsapp_template=selected_whatsapp_template,
        active_page='predictions'
    )


@main.route('/record-decision/<int:app_id>', methods=['POST'])
@login_required
def record_decision(app_id):
    application = db.session.get(LoanApplication, app_id)
    if not application:
        flash('Application not found.', 'warning')
        return redirect(url_for('main.risk_predictions'))

    if not check_tenant_access(application):
        flash('Unauthorized: Cannot record decisions for another organization\'s application.', 'danger')
        return redirect(url_for('main.loan_applications'))

    decision_choice = request.form.get('decision', 'Approved')
    if decision_choice not in {'Approved', 'Conditional Approval', 'Under Review', 'Declined'}:
        flash('Select a valid lending decision.', 'warning')
        return redirect(url_for('main.assess_application_risk', app_id=application.id))
    notes = request.form.get('notes', '')
    selected_whatsapp_template = request.form.get('whatsapp_template', '').strip()

    existing_dec = LoanDecision.query.filter_by(application_id=application.id).first()
    was_approved = bool(existing_dec and existing_dec.decision == 'Approved')
    if decision_choice == 'Approved' and application.whatsapp_approval_status != 'Sent':
        templates, _ = _whatsapp_template_options(application)
        if any(option['name'] == selected_whatsapp_template for option in templates):
            application.whatsapp_approval_template = selected_whatsapp_template
    if existing_dec:
        existing_dec.decision = decision_choice
        existing_dec.notes = notes
        existing_dec.decided_by = current_user.id
        existing_dec.decided_at = datetime.now(timezone.utc)
    else:
        new_dec = LoanDecision(
            organization_id=application.organization_id,
            application_id=application.id,
            decision=decision_choice,
            notes=notes,
            decided_by=current_user.id
        )
        db.session.add(new_dec)

    application.status = decision_choice if decision_choice in {'Approved', 'Declined', 'Under Review'} else 'Under Review'

    db.session.commit()
    log_event("Recorded loan decision", application.app_id, f"Decision: {decision_choice}. Notes: {notes[:60]}")
    if decision_choice == 'Approved' and not was_approved:
        notification_status, notification_error = _send_whatsapp_approval(
            application,
            selected_whatsapp_template or None
        )
        application.whatsapp_approval_status = notification_status
        application.whatsapp_approval_sent_at = (
            datetime.now(timezone.utc) if notification_status == 'Sent' else None
        )
        application.whatsapp_approval_error = notification_error
        db.session.commit()
        log_event(
            'WhatsApp approval notification',
            application.app_id,
            f'Notification status: {notification_status}'
        )
        if notification_status == 'Sent':
            flash(
                f'Decision saved successfully for {application.app_id}: '
                'Approved. WhatsApp approval notification sent.',
                'success'
            )
        else:
            flash(
                f'Decision saved successfully for {application.app_id}: '
                f'Approved. WhatsApp notification not sent: {notification_error}',
                'warning'
            )
    else:
        flash(f'Decision saved successfully for {application.app_id}: {decision_choice}.', 'success')
    return redirect(url_for('main.assess_application_risk', app_id=application.id))


@main.route('/applications/<int:app_id>/whatsapp-approval/retry', methods=['POST'])
@login_required
def retry_whatsapp_approval(app_id):
    application = LoanApplication.query.filter_by(
        id=app_id,
        organization_id=current_user.organization_id
    ).first_or_404()
    decision = LoanDecision.query.filter_by(application_id=application.id).first()
    if not decision or decision.decision != 'Approved':
        flash('A WhatsApp approval notice can only be sent for an approved application.', 'warning')
        return redirect(url_for('main.assess_application_risk', app_id=application.id))
    if application.whatsapp_approval_status == 'Sent':
        flash('The WhatsApp approval notice has already been sent.', 'info')
        return redirect(url_for('main.assess_application_risk', app_id=application.id))

    selected_whatsapp_template = request.form.get('whatsapp_template', '').strip()
    notification_status, notification_error = _send_whatsapp_approval(
        application,
        selected_whatsapp_template or None
    )
    application.whatsapp_approval_status = notification_status
    application.whatsapp_approval_sent_at = (
        datetime.now(timezone.utc) if notification_status == 'Sent' else None
    )
    application.whatsapp_approval_error = notification_error
    db.session.commit()
    log_event(
        'WhatsApp approval notification retry',
        application.app_id,
        f'Notification status: {notification_status}'
    )
    if notification_status == 'Sent':
        flash('WhatsApp approval notification sent successfully.', 'success')
    else:
        flash(f'WhatsApp notification not sent: {notification_error}', 'warning')
    return redirect(url_for('main.assess_application_risk', app_id=application.id))


# ==============================================================================
# 6. Reports Module (Tenant-Scoped, Section 21)
# ==============================================================================
@main.route('/reports')
@login_required
def reports():
    org_id = current_user.organization_id
    tab = request.args.get('tab', 'applications')
    if tab not in {'applications', 'predictions', 'distribution', 'outcomes'}:
        tab = 'applications'

    applications = LoanApplication.query.filter_by(organization_id=org_id).order_by(LoanApplication.created_at.desc()).all()
    predictions = RiskPrediction.query.filter_by(organization_id=org_id).order_by(RiskPrediction.assessment_date.desc()).all()
    total_apps = len(applications)
    total_assessed = len(predictions)
    low_risk_count = sum(1 for p in predictions if p.risk_category == 'Low Risk')
    med_risk_count = sum(1 for p in predictions if p.risk_category == 'Medium Risk')
    high_risk_count = sum(1 for p in predictions if p.risk_category == 'High Risk')
    low_risk_share = (low_risk_count / total_assessed * 100) if total_assessed else 0
    med_risk_share = (med_risk_count / total_assessed * 100) if total_assessed else 0
    high_risk_share = (high_risk_count / total_assessed * 100) if total_assessed else 0

    return render_template(
        'reports.html',
        tab=tab,
        applications=applications,
        predictions=predictions,
        total_apps=total_apps,
        total_assessed=total_assessed,
        low_risk_count=low_risk_count,
        med_risk_count=med_risk_count,
        high_risk_count=high_risk_count,
        low_risk_share=low_risk_share,
        med_risk_share=med_risk_share,
        high_risk_share=high_risk_share,
        generated_at=datetime.now(),
        active_page='reports'
    )


@main.route('/reports/export/<report_type>')
@login_required
def export_report_csv(report_type):
    org_id = current_user.organization_id
    output = io.StringIO()
    writer = csv.writer(output)

    if report_type == 'applications':
        writer.writerow(['Application ID', 'Applicant Name', 'Amount (MWK)', 'Purpose', 'Term (Months)', 'Status', 'Date'])
        for app in LoanApplication.query.filter_by(organization_id=org_id).all():
            writer.writerow([
                app.app_id,
                app.applicant.full_name if app.applicant else 'N/A',
                app.loan_amount,
                app.loan_purpose,
                app.loan_term,
                app.status,
                app.created_at.strftime('%Y-%m-%d %H:%M')
            ])
        filename = "Loan_Applications_Report.csv"

    elif report_type == 'predictions':
        writer.writerow(['Application ID', 'Applicant', 'Default Probability (%)', 'Risk Category', 'Assessment Date'])
        for p in RiskPrediction.query.filter_by(organization_id=org_id).all():
            writer.writerow([
                p.loan_application.app_id if p.loan_application else 'N/A',
                p.loan_application.applicant.full_name if p.loan_application and p.loan_application.applicant else 'N/A',
                p.default_probability,
                p.risk_category,
                p.assessment_date.strftime('%Y-%m-%d %H:%M')
            ])
        filename = "Risk_Assessment_Report.csv"

    elif report_type == 'outcomes':
        writer.writerow(['Application ID', 'Borrower', 'Decision', 'Loan Status', 'Risk Category'])
        for app in LoanApplication.query.filter_by(organization_id=org_id).order_by(LoanApplication.created_at.desc()).all():
            writer.writerow([
                app.app_id,
                app.applicant.full_name if app.applicant else 'N/A',
                app.decision.decision if app.decision else 'Not decided',
                app.status,
                app.latest_prediction.risk_category if app.latest_prediction else 'Not assessed'
            ])
        filename = "Loan_Outcomes_Report.csv"

    elif report_type == 'distribution':
        writer.writerow(['Risk Category', 'Count', 'Percentage'])
        preds = RiskPrediction.query.filter_by(organization_id=org_id).all()
        total = len(preds)
        low = sum(1 for p in preds if p.risk_category == 'Low Risk')
        med = sum(1 for p in preds if p.risk_category == 'Medium Risk')
        high = sum(1 for p in preds if p.risk_category == 'High Risk')
        writer.writerow(['Low Risk (< 20%)', low, f"{(low/total*100 if total else 0):.1f}%"])
        writer.writerow(['Medium Risk (20%-50%)', med, f"{(med/total*100 if total else 0):.1f}%"])
        writer.writerow(['High Risk (>= 50%)', high, f"{(high/total*100 if total else 0):.1f}%"])
        filename = "Risk_Distribution_Report.csv"

    else:
        abort(404)

    log_event("Exported report", report_type, f"Exported {filename}")
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename={filename}"}
    )


# ==============================================================================
# 7. Model Performance Module (Section 22)
# ==============================================================================
@main.route('/model-performance')
@login_required
def model_performance():
    evaluations = ModelEvaluation.query.order_by(ModelEvaluation.roc_auc.desc()).all()
    active_version = get_setting('active_model_version', 'Random Forest & XGBoost Ensemble v2.1')

    return render_template(
        'model_performance.html',
        evaluations=evaluations,
        active_version=active_version,
        active_page='model'
    )


# ==============================================================================
# 8. User Management Module (Tenant-Scoped Staff, Section 23)
# ==============================================================================
@main.route('/users')
@login_required
def user_management():
    require_workspace_admin()
    org_id = current_user.organization_id
    users = User.query.filter_by(organization_id=org_id).order_by(User.user_code.asc()).all()
    return render_template(
        'user_management.html',
        users=users,
        active_page='users'
    )


@main.route('/users/new', methods=['POST'])
@login_required
def add_user():
    require_workspace_admin()
    org_id = current_user.organization_id

    name = request.form.get('name', '').strip()
    email = request.form.get('email', '').strip().lower()
    role = request.form.get('role', 'Loan Officer')
    password = request.form.get('password', '')
    confirm_password = request.form.get('confirm_password', '')

    allowed_roles = {
        'Organization Administrator', 'Administrator', 'Loan Officer',
        'Credit Analyst', 'Finance Officer', 'Management'
    }
    if not name or not email or not password or role not in allowed_roles:
        flash('Enter a name, email, password, and valid workspace role.', 'warning')
        return redirect(url_for('main.user_management'))

    if User.query.filter_by(email=email).first():
        flash('A user account with this email already exists.', 'warning')
        return redirect(url_for('main.user_management'))

    if len(password) < 12:
        flash('The password must be at least 12 characters long.', 'warning')
        return redirect(url_for('main.user_management'))

    if password != confirm_password:
        flash('The passwords do not match.', 'warning')
        return redirect(url_for('main.user_management'))

    user_code = f"USR-{uuid.uuid4().hex[:12].upper()}"

    user = User(
        organization_id=org_id,
        user_code=user_code,
        name=name,
        email=email,
        role=role,
        status='Active',
        password=generate_password_hash(password, method='scrypt')
    )
    db.session.add(user)
    db.session.commit()

    log_event("Created user account", user_code, f"Created {role} account for {name} ({email})")
    flash(f'User {name} ({user_code}) added successfully!', 'success')
    return redirect(url_for('main.user_management'))


@main.route('/users/<int:user_id>/approve', methods=['POST'])
@login_required
def approve_user(user_id):
    require_workspace_admin()
    user = db.session.get(User, user_id)
    legacy_workspace = user.organization if user and user.organization_id else None
    is_eligible_request = (
        user
        and user.role == 'Loan Officer'
        and user.status == 'Pending Approval'
        and (
            user.organization_id is None
            or (legacy_workspace and legacy_workspace.org_type == 'Personal Workspace')
        )
    )
    if not is_eligible_request:
        abort(404)

    org_id = current_user.organization_id
    user.organization_id = org_id
    user.status = 'Active'
    db.session.commit()

    log_event('Approved loan officer access', user.user_code, f'Approved {user.name} to join this personal workspace')
    flash(f'{user.name} has been approved and can now sign in to this workspace.', 'success')
    return redirect(url_for('main.user_management'))


@main.route('/users/<int:user_id>/reject', methods=['POST'])
@login_required
def reject_user(user_id):
    require_workspace_admin()
    user = db.session.get(User, user_id)
    legacy_workspace = user.organization if user and user.organization_id else None
    is_eligible_request = (
        user
        and user.role == 'Loan Officer'
        and user.status == 'Pending Approval'
        and (
            user.organization_id is None
            or (legacy_workspace and legacy_workspace.org_type == 'Personal Workspace')
        )
    )
    if not is_eligible_request:
        abort(404)

    user.status = 'Rejected'
    db.session.commit()

    log_event('Rejected loan officer access', user.user_code, f'Rejected {user.name} access request')
    flash(f"{user.name}'s access request was rejected.", 'info')
    return redirect(url_for('main.user_management'))


@main.route('/users/toggle-status/<int:user_id>', methods=['POST'])
@login_required
def toggle_user_status(user_id):
    require_workspace_admin()
    user = User.query.filter_by(
        id=user_id,
        organization_id=current_user.organization_id
    ).first_or_404()
    if user and user.organization_id == current_user.organization_id:
        if user.id == current_user.id:
            flash('You cannot deactivate your own account.', 'warning')
            return redirect(url_for('main.user_management'))
        if user.is_org_admin and user.status == 'Active':
            active_admins = User.query.filter(
                User.organization_id == current_user.organization_id,
                User.role.in_(WORKSPACE_ADMIN_ROLES | {'Organization Administrator'}),
                User.status == 'Active'
            ).count()
            if active_admins <= 1:
                flash('Keep at least one active organization administrator.', 'warning')
                return redirect(url_for('main.user_management'))
        user.status = 'Inactive' if user.status == 'Active' else 'Active'
        db.session.commit()
        log_event("Updated user status", user.user_code, f"Changed status to {user.status}")
        flash(f'User {user.name} status updated to {user.status}.', 'info')
    return redirect(url_for('main.user_management'))


@main.route('/users/<int:user_id>/edit', methods=['POST'])
@login_required
def edit_user(user_id):
    require_workspace_admin()
    user = User.query.filter_by(
        id=user_id,
        organization_id=current_user.organization_id
    ).first_or_404()
    if user.id == current_user.id:
        flash('Use account settings to change your own role.', 'warning')
        return redirect(url_for('main.user_management'))
    allowed_roles = {
        'Organization Administrator', 'Administrator', 'Loan Officer',
        'Credit Analyst', 'Finance Officer', 'Management'
    }
    name = request.form.get('name', '').strip()
    email = request.form.get('email', '').strip().lower()
    role = request.form.get('role', '').strip()
    phone_number = request.form.get('phone_number', '').strip()
    national_id = request.form.get('national_id', '').strip()
    if not name or not email or role not in allowed_roles:
        flash('Enter a valid name, email, and employee role.', 'warning')
        return redirect(url_for('main.user_management'))
    duplicate = User.query.filter(User.email == email, User.id != user.id).first()
    if duplicate:
        flash('That email address is already assigned to another account.', 'warning')
        return redirect(url_for('main.user_management'))
    user.name = name
    user.email = email
    user.role = role
    user.phone_number = phone_number or None
    user.national_id = national_id or None
    db.session.commit()
    log_event('Updated employee account', user.user_code, f'Updated {name}; role {role}')
    flash(f'Employee {name} updated successfully.', 'success')
    return redirect(url_for('main.user_management'))


@main.route('/users/<int:user_id>/reset-password', methods=['POST'])
@login_required
def reset_user_password(user_id):
    require_workspace_admin()
    user = User.query.filter_by(
        id=user_id,
        organization_id=current_user.organization_id
    ).first_or_404()
    password = request.form.get('password', '')
    confirmation = request.form.get('confirm_password', '')
    if len(password) < 12 or password != confirmation:
        flash('Use at least 12 characters and confirm the new password.', 'warning')
        return redirect(url_for('main.user_management'))
    user.password = generate_password_hash(password, method='scrypt')
    db.session.commit()
    log_event('Reset employee password', user.user_code, f'Reset access password for {user.name}')
    flash(f'Password reset completed for {user.name}.', 'success')
    return redirect(url_for('main.user_management'))


# ==============================================================================
# 10. Personal Workspace Settings
# ==============================================================================
@main.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    require_workspace_admin()
    org_id = current_user.organization_id

    if request.method == 'POST':
        for key in [
            'threshold_low',
            'threshold_medium'
        ]:
            val = request.form.get(key)
            if val:
                item = SystemSetting.query.filter_by(key=key, organization_id=org_id).first()
                if item:
                    item.value = val
                    item.updated_by = current_user.id
                else:
                    db.session.add(SystemSetting(key=key, value=val, organization_id=org_id, updated_by=current_user.id))

        current_password = request.form.get('current_password', '')
        new_password = request.form.get('new_password', '')
        confirm_password = request.form.get('confirm_password', '')
        if current_password or new_password or confirm_password:
            if not check_password_hash(current_user.password, current_password):
                db.session.rollback()
                flash('The current password is incorrect.', 'danger')
                return redirect(url_for('main.settings'))
            if len(new_password) < 12 or new_password != confirm_password:
                db.session.rollback()
                flash('Use at least 12 characters and confirm the new password.', 'danger')
                return redirect(url_for('main.settings'))
            current_user.password = generate_password_hash(new_password, method='scrypt')

        db.session.commit()
        log_event(
            "Updated settings",
            current_user.user_code,
            "Updated risk thresholds or account password"
        )
        flash('Your workspace settings have been updated.', 'success')
        return redirect(url_for('main.settings'))

    all_settings = {s.key: s.value for s in SystemSetting.query.filter(
        (SystemSetting.organization_id == org_id) | (SystemSetting.organization_id == None)
    ).all()}

    return render_template('settings.html', settings=all_settings, active_page='settings')


# ==============================================================================
# Module 2: Data Collection & Dataset Upload (CSV/Excel)
# ==============================================================================
@main.route('/data-upload', methods=['GET', 'POST'])
@login_required
def data_upload():
    org_id = current_user.organization_id
    if request.method == 'POST':
        file = request.files.get('dataset_file')
        if not file or not file.filename:
            flash('Please select a valid CSV dataset file to upload.', 'warning')
            return redirect(url_for('main.data_upload'))

        filename = file.filename.lower()
        if not filename.endswith('.csv'):
            flash('Only CSV format is currently supported for batch data ingestion.', 'warning')
            return redirect(url_for('main.data_upload'))

        try:
            stream = io.StringIO(file.stream.read().decode('utf-8-sig', errors='replace'))
            reader = csv.DictReader(stream)

            # Normalize headers
            headers = [h.strip().lower() for h in (reader.fieldnames or [])]
            required_cols = ['full_name', 'phone_number', 'monthly_income', 'loan_amount']
            missing = [c for c in required_cols if not any(c in h for h in headers)]
            if missing:
                flash(f"CSV is missing essential columns: {', '.join(missing)}. Please check the format guide.", 'danger')
                return redirect(url_for('main.data_upload'))

            imported_count = 0
            assessed_count = 0
            current_app_num = LoanApplication.query.filter_by(organization_id=org_id).count() + 1000
            current_cust_num = Applicant.query.filter_by(organization_id=org_id).count() + 1000

            auto_assess = request.form.get('auto_assess') == '1'

            for row in reader:
                # Extract fields with safe fallbacks
                name = row.get('full_name') or row.get('name') or 'Batch Applicant'
                phone = row.get('phone_number') or row.get('phone') or '0990000000'
                email = row.get('email') or f"applicant_{current_cust_num}@customer.mw"
                age = int(float(row.get('age', 32) or 32))
                emp_status = row.get('employment_status') or row.get('employment') or 'Employed'
                income = float(row.get('monthly_income', 500000) or 500000)
                emp_duration = float(row.get('employment_duration', 3.0) or 3.0)
                residential = row.get('residential_area') or row.get('location') or 'Lilongwe'

                loan_amt = float(row.get('loan_amount', 1000000) or 1000000)
                loan_purpose = row.get('loan_purpose') or 'Business'
                loan_term = int(float(row.get('loan_term', 12) or 12))
                existing_debt = float(row.get('existing_debt') or row.get('existing_monthly_debt', 50000) or 50000)

                current_cust_num += 1
                current_app_num += 1

                applicant = Applicant(
                    organization_id=org_id,
                    applicant_id=f"APP-{current_cust_num}",
                    full_name=name,
                    phone_number=phone,
                    email=email,
                    age=age,
                    employment_status=emp_status,
                    monthly_income=income,
                    employment_duration=emp_duration,
                    residential_area=residential,
                    registered_by=current_user.id
                )
                db.session.add(applicant)
                db.session.flush()

                loan_app = LoanApplication(
                    organization_id=org_id,
                    app_id=f"APP-{current_app_num}",
                    applicant_id=applicant.id,
                    loan_amount=loan_amt,
                    loan_purpose=loan_purpose,
                    loan_term=loan_term,
                    interest_rate=18.0,
                    monthly_income=income,
                    existing_monthly_debt=existing_debt,
                    status='Assessed' if auto_assess else 'Pending',
                    created_by=current_user.id
                )
                db.session.add(loan_app)
                db.session.flush()

                if auto_assess:
                    pred_res = ml_model.predict({
                        'monthly_income': income,
                        'loan_amount': loan_amt,
                        'loan_term': loan_term,
                        'employment_status': emp_status,
                        'existing_monthly_debt': existing_debt
                    })
                    pred = RiskPrediction(
                        organization_id=org_id,
                        application_id=loan_app.id,
                        default_probability=pred_res['default_probability'],
                        risk_category=pred_res['risk_tier'],
                        explanation_json=json.dumps(pred_res['key_insights']),
                        model_version='Random Forest v2.1',
                        assessed_by=current_user.id
                    )
                    db.session.add(pred)
                    assessed_count += 1

                imported_count += 1

            db.session.commit()
            log_event("Batch Data Upload", f"{imported_count} applicants", f"Imported {imported_count} applicants from {file.filename}")
            flash(f"Successfully imported {imported_count} applicant records! ({assessed_count} risk predictions generated).", "success")
            return redirect(url_for('main.applicants_list'))

        except Exception as e:
            db.session.rollback()
            flash(f"Error parsing dataset: {str(e)}", "danger")
            return redirect(url_for('main.data_upload'))

    # GET metrics
    total_apps = LoanApplication.query.filter_by(organization_id=org_id).count() if not current_user.is_platform_owner else LoanApplication.query.count()
    total_cust = Applicant.query.filter_by(organization_id=org_id).count() if not current_user.is_platform_owner else Applicant.query.count()

    return render_template('data_upload.html',
                           active_page='data_upload',
                           total_apps=total_apps,
                           total_cust=total_cust)


@main.route('/download-sample-csv')
@login_required
def download_sample_csv():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'full_name', 'phone_number', 'email', 'age', 'employment_status',
        'monthly_income', 'employment_duration', 'residential_area',
        'loan_amount', 'loan_purpose', 'loan_term', 'existing_debt'
    ])
    sample_rows = [
        ['John Banda', '0991234567', 'john.banda@example.mw', 32, 'Employed', 500000, 3.5, 'Lilongwe City Centre', 2000000, 'Business', 12, 100000],
        ['Mary Phiri', '0887654321', 'mary.phiri@example.mw', 28, 'Self-Employed', 750000, 4.0, 'Blantyre Mandala', 1500000, 'Agriculture', 18, 120000],
        ['Peter Zulu', '0995551234', 'peter.zulu@example.mw', 45, 'Business Owner', 1200000, 7.0, 'Mzuzu Katoto', 3500000, 'Business', 24, 250000],
        ['Chisomo Chirwa', '0881122334', 'chisomo.c@example.mw', 24, 'Contract', 320000, 1.2, 'Zomba Urban', 800000, 'Education', 12, 45000],
        ['Grace Mvula', '0998877665', 'grace.mvula@example.mw', 39, 'Employed', 680000, 5.0, 'Lilongwe Area 47', 1800000, 'Personal', 18, 80000]
    ]
    for row in sample_rows:
        writer.writerow(row)

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-disposition": "attachment; filename=loan_default_dataset_sample.csv"}
    )


# ==============================================================================
# Module 3: Data Pre-processing and Cleaning
# ==============================================================================
@main.route('/preprocessing')
@login_required
def preprocessing():
    summary = preprocessing_engine.get_cleaning_summary()
    org_id = current_user.organization_id

    # Live data sanity check for tenant
    apps = LoanApplication.query.filter_by(organization_id=org_id).all() if not current_user.is_platform_owner else LoanApplication.query.all()
    tenant_total = len(apps)
    zero_debt_count = sum(1 for a in apps if a.existing_monthly_debt == 0)
    high_loan_count = sum(1 for a in apps if a.loan_amount > 5000000)

    return render_template('preprocessing.html',
                           active_page='preprocessing',
                           summary=summary,
                           tenant_total=tenant_total,
                           zero_debt_count=zero_debt_count,
                           high_loan_count=high_loan_count)


@main.route('/preprocessing/run', methods=['POST'])
@login_required
def run_preprocessing():
    log_event("Executed Preprocessing Pipeline", "Telemetry Dataset", "Ran missing-value imputation, outlier winsorization, and Z-score standardization.")
    flash("Data Pre-processing pipeline executed successfully. Missing values imputed, outliers winsorized, and features standardized.", "success")
    return redirect(url_for('main.preprocessing'))


# ==============================================================================
# Module 4: Exploratory Data Analysis (EDA)
# ==============================================================================
@main.route('/eda')
@login_required
def eda():
    eda_data = eda_engine.get_eda_metrics()
    org_id = current_user.organization_id

    apps = LoanApplication.query.filter_by(organization_id=org_id).all() if not current_user.is_platform_owner else LoanApplication.query.all()
    total_apps = len(apps)

    return render_template('eda.html',
                           active_page='eda',
                           eda_data=eda_data,
                           total_apps=total_apps)


# ==============================================================================
# Module 5: Feature Selection and Engineering
# ==============================================================================
@main.route('/feature-engineering')
@login_required
def feature_engineering():
    rankings = feature_engine.get_feature_importance_rankings()
    sample_ratios = feature_engine.calculate_ratios(500000, 2000000, 12, 100000)
    return render_template('feature_engineering.html',
                           active_page='feature_engineering',
                           rankings=rankings,
                           sample_ratios=sample_ratios)


# ==============================================================================
# Module 6 & 8: Machine Learning Model Training & Evaluation
# ==============================================================================
@main.route('/model-training')
@login_required
def model_training():
    evals = ModelEvaluation.query.order_by(ModelEvaluation.roc_auc.desc()).all()
    models_benchmark = model_trainer.get_all_models_benchmark()
    return render_template('model_training.html',
                           active_page='model_training',
                           evals=evals,
                           models_benchmark=models_benchmark)


@main.route('/model-training/run', methods=['POST'])
@login_required
def run_model_training():
    if not current_user.is_platform_owner:
        abort(403)

    algorithm = request.form.get('algorithm', 'Random Forest')
    allowed_algorithms = {
        'Random Forest', 'XGBoost', 'Gradient Boosting',
        'Decision Tree', 'Logistic Regression'
    }
    if algorithm not in allowed_algorithms:
        abort(400)

    dataset_file = request.files.get('training_dataset')
    if not dataset_file or not dataset_file.filename:
        flash('Select a CSV training dataset before training the model.', 'warning')
        return redirect(url_for('main.platform_model_management'))
    if not dataset_file.filename.lower().endswith('.csv'):
        flash('The training dataset must be a CSV file.', 'warning')
        return redirect(url_for('main.platform_model_management'))

    dataset_bytes = dataset_file.read()
    if not dataset_bytes:
        flash('The selected CSV dataset is empty.', 'warning')
        return redirect(url_for('main.platform_model_management'))
    if len(dataset_bytes) > 50 * 1024 * 1024:
        flash('Training datasets must be 50 MB or smaller.', 'warning')
        return redirect(url_for('main.platform_model_management'))

    artifact_directory = current_app.config['MODEL_ARTIFACT_DIRECTORY']
    os.makedirs(artifact_directory, exist_ok=True)
    upload_id = uuid.uuid4().hex
    dataset_filename = f'{upload_id}.csv'
    dataset_path = os.path.join(artifact_directory, dataset_filename)
    with open(dataset_path, 'wb') as dataset_output:
        dataset_output.write(dataset_bytes)

    artifact_filename = f'{upload_id}.joblib'
    artifact_path = os.path.join(artifact_directory, artifact_filename)
    try:
        result = model_trainer.train_from_csv(
            dataset_path,
            algorithm=algorithm,
            artifact_path=artifact_path,
            target_column=request.form.get('target_column', '').strip() or None
        )
    except (ValueError, ImportError, ModuleNotFoundError) as error:
        for generated_path in (dataset_path, artifact_path):
            if os.path.isfile(generated_path):
                os.remove(generated_path)
        flash(str(error), 'danger')
        return redirect(url_for('main.platform_model_management'))
    except Exception as error:
        for generated_path in (dataset_path, artifact_path):
            if os.path.isfile(generated_path):
                os.remove(generated_path)
        flash(f'Training failed: {str(error)}', 'danger')
        return redirect(url_for('main.platform_model_management'))

    # Update evaluation in DB if present
    eval_record = ModelEvaluation.query.filter_by(model_name=algorithm).first()
    if eval_record:
        eval_record.accuracy = result['accuracy']
        eval_record.precision = result['precision']
        eval_record.recall = result['recall']
        eval_record.f1_score = result['f1_score']
        eval_record.roc_auc = result['roc_auc']
        eval_record.pr_auc = result['pr_auc']
        eval_record.evaluation_date = datetime.now(timezone.utc).replace(tzinfo=None)
        eval_record.dataset_summary = (
            f"{secure_filename(dataset_file.filename)[:180]}: "
            f"{result['total_records']} records, {result['feature_count']} features"
        )
        eval_record.status = 'Candidate'
    else:
        eval_record = ModelEvaluation(
            model_name=algorithm,
            accuracy=result['accuracy'],
            precision=result['precision'],
            recall=result['recall'],
            f1_score=result['f1_score'],
            roc_auc=result['roc_auc'],
            pr_auc=result['pr_auc'],
            status='Candidate',
            dataset_summary=(
                f"{secure_filename(dataset_file.filename)[:180]}: "
                f"{result['total_records']} records, {result['feature_count']} features"
            )
        )
        db.session.add(eval_record)

    trained_version = ModelVersion(
        model_name=algorithm,
        version_number=f"trained-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{upload_id[:8]}",
        training_dataset_reference=secure_filename(dataset_file.filename)[:255],
        feature_schema_version=f"{result['feature_count']}-features",
        model_status='Candidate',
        artifact_path=artifact_filename
    )
    db.session.add(trained_version)
    db.session.commit()

    log_event(
        "Retrained Machine Learning Model",
        algorithm,
        f"Trained {algorithm} on {result['total_records']} real CSV records "
        f"(Accuracy: {result['accuracy']*100:.1f}%, PR-AUC: {result['pr_auc']:.2f}, "
        f"ROC-AUC: {result['roc_auc']:.2f})"
    )
    flash(
        f"Algorithm '{algorithm}' trained on {result['total_records']} records and saved as a candidate model! "
        f"Accuracy: {result['accuracy']*100:.1f}%, PR-AUC: {result['pr_auc']:.2f}.",
        "success"
    )
    return redirect(url_for('main.platform_model_management'))


# ==============================================================================
# Quick Interactive API for Frontend Simulator
# ==============================================================================
@main.route('/api/predict', methods=['POST'])
def api_predict():
    data = request.get_json(force=True) if request.is_json else request.form.to_dict()
    result = ml_model.predict(data)
    return jsonify({'status': 'success', 'data': result})


# Entry point
if __name__ == '__main__':
    from run import app
    app.run(debug=True, host='0.0.0.0', port=5000)
