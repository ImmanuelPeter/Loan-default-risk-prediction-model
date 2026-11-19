import json
from datetime import datetime, timezone, timedelta
from flask_login import UserMixin
from sqlalchemy import UniqueConstraint
from database import db

def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ==============================================================================
# 1. Multi-Tenant Organization Models
# ==============================================================================
class Organization(db.Model):
    __tablename__ = 'organizations'

    id = db.Column(db.Integer, primary_key=True)
    org_code = db.Column(db.String(30), unique=True, nullable=False) # e.g. ORG-1001
    name = db.Column(db.String(150), nullable=False)
    org_type = db.Column(db.String(80), nullable=False, default='Microfinance Institution') # Bank, MFI, SACCO, Fintech
    email = db.Column(db.String(150), nullable=False)
    phone = db.Column(db.String(50), nullable=False)
    location = db.Column(db.String(100), nullable=True)
    address = db.Column(db.String(255), nullable=True)
    logo_path = db.Column(db.String(255), nullable=True)
    verification_status = db.Column(db.String(30), nullable=False, default='Pending Approval')
    created_at = db.Column(db.DateTime, default=utc_now)

    # Relationships
    users = db.relationship('User', backref='organization', lazy=True, cascade='all, delete-orphan')
    applicants = db.relationship('Applicant', backref='organization', lazy=True, cascade='all, delete-orphan')
    applications = db.relationship('LoanApplication', backref='organization', lazy=True, cascade='all, delete-orphan')
    audit_logs = db.relationship('AuditLog', backref='organization', lazy=True, cascade='all, delete-orphan')

    def __repr__(self):
        return f'<Organization {self.org_code} - {self.name}>'


# ==============================================================================
# 2. User & Authentication Models
# ==============================================================================
class User(UserMixin, db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey('organizations.id'), nullable=True) # Null for Platform Owner
    user_code = db.Column(db.String(20), unique=True, nullable=False) # e.g. USR-001
    name = db.Column(db.String(150), nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False)
    phone_number = db.Column(db.String(20), nullable=True)
    national_id = db.Column(db.String(30), unique=True, nullable=True)
    password = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(50), nullable=False, default='Loan Officer') # Platform Owner, Organization Administrator, Loan Officer, Manager, Auditor
    status = db.Column(db.String(20), nullable=False, default='Active') # Active, Inactive, Suspended
    created_at = db.Column(db.DateTime, default=utc_now)
    last_login = db.Column(db.DateTime, nullable=True)

    # Relationships
    registered_applicants = db.relationship('Applicant', backref='registered_by_user', lazy=True)
    loan_applications = db.relationship('LoanApplication', backref='created_by_user', lazy=True)
    predictions = db.relationship('RiskPrediction', backref='assessed_by_user', lazy=True)
    decisions = db.relationship('LoanDecision', backref='decided_by_user', lazy=True)

    @property
    def is_platform_owner(self):
        return self.role == 'Platform Owner'

    @property
    def is_org_admin(self):
        return self.role in ['Organization Administrator', 'Administrator', 'Platform Owner']

    def __repr__(self):
        return f'<User {self.user_code} - {self.name} ({self.role})>'


# ==============================================================================
# 3. Core Lending Models (Tenant-Scoped)
# ==============================================================================
class Applicant(db.Model):
    __tablename__ = 'applicants'

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey('organizations.id'), nullable=True) # Tenant scope
    applicant_id = db.Column(db.String(30), nullable=False) # e.g. CUST-1001 (Unique within org)
    full_name = db.Column(db.String(150), nullable=False)
    phone_number = db.Column(db.String(50), nullable=False)
    email = db.Column(db.String(150), nullable=False)
    age = db.Column(db.Integer, nullable=False)
    gender = db.Column(db.String(30), nullable=True)
    national_id = db.Column(db.String(30), nullable=True)
    employment_status = db.Column(db.String(50), nullable=False) # Employed, Self-Employed, Business Owner, Unemployed, Contract
    monthly_income = db.Column(db.Float, nullable=False) # in MWK
    employment_duration = db.Column(db.Float, nullable=False) # in years
    existing_debt = db.Column(db.Float, nullable=False, default=0.0)
    credit_history = db.Column(db.Text, nullable=True)
    whatsapp_opt_in = db.Column(db.Boolean, nullable=False, default=False)
    residential_area = db.Column(db.String(150), nullable=False)
    supporting_document_path = db.Column(db.String(255), nullable=True)
    registration_date = db.Column(db.DateTime, default=utc_now)
    registered_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    # Relationships
    applications = db.relationship('LoanApplication', backref='applicant', lazy=True, cascade='all, delete-orphan')

    def to_dict(self):
        return {
            'id': self.id,
            'applicant_id': self.applicant_id,
            'full_name': self.full_name,
            'phone_number': self.phone_number,
            'email': self.email,
            'age': self.age,
            'gender': self.gender,
            'national_id': self.national_id,
            'employment_status': self.employment_status,
            'monthly_income': self.monthly_income,
            'employment_duration': self.employment_duration,
            'existing_debt': self.existing_debt,
            'credit_history': self.credit_history,
            'residential_area': self.residential_area,
            'registration_date': self.registration_date.strftime('%d %b %Y, %H:%M')
        }


class LoanApplication(db.Model):
    __tablename__ = 'loan_applications'
    __table_args__ = (
        UniqueConstraint(
            'organization_id',
            'applicant_id',
            name='uq_loan_applications_one_per_borrower'
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey('organizations.id'), nullable=True) # Tenant scope
    app_id = db.Column(db.String(30), nullable=False) # e.g. APP-1001
    applicant_id = db.Column(db.Integer, db.ForeignKey('applicants.id'), nullable=False)
    loan_amount = db.Column(db.Float, nullable=False) # in MWK
    loan_purpose = db.Column(db.String(100), nullable=False) # Business, Agriculture, Education, Personal, etc.
    loan_term = db.Column(db.Integer, nullable=False) # months (e.g. 12)
    interest_rate = db.Column(db.Float, nullable=False, default=18.0) # % APR
    monthly_income = db.Column(db.Float, nullable=False) # in MWK
    existing_monthly_debt = db.Column(db.Float, nullable=False, default=0.0) # in MWK
    credit_history_status = db.Column(
        db.String(30),
        nullable=False,
        default='Not checked'
    )
    prior_loan_count = db.Column(db.Integer, nullable=True)
    prior_loans_repaid = db.Column(db.Integer, nullable=True)
    late_payments_12m = db.Column(db.Integer, nullable=True)
    max_days_past_due_12m = db.Column(db.Integer, nullable=True)
    previous_default = db.Column(db.Boolean, nullable=True)
    supporting_document_path = db.Column(db.String(255), nullable=True)
    whatsapp_approval_status = db.Column(
        db.String(30),
        nullable=False,
        default='Not sent'
    )
    whatsapp_approval_sent_at = db.Column(db.DateTime, nullable=True)
    whatsapp_approval_error = db.Column(db.String(255), nullable=True)
    whatsapp_approval_template = db.Column(db.String(100), nullable=True)
    status = db.Column(db.String(50), nullable=False, default='Pending') # Pending, Under Review, Assessed, Approved, Declined
    created_at = db.Column(db.DateTime, default=utc_now)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    # Relationships
    predictions = db.relationship('RiskPrediction', backref='loan_application', lazy=True, cascade='all, delete-orphan')
    decision = db.relationship('LoanDecision', backref='loan_application', uselist=False, cascade='all, delete-orphan')

    @property
    def latest_prediction(self):
        if self.predictions:
            return sorted(self.predictions, key=lambda p: p.assessment_date, reverse=True)[0]
        return None

    def to_dict(self):
        pred = self.latest_prediction
        return {
            'id': self.id,
            'app_id': self.app_id,
            'applicant_name': self.applicant.full_name if self.applicant else 'Unknown',
            'applicant_code': self.applicant.applicant_id if self.applicant else '',
            'loan_amount': self.loan_amount,
            'loan_purpose': self.loan_purpose,
            'loan_term': self.loan_term,
            'interest_rate': self.interest_rate,
            'monthly_income': self.monthly_income,
            'existing_monthly_debt': self.existing_monthly_debt,
            'credit_history_status': self.credit_history_status,
            'prior_loan_count': self.prior_loan_count,
            'prior_loans_repaid': self.prior_loans_repaid,
            'late_payments_12m': self.late_payments_12m,
            'max_days_past_due_12m': self.max_days_past_due_12m,
            'previous_default': self.previous_default,
            'status': self.status,
            'created_at': self.created_at.strftime('%d %b %Y, %H:%M'),
            'default_probability': pred.default_probability if pred else None,
            'risk_category': pred.risk_category if pred else 'Not Assessed'
        }


class RiskPrediction(db.Model):
    __tablename__ = 'risk_predictions'

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey('organizations.id'), nullable=True) # Tenant scope
    application_id = db.Column(db.Integer, db.ForeignKey('loan_applications.id'), nullable=False)
    default_probability = db.Column(db.Float, nullable=False) # in % (e.g. 25.0)
    risk_category = db.Column(db.String(50), nullable=False) # Low Risk, Medium Risk, High Risk
    model_version = db.Column(db.String(100), nullable=False, default='Random Forest & XGBoost Ensemble v2.1')
    explanation_json = db.Column(db.Text, nullable=True) # JSON list of SHAP-like factors
    assessed_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    assessment_date = db.Column(db.DateTime, default=utc_now)

    @property
    def key_insights(self):
        if self.explanation_json:
            try:
                return json.loads(self.explanation_json)
            except Exception:
                return [self.explanation_json]
        return []


class LoanDecision(db.Model):
    __tablename__ = 'loan_decisions'

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey('organizations.id'), nullable=True) # Tenant scope
    application_id = db.Column(db.Integer, db.ForeignKey('loan_applications.id'), unique=True, nullable=False)
    decision = db.Column(db.String(50), nullable=False) # Approved, Conditional Approval, Declined, Referred
    notes = db.Column(db.Text, nullable=True)
    decided_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    decided_at = db.Column(db.DateTime, default=utc_now)


# ==============================================================================
# 4. Machine Learning Model Governance & Evaluations
# ==============================================================================
class ModelVersion(db.Model):
    __tablename__ = 'model_versions'

    id = db.Column(db.Integer, primary_key=True)
    model_name = db.Column(db.String(100), nullable=False)
    version_number = db.Column(db.String(50), nullable=False) # e.g. v2.1
    training_date = db.Column(db.DateTime, default=utc_now)
    training_dataset_reference = db.Column(db.String(255), default='10,000 Retail & SME Consumer Loans (Malawi FinScope Benchmark)')
    feature_schema_version = db.Column(db.String(50), default='v1.0')
    model_status = db.Column(db.String(50), default='Active Production') # Active Production, Candidate, Archived
    artifact_path = db.Column(db.String(255), nullable=True)


class ModelEvaluation(db.Model):
    __tablename__ = 'model_evaluations'

    id = db.Column(db.Integer, primary_key=True)
    model_name = db.Column(db.String(100), nullable=False) # Logistic Regression, Decision Tree, Random Forest, XGBoost
    accuracy = db.Column(db.Float, nullable=False) # e.g. 0.90
    precision = db.Column(db.Float, nullable=False) # e.g. 0.80
    recall = db.Column(db.Float, nullable=False) # e.g. 0.82
    f1_score = db.Column(db.Float, nullable=False) # e.g. 0.81
    roc_auc = db.Column(db.Float, nullable=False) # e.g. 0.92
    pr_auc = db.Column(db.Float, nullable=True, default=0.74) # Precision-Recall AUC (Module 8)
    status = db.Column(db.String(50), default='Evaluated') # Active Production, Candidate, Baseline
    dataset_summary = db.Column(db.String(255), default='10,000 Retail & SME Consumer Loans')
    evaluation_date = db.Column(db.DateTime, default=utc_now)


# ==============================================================================
# 5. Security Audit Trail & System Configuration
# ==============================================================================
class AuditLog(db.Model):
    __tablename__ = 'audit_logs'

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey('organizations.id'), nullable=True) # Tenant scope
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    user_code = db.Column(db.String(50), nullable=True, default='SYSTEM')
    username = db.Column(db.String(150), nullable=True, default='System')
    action = db.Column(db.String(150), nullable=False)
    target_record = db.Column(db.String(100), nullable=True)
    details = db.Column(db.Text, nullable=True)
    ip_address = db.Column(db.String(50), default='127.0.0.1')
    timestamp = db.Column(db.DateTime, default=utc_now)

    def to_dict(self):
        return {
            'id': self.id,
            'timestamp': self.timestamp.strftime('%d %b %Y, %H:%M'),
            'user': self.user_code or self.username,
            'action': self.action,
            'record': self.target_record or '-',
            'details': self.details or ''
        }


class SystemSetting(db.Model):
    __tablename__ = 'system_settings'

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.Integer, db.ForeignKey('organizations.id'), nullable=True) # Global if null
    key = db.Column(db.String(100), nullable=False)
    value = db.Column(db.Text, nullable=False)
    description = db.Column(db.String(255), nullable=True)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)
    updated_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)


# Keep legacy aliases for backwards compatibility with any existing scripts
LoanAssessment = LoanApplication
