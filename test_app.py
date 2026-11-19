import io
import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from unittest.mock import patch
from urllib.error import URLError

from __init__ import create_app
from database import db
from models import (
    Applicant, AuditLog, LoanApplication, ModelEvaluation,
    ModelVersion, Organization, RiskPrediction, SystemSetting, User
)


class OrganizationWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = os.path.join(self.temp_dir.name, 'test.sqlite')
        self.database_override = patch.dict(
            os.environ,
            {'LOAN_RISK_DATABASE_PATH': self.database_path}
        )
        self.database_override.start()
        self.app = create_app()
        self.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
            MODEL_ARTIFACT_DIRECTORY=os.path.join(self.temp_dir.name, 'model_artifacts')
        )
        self.client = self.app.test_client()
        self.registration_count = 0

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        self.database_override.stop()
        self.temp_dir.cleanup()

    def register_organization(self, name, admin_email):
        self.registration_count += 1
        return self.client.post('/register-organization', data={
            'org_name': name,
            'org_type': 'Microfinance Institution',
            'org_email': f'info@{admin_email.split("@")[1]}',
            'org_phone': '+265 999 000 000',
            'org_location': 'Lilongwe',
            'org_address': 'Area 3, Lilongwe',
            'admin_name': f'{name} Admin',
            'admin_email': admin_email,
            'admin_phone': '+265 888 111 111',
            'admin_national_id': f'{self.registration_count:08d}',
            'admin_password': 'secure-password-123',
            'admin_confirm_password': 'secure-password-123'
        }, follow_redirects=True)

    def create_platform_owner(self):
        result = self.app.test_cli_runner().invoke(
            args=['create-platform-owner'],
            input='Platform Operator\noperator@example.com\nsecure-owner-password\nsecure-owner-password\n'
        )
        self.assertEqual(result.exit_code, 0, result.output)

    def approve_organization(self, organization_id):
        self.client.post('/login', data={
            'email': 'operator@example.com',
            'password': 'secure-owner-password'
        })
        return self.client.post(
            f'/platform-admin/approve-organization/{organization_id}',
            follow_redirects=True
        )

    def test_database_starts_empty_and_landing_is_simple(self):
        with self.app.app_context():
            self.assertEqual(Organization.query.count(), 0)
            self.assertEqual(User.query.count(), 0)
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Predict loan default risk', response.data)
        self.assertIn(b'machine learning', response.data)
        self.assertNotIn(b'bank-hero.svg', response.data)
        self.assertIn(b'Learn More', response.data)
        self.assertIn(b'Get Started', response.data)
        self.assertIn(b'About us', response.data)
        self.assertIn(b'Platform features', response.data)
        self.assertIn(b'How it works', response.data)
        self.assertIn(b'Who we serve', response.data)
        self.assertIn(b'Mission and vision', response.data)
        self.assertIn(b'Security and data protection', response.data)
        self.assertIn(b'Frequently asked questions', response.data)
        self.assertIn(b'Contact us', response.data)
        self.assertIn(b'Privacy Policy', response.data)
        self.assertIn(b'Terms and Conditions', response.data)
        self.assertIn(b'lm-section-tabs', response.data)
        self.assertIn(b'data-lm-tab="features"', response.data)
        self.assertIn(b'data-lm-panel="contact"', response.data)
        self.assertNotIn(b'up to 90% accuracy', response.data)
        self.assertNotIn(b'analyst-work.svg', response.data)
        self.assertNotIn(b'Create an organization account', response.data)
        self.assertNotIn(b'Log in to your account', response.data)
        self.assertNotIn(b'Data Pre-processing', response.data)
        create_account = self.client.get('/register-organization')
        self.assertIn(b'Company profile', create_account.data)
        self.assertIn(b'name="org_location"', create_account.data)
        self.assertIn(b'name="org_address"', create_account.data)
        self.assertIn(b'name="admin_phone"', create_account.data)
        self.assertIn(b'name="admin_national_id"', create_account.data)
        self.assertIn(b'Create account and submit for approval', create_account.data)
        self.assertIn(b'platform owner approves it', create_account.data)
        self.assertEqual(self.client.get('/signup').location, '/login')

    def test_login_page_uses_loan_background_and_preserves_login_fields(self):
        response = self.client.get('/login')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'login-background.png', response.data)
        self.assertIn(b'name="email"', response.data)
        self.assertIn(b'name="password"', response.data)
        self.assertIn(b'name="remember"', response.data)
        self.assertIn(b'Create account', response.data)

    def test_organization_requires_approval_before_login(self):
        response = self.register_organization('Nyasa Lending', 'admin@nyasa.example')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Account request submitted', response.data)
        self.assertIn(b'pending platform owner approval', response.data)
        with self.app.app_context():
            organization = Organization.query.one()
            admin = User.query.filter_by(email='admin@nyasa.example').one()
            self.assertEqual(organization.verification_status, 'Pending Approval')
            self.assertEqual(admin.role, 'Organization Administrator')
            self.assertEqual(organization.location, 'Lilongwe')
            self.assertEqual(organization.address, 'Area 3, Lilongwe')
            self.assertEqual(admin.phone_number, '+265888111111')
            self.assertEqual(admin.national_id, '00000001')
            organization_id = organization.id

        pending_login = self.client.post('/login', data={
            'email': 'admin@nyasa.example',
            'password': 'secure-password-123'
        })
        self.assertIn(b'waiting for platform approval', pending_login.data)

        self.create_platform_owner()
        approval = self.approve_organization(organization_id)
        self.assertIn(b'approved and can now sign in', approval.data)
        self.client.get('/logout')
        admin_login = self.client.post('/login', data={
            'email': 'admin@nyasa.example',
            'password': 'secure-password-123'
        }, follow_redirects=True)
        self.assertEqual(admin_login.status_code, 200)
        self.assertIn(b'Welcome, Nyasa Lending Admin', admin_login.data)

    def test_prediction_detail_shows_probability_charts_and_underwriting_applicant(self):
        self.register_organization('Nyasa Lending', 'admin@nyasa.example')
        with self.app.app_context():
            organization = Organization.query.one()
            organization.verification_status = 'Verified'
            applicant = Applicant(
                organization_id=organization.id,
                applicant_id='BOR-1001',
                full_name='Amina Banda',
                phone_number='+265999111222',
                email='amina@example.com',
                age=34,
                employment_status='Employed',
                monthly_income=400000,
                employment_duration=5,
                residential_area='Lilongwe'
            )
            db.session.add(applicant)
            db.session.flush()
            test_organization_id = organization.id
            test_applicant_id = applicant.id
            application = LoanApplication(
                organization_id=test_organization_id,
                app_id='APP-1001',
                applicant_id=test_applicant_id,
                loan_amount=1200000,
                loan_purpose='Business',
                loan_term=12,
                interest_rate=18,
                monthly_income=400000,
                existing_monthly_debt=50000
            )
            db.session.add(application)
            db.session.flush()
            db.session.add(RiskPrediction(
                organization_id=organization.id,
                application_id=application.id,
                default_probability=23.4,
                risk_category='Medium Risk'
            ))
            db.session.commit()
            application_id = application.id

        self.client.post('/login', data={
            'email': 'admin@nyasa.example',
            'password': 'secure-password-123'
        })
        response = self.client.get(f'/predict/{application_id}')

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'23.4% default probability', response.data)
        self.assertIn(b'76.6% estimated non-default', response.data)
        self.assertIn(b'class="risk-donut moderate"', response.data)
        self.assertIn(b'--risk-probability: 23.4%;', response.data)
        self.assertIn(b'Borrower Financial Summary', response.data)
        self.assertIn(b'12.5%', response.data)
        self.assertIn(b'Amina Banda', response.data)
        self.assertIn(b'Update Prediction', response.data)
        self.assertNotIn(b'Key risk factors', response.data)
        self.assertNotIn(b'Assessment insight', response.data)
        self.assertNotIn(b'verifying evidence', response.data)

        with patch('main.urlopen') as no_consent_send:
            saved = self.client.post(
                f'/record-decision/{application_id}',
                data={'decision': 'Approved', 'notes': 'Decision rationale recorded.'},
                follow_redirects=True
            )
        no_consent_send.assert_not_called()

        self.assertEqual(saved.status_code, 200)
        self.assertIn(b'Decision saved successfully for APP-1001: Approved.', saved.data)
        self.assertIn(b'WhatsApp notification not sent', saved.data)
        self.assertIn(b'class="decision-flash warning"', saved.data)
        self.assertIn(b'aria-label="Saved underwriting decision"', saved.data)
        self.assertIn(b'Decision saved', saved.data)
        self.assertIn(b'Decision rationale recorded.', saved.data)
        self.assertIn(b'Application status: <strong>Approved</strong>', saved.data)
        self.assertIn(b'Update Lending Decision', saved.data)
        with self.app.app_context():
            saved_application = db.session.get(LoanApplication, application_id)
            self.assertEqual(saved_application.status, 'Approved')
            self.assertEqual(saved_application.decision.decision, 'Approved')
            self.assertEqual(saved_application.whatsapp_approval_status, 'Consent required')
        self.assertIn(b'WhatsApp approval notification: Consent required', saved.data)

        risk_page = self.client.get('/predictions')
        self.assertEqual(risk_page.status_code, 200)
        self.assertIn(b'Risk assessment graphs', risk_page.data)
        self.assertIn(b'id="riskDonutCanvas"', risk_page.data)
        self.assertIn(b'data-low="0"', risk_page.data)
        self.assertIn(b'data-medium="1"', risk_page.data)
        self.assertIn(b'data-high="0"', risk_page.data)
        self.assertIn(b'id="trendLineCanvas"', risk_page.data)
        self.assertIn(b'data-values=\'[23.4]\'', risk_page.data)
        self.assertIn(b'Average model estimate by assessment date', risk_page.data)
        self.assertIn(b'id="risk-filter-title">Find an assessment', risk_page.data)
        self.assertIn(b'Filters only change the results shown; they do not change a prediction.', risk_page.data)
        self.assertIn(b'Export full history CSV', risk_page.data)
        filtered_page = self.client.get('/predictions?search=APP-1001&risk=Medium%20Risk')
        self.assertIn(b'Matching risk assessments (1)', filtered_page.data)
        self.assertIn(b'Active filters:', filtered_page.data)
        self.assertIn(b'Clear filters', filtered_page.data)

        with self.app.app_context():
            second_applicant = Applicant(
                organization_id=test_organization_id,
                applicant_id='BOR-1002',
                full_name='Amina Banda Two',
                phone_number='+265999111223',
                email='amina.two@example.com',
                age=35,
                employment_status='Employed',
                monthly_income=400000,
                employment_duration=5,
                residential_area='Lilongwe'
            )
            db.session.add(second_applicant)
            db.session.flush()
            saved_application = LoanApplication(
                organization_id=test_organization_id,
                app_id='APP-1002',
                applicant_id=second_applicant.id,
                loan_amount=800000,
                loan_purpose='Education',
                loan_term=10,
                interest_rate=18,
                monthly_income=400000,
                existing_monthly_debt=50000
            )
            db.session.add(saved_application)
            db.session.commit()
            second_application_id = saved_application.id

        first_run_page = self.client.get(f'/predict/{second_application_id}')
        self.assertIn(b'Run Prediction', first_run_page.data)
        with patch('main.ml_model.predict', side_effect=[
            {'default_probability': 8.2, 'key_insights': []},
            {'default_probability': 7.1, 'key_insights': []}
        ]):
            first_run = self.client.post(
                f'/predict/{second_application_id}',
                follow_redirects=True
            )
            self.assertIn(b'Risk prediction generated for APP-1002: 8.2%', first_run.data)
            self.assertIn(b'Update Prediction', first_run.data)

            with self.app.app_context():
                saved_prediction = RiskPrediction.query.filter_by(
                    application_id=second_application_id
                ).one()
                saved_prediction_id = saved_prediction.id

            second_run = self.client.post(
                f'/predict/{second_application_id}',
                follow_redirects=True
            )
            self.assertIn(b'Risk prediction updated for APP-1002: 7.1%', second_run.data)
            self.assertIn(b'Update Prediction', second_run.data)

        with self.app.app_context():
            predictions = RiskPrediction.query.filter_by(
                application_id=second_application_id
            ).all()
            self.assertEqual(len(predictions), 1)
            self.assertEqual(predictions[0].id, saved_prediction_id)
            self.assertEqual(predictions[0].default_probability, 7.1)

    def test_whatsapp_approval_sends_once_for_opted_in_borrower(self):
        self.register_organization('WhatsApp Finance', 'admin@whatsapp.example')
        with self.app.app_context():
            organization = Organization.query.one()
            organization.verification_status = 'Verified'
            applicant = Applicant(
                organization_id=organization.id,
                applicant_id='BOR-2001',
                full_name='Chisomo Phiri',
                phone_number='0991234567',
                email='chisomo@example.com',
                age=30,
                employment_status='Employed',
                monthly_income=350000,
                employment_duration=4,
                residential_area='Blantyre',
                whatsapp_opt_in=True
            )
            db.session.add(applicant)
            db.session.flush()
            application = LoanApplication(
                organization_id=organization.id,
                app_id='APP-2001',
                applicant_id=applicant.id,
                loan_amount=500000,
                loan_purpose='Business',
                loan_term=12,
                interest_rate=18,
                monthly_income=350000,
                existing_monthly_debt=25000
            )
            db.session.add(application)
            db.session.commit()
            application_id = application.id

        self.app.config.update(
            WHATSAPP_ACCESS_TOKEN='test-access-token',
            WHATSAPP_PHONE_NUMBER_ID='123456789',
            WHATSAPP_GRAPH_API_VERSION='v23.0',
            WHATSAPP_APPROVAL_TEMPLATES=json.dumps([
                {
                    'name': 'approval_short',
                    'language': 'en',
                    'label': 'Short approval',
                    'body': 'Hello {{1}}, application {{2}} is approved for {{3}}.',
                    'fields': ['borrower_name', 'application_id', 'loan_amount']
                },
                {
                    'name': 'loan_approved_full',
                    'language': 'en',
                    'label': 'Approval with loan details',
                    'body': (
                        'Hello {{1}}, application {{2}} has been approved for {{3}}. '
                        'Purpose: {{4}}. Loan term: {{5}} months. Interest rate: {{6}}%.'
                    ),
                    'fields': [
                        'borrower_name',
                        'application_id',
                        'loan_amount',
                        'loan_purpose',
                        'loan_term',
                        'interest_rate'
                    ]
                }
            ])
        )
        self.client.post('/login', data={
            'email': 'admin@whatsapp.example',
            'password': 'secure-password-123'
        })
        assessment_page = self.client.get(f'/predict/{application_id}')
        self.assertIn(b'Approval with loan details', assessment_page.data)
        self.assertIn(b'MWK 500,000', assessment_page.data)
        self.assertIn(b'Purpose: Business. Loan term: 12 months. Interest rate: 18%.', assessment_page.data)

        with patch('main.urlopen') as send:
            send.return_value.__enter__.return_value.read.return_value = (
                b'{"messages":[{"id":"wamid.test"}]}'
            )
            response = self.client.post(
                f'/record-decision/{application_id}',
                data={
                    'decision': 'Approved',
                    'whatsapp_template': 'loan_approved_full'
                },
                follow_redirects=True
            )

        self.assertIn(b'WhatsApp approval notification sent', response.data)
        with self.app.app_context():
            saved_application = db.session.get(LoanApplication, application_id)
            self.assertEqual(saved_application.decision.decision, 'Approved')
            self.assertEqual(saved_application.whatsapp_approval_status, 'Sent')
            self.assertIsNotNone(saved_application.whatsapp_approval_sent_at)
            self.assertEqual(saved_application.whatsapp_approval_template, 'loan_approved_full')
        api_request = send.call_args[0][0]
        payload = json.loads(api_request.data.decode('utf-8'))
        self.assertEqual(payload['to'], '265991234567')
        self.assertEqual(payload['template']['name'], 'loan_approved_full')
        self.assertEqual(
            [parameter['text'] for parameter in payload['template']['components'][0]['parameters']],
            [
                'Chisomo Phiri',
                'APP-2001',
                'MWK 500,000',
                'Business',
                '12',
                '18'
            ]
        )
        self.assertNotIn('loan_amount', payload)

        with patch('main.urlopen') as duplicate_send:
            updated = self.client.post(
                f'/record-decision/{application_id}',
                data={'decision': 'Approved', 'notes': 'Updated rationale.'},
                follow_redirects=True
            )
        self.assertEqual(updated.status_code, 200)
        duplicate_send.assert_not_called()
        with patch('main.urlopen') as duplicate_retry:
            retry_result = self.client.post(
                f'/applications/{application_id}/whatsapp-approval/retry',
                follow_redirects=True
            )
        self.assertIn(b'already been sent', retry_result.data)
        duplicate_retry.assert_not_called()

    def test_whatsapp_failure_keeps_approval_and_can_be_retried(self):
        self.register_organization('Retry Finance', 'admin@retry.example')
        with self.app.app_context():
            organization = Organization.query.one()
            organization.verification_status = 'Verified'
            applicant = Applicant(
                organization_id=organization.id,
                applicant_id='BOR-3001',
                full_name='Tadala Zulu',
                phone_number='+265 999 123 456',
                email='tadala@example.com',
                age=29,
                employment_status='Self-Employed',
                monthly_income=280000,
                employment_duration=3,
                residential_area='Mzuzu',
                whatsapp_opt_in=True
            )
            db.session.add(applicant)
            db.session.flush()
            application = LoanApplication(
                organization_id=organization.id,
                app_id='APP-3001',
                applicant_id=applicant.id,
                loan_amount=400000,
                loan_purpose='Agriculture',
                loan_term=10,
                interest_rate=18,
                monthly_income=280000,
                existing_monthly_debt=20000
            )
            db.session.add(application)
            db.session.commit()
            application_id = application.id

        self.app.config.update(
            WHATSAPP_ACCESS_TOKEN='test-access-token',
            WHATSAPP_PHONE_NUMBER_ID='123456789',
            WHATSAPP_GRAPH_API_VERSION='v23.0',
            WHATSAPP_APPROVAL_TEMPLATE_NAME='loan_approved',
            WHATSAPP_TEMPLATE_LANGUAGE='en'
        )
        self.client.post('/login', data={
            'email': 'admin@retry.example',
            'password': 'secure-password-123'
        })
        with patch('main.urlopen', side_effect=URLError('private transport detail')):
            failed = self.client.post(
                f'/record-decision/{application_id}',
                data={'decision': 'Approved'},
                follow_redirects=True
            )
        self.assertIn(b'WhatsApp service could not be reached', failed.data)
        with self.app.app_context():
            saved_application = db.session.get(LoanApplication, application_id)
            self.assertEqual(saved_application.decision.decision, 'Approved')
            self.assertEqual(saved_application.whatsapp_approval_status, 'Failed')
            self.assertNotIn(
                'private transport detail',
                saved_application.whatsapp_approval_error
            )

        with patch('main.urlopen') as retry_send:
            retry_send.return_value.__enter__.return_value.read.return_value = (
                b'{"messages":[{"id":"wamid.retry"}]}'
            )
            retried = self.client.post(
                f'/applications/{application_id}/whatsapp-approval/retry',
                follow_redirects=True
            )
        self.assertIn(b'WhatsApp approval notification sent successfully', retried.data)
        retry_send.assert_called_once()
        with self.app.app_context():
            saved_application = db.session.get(LoanApplication, application_id)
            self.assertEqual(saved_application.whatsapp_approval_status, 'Sent')

    def test_application_credit_history_is_captured_and_displayed_without_scoring(self):
        self.register_organization('Credit History Finance', 'admin@history.example')
        with self.app.app_context():
            organization = Organization.query.one()
            organization.verification_status = 'Verified'
            applicant = Applicant(
                organization_id=organization.id,
                applicant_id='BOR-2001',
                full_name='Chisomo Phiri',
                phone_number='+265999111222',
                email='chisomo@example.com',
                age=31,
                employment_status='Business Owner',
                monthly_income=350000,
                employment_duration=4,
                residential_area='Mzuzu'
            )
            db.session.add(applicant)
            db.session.commit()
            organization_id = organization.id
            applicant_id = applicant.id

        self.client.post('/login', data={
            'email': 'admin@history.example',
            'password': 'secure-password-123'
        })
        applications_page = self.client.get('/applications')
        self.assertIn(b'Previous credit history', applications_page.data)
        self.assertIn(b'No previous loans', applications_page.data)

        created = self.client.post('/applications/new', data={
            'applicant_id': str(applicant_id),
            'loan_amount': '600000',
            'loan_purpose': 'Business',
            'loan_term': '12',
            'interest_rate': '18',
            'monthly_income': '350000',
            'existing_monthly_debt': '40000',
            'credit_history_status': 'History available',
            'prior_loan_count': '3',
            'prior_loans_repaid': '2',
            'late_payments_12m': '1',
            'max_days_past_due_12m': '20',
            'previous_default': 'yes'
        }, follow_redirects=True)
        self.assertIn(b'submitted successfully', created.data)

        repeated_submission = self.client.post('/applications/new', data={
            'applicant_id': str(applicant_id),
            'loan_amount': '600000',
            'loan_purpose': 'Business',
            'loan_term': '12',
            'interest_rate': '18',
            'monthly_income': '350000',
            'existing_monthly_debt': '40000'
        }, follow_redirects=True)
        self.assertIn(b'Borrower already exists with a loan application', repeated_submission.data)
        self.assertIn(b'Borrower already exists', repeated_submission.data)

        with self.app.app_context():
            application = LoanApplication.query.filter_by(
                organization_id=organization_id
            ).one()
            application_id = application.id
            self.assertEqual(application.credit_history_status, 'History available')
            self.assertEqual(application.prior_loan_count, 3)
            self.assertEqual(application.prior_loans_repaid, 2)
            self.assertEqual(application.late_payments_12m, 1)
            self.assertEqual(application.max_days_past_due_12m, 20)
            self.assertIs(application.previous_default, True)

        assessment_page = self.client.get(f'/predict/{application_id}')
        self.assertIn(b'Prior credit history', assessment_page.data)
        self.assertIn(b'History available', assessment_page.data)
        self.assertIn(b'Longest delay (12 months)', assessment_page.data)
        self.assertIn(b'do not currently affect the model prediction', assessment_page.data)

        updated = self.client.post(
            f'/applications/{application_id}/update',
            data={
                'credit_history_status': 'No prior loans',
                'previous_default': 'unknown'
            },
            follow_redirects=True
        )
        self.assertIn(b'updated successfully', updated.data)
        with self.app.app_context():
            application = db.session.get(LoanApplication, application_id)
            self.assertEqual(application.credit_history_status, 'No prior loans')
            self.assertEqual(application.prior_loan_count, 0)
            self.assertEqual(application.prior_loans_repaid, 0)
            self.assertIsNone(application.previous_default)

        assessment_page = self.client.get(f'/predict/{application_id}')
        self.assertIn(b'No previous borrowing was reported', assessment_page.data)
        with self.app.app_context():
            application = db.session.get(LoanApplication, application_id)
            application.status = 'Declined'
            db.session.commit()

        declined_repeat = self.client.post('/applications/new', data={
            'applicant_id': str(applicant_id),
            'loan_amount': '600000',
            'loan_purpose': 'Business',
            'loan_term': '12',
            'interest_rate': '18',
            'monthly_income': '350000',
            'existing_monthly_debt': '40000'
        }, follow_redirects=True)
        self.assertIn(b'Borrower already exists with a loan application', declined_repeat.data)
        with self.app.app_context():
            self.assertEqual(
                LoanApplication.query.filter_by(
                    organization_id=organization_id,
                    applicant_id=applicant_id
                ).count(),
                1
            )

    def test_platform_owner_can_manage_companies_and_review_platform_activity(self):
        self.register_organization('Nyasa Lending', 'admin@nyasa.example')
        self.register_organization('Mala Finance', 'admin@mala.example')
        with self.app.app_context():
            nyasa = Organization.query.filter_by(name='Nyasa Lending').one()
            mala = Organization.query.filter_by(name='Mala Finance').one()
            nyasa_id = nyasa.id
            mala_id = mala.id

        self.create_platform_owner()
        self.client.post('/login', data={
            'email': 'operator@example.com',
            'password': 'secure-owner-password'
        })
        dashboard = self.client.get('/platform-admin')
        self.assertIn(b'Registered companies', dashboard.data)
        self.assertNotIn(b'data-platform-activity-panel', dashboard.data)
        self.assertIn(b'data-company-search', dashboard.data)
        self.assertIn(b'platform-activity-nav-button', dashboard.data)
        self.assertNotIn(b'Security management', dashboard.data)
        self.assertNotIn(
            '/platform-security',
            {rule.rule for rule in self.app.url_map.iter_rules()}
        )
        self.assertIn(b'<span class="link-text">Platform activity</span>', dashboard.data)
        self.assertIn(b'href="/platform-activity"', dashboard.data)
        self.assertIn(b'Registration date', dashboard.data)
        self.assertIn(b'Account status', dashboard.data)
        self.assertIn(b'aria-label="Account status: Pending Approval"', dashboard.data)
        self.assertIn(b'<time datetime=', dashboard.data)
        self.assertIn(b'Nyasa Lending', dashboard.data)
        self.assertIn(b'REGISTERING ADMINISTRATOR', dashboard.data)
        self.assertIn(b'Nyasa Lending Admin', dashboard.data)
        self.assertIn(b'admin@nyasa.example', dashboard.data)
        self.assertIn(b'+265888111111', dashboard.data)
        self.assertIn(b'Organization Administrator', dashboard.data)
        self.assertIn(b'Account created', dashboard.data)
        self.assertIn(b'<strong>2</strong>', dashboard.data)
        self.assertLess(
            dashboard.data.index(b'<span class="link-text">Company management</span>'),
            dashboard.data.index(b'<span class="link-text">Platform activity</span>')
        )
        activity_page = self.client.get('/platform-activity')
        self.assertEqual(activity_page.status_code, 200)
        self.assertIn(b'<h1 class="welcome-heading">Platform activity</h1>', activity_page.data)
        self.assertIn(b'All activity', activity_page.data)
        self.assertIn(b'class="dashboard-main-content platform-activity-page"', activity_page.data)
        self.assertNotIn(b'Registered companies', activity_page.data)
        image_response = self.client.get('/static/images/platform-loan-office.png')
        self.assertEqual(image_response.status_code, 200)
        self.assertEqual(image_response.mimetype, 'image/png')
        image_response.close()
        self.assertIn(b'class="dashboard-main-content platform-company-page"', dashboard.data)

        rejected = self.client.post(
            f'/platform-admin/reject-organization/{mala_id}',
            follow_redirects=True
        )
        self.assertIn(b'registration was rejected', rejected.data)
        rejected_dashboard = self.client.get('/platform-admin')
        self.assertIn(b'Rejected companies', rejected_dashboard.data)
        self.assertIn(b'Mala Finance', rejected_dashboard.data)
        self.assertIn(b'aria-label="Account status: Rejected"', rejected_dashboard.data)
        self.assertIn(b'Mala Finance Admin', rejected_dashboard.data)
        self.assertIn(b'admin@mala.example', rejected_dashboard.data)
        self.assertIn(b'<strong>1</strong>', rejected_dashboard.data)
        self.assertIn(b'Approve and activate', rejected_dashboard.data)
        self.approve_organization(nyasa_id)
        self.client.post(f'/platform-admin/toggle-org/{nyasa_id}')
        suspended_dashboard = self.client.get('/platform-admin')
        self.assertIn(b'Suspended companies', suspended_dashboard.data)
        self.assertIn(b'Nyasa Lending', suspended_dashboard.data)
        self.assertIn(b'aria-label="Account status: Suspended"', suspended_dashboard.data)
        self.assertIn(b'Nyasa Lending Admin', suspended_dashboard.data)
        self.assertIn(b'admin@nyasa.example', suspended_dashboard.data)
        self.assertIn(b'Reactivate company', suspended_dashboard.data)
        self.assertIn(b'<strong>1</strong>', suspended_dashboard.data)
        reactivated = self.client.post(
            f'/platform-admin/organization/{nyasa_id}/status',
            data={'status': 'Verified'},
            follow_redirects=True
        )
        self.assertIn(b'Nyasa Lending status updated to Verified', reactivated.data)
        self.client.post(f'/platform-admin/organization/{nyasa_id}/status', data={
            'status': 'Deactivated'
        })
        self.client.post(f'/platform-admin/organization/{nyasa_id}/status', data={
            'status': 'Verified'
        })
        updated = self.client.post(
            f'/platform-admin/organization/{nyasa_id}/update',
            data={
                'name': 'Nyasa Lending Group',
                'org_type': 'Microfinance Institution',
                'email': 'info@nyasa.example',
                'phone': '+265 999 111 111',
                'location': 'Blantyre',
                'address': 'Area 4, Blantyre'
            },
            follow_redirects=True
        )
        self.assertIn(b'Nyasa Lending Group company details updated', updated.data)

        deleted = self.client.post(
            f'/platform-admin/organization/{nyasa_id}/delete',
            follow_redirects=True
        )
        self.assertIn(b'and its workspace records were deleted', deleted.data)
        activity_after_delete = self.client.get('/platform-activity')
        self.assertIn(b'Deleted organization', activity_after_delete.data)
        with self.app.app_context():
            self.assertIsNone(db.session.get(Organization, nyasa_id))
            self.assertEqual(
                Organization.query.filter_by(verification_status='Rejected').count(),
                1
            )

    def test_platform_owner_sees_registering_admin_and_can_contact_company(self):
        self.register_organization('Admin Controls Finance', 'admin@controls.example')
        with self.app.app_context():
            admin = User.query.filter_by(email='admin@controls.example').one()
            admin_id = admin.id
        self.create_platform_owner()
        self.client.post('/login', data={
            'email': 'operator@example.com',
            'password': 'secure-owner-password'
        })
        dashboard = self.client.get('/platform-admin')
        self.assertIn(b'REGISTERING ADMINISTRATOR', dashboard.data)
        self.assertIn(b'Admin Controls Finance Admin', dashboard.data)
        self.assertIn(b'admin@controls.example', dashboard.data)
        self.assertIn(b'+265888111111', dashboard.data)
        self.assertIn(b'href="mailto:info@controls.example?', dashboard.data)
        self.assertIn(b'href="tel:+265999000000"', dashboard.data)
        self.assertNotIn(b'Contact company', dashboard.data)
        self.assertNotIn(b'Update administrator', dashboard.data)
        self.assertNotIn(b'Suspend admin', dashboard.data)
        self.assertNotIn(b'Deactivate admin', dashboard.data)
        self.assertNotIn(b'Delete admin', dashboard.data)
        for action in ('update', 'status', 'delete'):
            response = self.client.post(
                f'/platform-admin/organization-admin/{admin_id}/{action}',
                data={'status': 'Suspended'}
            )
            self.assertEqual(response.status_code, 404)
        with self.app.app_context():
            self.assertEqual(db.session.get(User, admin_id).status, 'Active')

    def test_platform_owner_can_export_delete_and_clear_all_activity(self):
        self.register_organization('Workbook Finance', 'admin@workbook.example')
        self.create_platform_owner()
        self.client.post('/login', data={
            'email': 'operator@example.com',
            'password': 'secure-owner-password'
        })

        with self.app.app_context():
            initial_count = AuditLog.query.count()
            activity_id = AuditLog.query.order_by(AuditLog.id.asc()).first().id
        exported = self.client.get('/platform-activity/export')
        self.assertEqual(exported.status_code, 200)
        self.assertEqual(
            exported.mimetype,
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        with zipfile.ZipFile(io.BytesIO(exported.data)) as workbook:
            self.assertIn('xl/worksheets/sheet1.xml', workbook.namelist())
            sheet = ET.fromstring(workbook.read('xl/worksheets/sheet1.xml'))
            cell_values = [
                node.text for node in sheet.iter()
                if node.tag.endswith('}t')
            ]
            self.assertIn('Action', cell_values)
            self.assertIn('Organization registration submitted', cell_values)
            self.assertIn('Workbook Finance', cell_values)
            sheet_data = next(node for node in sheet.iter() if node.tag.endswith('}sheetData'))
            self.assertEqual(len(sheet_data), initial_count + 1)

        deleted = self.client.post(f'/platform-activity/{activity_id}/delete')
        self.assertEqual(deleted.status_code, 302)
        with self.app.app_context():
            self.assertIsNone(db.session.get(AuditLog, activity_id))

        cleared = self.client.post('/platform-activity/clear', follow_redirects=True)
        self.assertIn(b'All platform activity entries have been cleared', cleared.data)
        with self.app.app_context():
            self.assertEqual(AuditLog.query.count(), 0)
        empty_export = self.client.get('/platform-activity/export')
        with zipfile.ZipFile(io.BytesIO(empty_export.data)) as workbook:
            sheet = ET.fromstring(workbook.read('xl/worksheets/sheet1.xml'))
            sheet_data = next(node for node in sheet.iter() if node.tag.endswith('}sheetData'))
            self.assertEqual(len(sheet_data), 1)

    def test_platform_owner_can_manage_model_versions_and_evaluations(self):
        self.create_platform_owner()
        self.client.post('/login', data={
            'email': 'operator@example.com',
            'password': 'secure-owner-password'
        })

        model_page = self.client.get('/platform-models')
        self.assertEqual(model_page.status_code, 200)
        self.assertIn(b'Machine learning model management', model_page.data)
        self.assertIn(b'Upload a trained model', model_page.data)
        self.assertIn(b'Prediction errors', model_page.data)
        self.assertIn(b'Train a model', model_page.data)
        self.assertIn(b'action="/model-training/run"', model_page.data)
        self.assertIn(b'name="training_dataset"', model_page.data)
        self.assertIn(b'name="target_column"', model_page.data)
        self.assertIn(b'href="/platform-models"', model_page.data)
        self.assertIn(b'class="dashboard-main-content platform-model-page"', model_page.data)
        static_response = self.client.get('/static/extended_beuaty.css')
        self.assertEqual(static_response.status_code, 200)
        self.assertIn(
            b'.dashboard-main-content.platform-model-page',
            static_response.get_data()
        )
        self.assertIn(b'height: 100dvh', static_response.get_data())
        self.assertIn(b'.dashboard-sidebar', static_response.get_data())
        self.assertIn(b'overscroll-behavior-y: contain', static_response.get_data())
        static_response.close()
        with self.app.app_context():
            db.session.add(AuditLog(
                action='Model prediction error',
                target_record='APP-ERROR-1',
                details='Model version Test v1: RuntimeError: test inference failure',
                username='Test platform owner'
            ))
            db.session.commit()
        errors_page = self.client.get('/platform-models')
        self.assertIn(b'APP-ERROR-1', errors_page.data)
        self.assertIn(b'test inference failure', errors_page.data)

        upload = self.client.post('/platform-models/upload', data={
            'model_name': 'Loan Default Model',
            'version_number': 'v1.0',
            'model_file': (io.BytesIO(b'test model artifact'), 'loan_model.joblib')
        }, content_type='multipart/form-data', follow_redirects=True)
        self.assertIn(b'uploaded as a candidate model', upload.data)
        with self.app.app_context():
            version = ModelVersion.query.filter_by(model_name='Loan Default Model').one()
            version_id = version.id
            artifact_path = os.path.join(
                self.app.config['MODEL_ARTIFACT_DIRECTORY'],
                version.artifact_path
            )
            self.assertTrue(os.path.isfile(artifact_path))
            self.assertEqual(version.model_status, 'Candidate')

        activated = self.client.post(
            f'/platform-models/activate-version/{version_id}',
            follow_redirects=True
        )
        self.assertIn(b'Loan Default Model v1.0 is now the active prediction model', activated.data)
        with self.app.app_context():
            active_setting = SystemSetting.query.filter_by(
                key='active_model_version',
                organization_id=None
            ).one()
            self.assertEqual(active_setting.value, 'Loan Default Model v1.0')

            evaluation = ModelEvaluation(
                model_name='Validated Test Classifier',
                accuracy=0.91,
                precision=0.88,
                recall=0.86,
                f1_score=0.87,
                roc_auc=0.94,
                pr_auc=0.82
            )
            db.session.add(evaluation)
            db.session.commit()
            evaluation_id = evaluation.id

        activated_evaluation = self.client.post(
            f'/platform-models/activate-evaluation/{evaluation_id}',
            follow_redirects=True
        )
        self.assertIn(b'Validated Test Classifier is now the active prediction model', activated_evaluation.data)
        with self.app.app_context():
            active_evaluation = db.session.get(ModelEvaluation, evaluation_id)
            self.assertEqual(active_evaluation.status, 'Active Production')
            self.assertEqual(
                SystemSetting.query.filter_by(key='active_model_version').one().value,
                'Validated Test Classifier'
            )

        invalid_upload = self.client.post('/platform-models/upload', data={
            'model_name': 'Unsupported Model',
            'version_number': 'v1',
            'model_file': (io.BytesIO(b'not a joblib file'), 'model.csv')
        }, content_type='multipart/form-data', follow_redirects=True)
        self.assertIn(b'Provide a model name', invalid_upload.data)

    def test_nonessential_company_support_routes_are_removed(self):
        for path in (
            '/contact',
            '/contact/status/1',
            '/organization/support',
            '/platform-support',
            '/pricing',
            '/organization/billing',
            '/audit-logs',
            '/reconciliation'
        ):
            self.assertEqual(self.client.get(path).status_code, 404)

    def test_registration_rejects_invalid_contact_and_identity_values(self):
        registration_data = {
            'org_name': 'Valid Finance',
            'org_type': 'Microfinance Institution',
            'org_email': 'info@valid.example',
            'org_phone': '+265999000000',
            'org_location': 'Lilongwe',
            'org_address': 'Area 3, Lilongwe',
            'admin_name': 'Valid Admin',
            'admin_email': 'admin@valid.example',
            'admin_phone': '+265888111111',
            'admin_national_id': '12345678',
            'admin_password': 'secure-password-123',
            'admin_confirm_password': 'secure-password-123'
        }
        invalid_values = (
            ('org_phone', '0999000000'),
            ('admin_phone', '+26512345'),
            ('admin_national_id', '12AB'),
            ('admin_national_id', '123456?789'),
            ('admin_email', 'not-an-email'),
            ('org_email', 'missing-domain')
        )

        for field, invalid_value in invalid_values:
            with self.subTest(field=field, value=invalid_value):
                data = dict(registration_data)
                data[field] = invalid_value
                response = self.client.post('/register-organization', data=data)
                self.assertEqual(response.status_code, 200)
                self.assertIn(b'Enter a valid', response.data)
                with self.app.app_context():
                    self.assertEqual(Organization.query.count(), 0)
                    self.assertEqual(User.query.count(), 0)

    def test_registration_accepts_longer_malawi_national_id(self):
        response = self.client.post('/register-organization', data={
            'org_name': 'National ID Finance',
            'org_type': 'Microfinance Institution',
            'org_email': 'info@nidfinance.example',
            'org_phone': '+265999000000',
            'org_location': 'Lilongwe',
            'org_address': 'Area 3, Lilongwe',
            'admin_name': 'National ID Admin',
            'admin_email': 'admin@nidfinance.example',
            'admin_phone': '+265888111111',
            'admin_national_id': '123456789012',
            'admin_password': 'secure-password-123',
            'admin_confirm_password': 'secure-password-123'
        }, follow_redirects=True)
        self.assertIn(b'Account request submitted', response.data)
        with self.app.app_context():
            administrator = User.query.one()
            self.assertEqual(administrator.national_id, '123456789012')

    def test_registration_accepts_custom_company_type(self):
        response = self.client.post('/register-organization', data={
            'org_name': 'Community Credit',
            'org_type': 'Other',
            'org_type_other': 'Community Credit Provider',
            'org_email': 'info@community.example',
            'org_phone': '+265999000000',
            'org_location': 'Lilongwe',
            'org_address': 'Area 3, Lilongwe',
            'admin_name': 'Community Admin',
            'admin_email': 'admin@community.example',
            'admin_phone': '+265888111111',
            'admin_national_id': '87654321',
            'admin_password': 'secure-password-123',
            'admin_confirm_password': 'secure-password-123'
        }, follow_redirects=True)
        self.assertIn(b'Account request submitted', response.data)
        with self.app.app_context():
            organization = Organization.query.one()
            self.assertEqual(organization.org_type, 'Community Credit Provider')

    def test_registration_requires_custom_company_type_when_other_selected(self):
        response = self.client.post('/register-organization', data={
            'org_name': 'Community Credit',
            'org_type': 'Other',
            'org_type_other': '',
            'org_email': 'info@community.example',
            'org_phone': '+265999000000',
            'org_location': 'Lilongwe',
            'org_address': 'Area 3, Lilongwe',
            'admin_name': 'Community Admin',
            'admin_email': 'admin@community.example',
            'admin_phone': '+265888111111',
            'admin_national_id': '87654321',
            'admin_password': 'secure-password-123',
            'admin_confirm_password': 'secure-password-123'
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Enter a valid company type', response.data)
        with self.app.app_context():
            self.assertEqual(Organization.query.count(), 0)

    def test_organization_staff_and_records_are_isolated(self):
        self.register_organization('First Finance', 'admin@first.example')
        with self.app.app_context():
            first = Organization.query.filter_by(name='First Finance').one()
            first_id = first.id

        self.create_platform_owner()
        self.approve_organization(first_id)
        self.client.get('/logout')
        self.client.post('/login', data={
            'email': 'admin@first.example',
            'password': 'secure-password-123'
        })
        created_staff = self.client.post('/users/new', data={
            'name': 'First Loan Officer',
            'email': 'staff@first.example',
            'role': 'Loan Officer',
            'password': 'staff-password-123',
            'confirm_password': 'staff-password-123'
        }, follow_redirects=True)
        self.assertIn(b'added successfully', created_staff.data)

        self.client.post('/applicants/new', data={
            'full_name': 'First Borrower',
            'phone_number': '+265 999 111 111',
            'email': 'borrower@first.example',
            'age': '34',
            'employment_status': 'Employed',
            'monthly_income': '500000',
            'employment_duration': '5',
            'residential_area': 'Lilongwe'
        }, follow_redirects=True)

        self.client.get('/logout')
        self.register_organization('Second Finance', 'admin@second.example')
        with self.app.app_context():
            second = Organization.query.filter_by(name='Second Finance').one()
            first_borrower = Applicant.query.filter_by(organization_id=first_id).one()
            application = LoanApplication(
                organization_id=first_id,
                app_id='FIRST-APP-1',
                applicant_id=first_borrower.id,
                loan_amount=100000,
                loan_purpose='Business',
                loan_term=12,
                interest_rate=12,
                monthly_income=500000,
                existing_monthly_debt=0,
                status='Pending'
            )
            db.session.add(application)
            db.session.commit()
            second_id = second.id
            application_id = application.id

        self.client.get('/logout')
        self.approve_organization(second_id)
        self.client.get('/logout')
        self.client.post('/login', data={
            'email': 'admin@second.example',
            'password': 'secure-password-123'
        })
        borrower_page = self.client.get('/applicants')
        self.assertNotIn(b'First Borrower', borrower_page.data)
        self.assertIn(b'Second Finance', borrower_page.data)
        cross_tenant_application = self.client.get(f'/predict/{application_id}')
        self.assertEqual(cross_tenant_application.status_code, 404)
        self.assertEqual(self.client.get('/data-upload').status_code, 404)

        self.client.get('/logout')
        staff_login = self.client.post('/login', data={
            'email': 'staff@first.example',
            'password': 'staff-password-123'
        }, follow_redirects=True)
        self.assertEqual(staff_login.status_code, 200)
        self.assertIn(b'First Finance', staff_login.data)
        self.assertEqual(self.client.get('/users').status_code, 403)
        self.assertEqual(self.client.get('/platform-admin').status_code, 404)
        self.assertEqual(self.client.get('/platform-activity').status_code, 404)
        self.assertEqual(self.client.get('/platform-activity/export').status_code, 404)
        self.assertEqual(self.client.post('/platform-activity/clear').status_code, 404)
        self.assertEqual(self.client.get('/platform-models').status_code, 404)
        self.assertEqual(self.client.post('/platform-models/upload').status_code, 404)
        self.assertEqual(
            self.client.post('/platform-models/activate-version/1').status_code,
            404
        )
        self.assertEqual(self.client.get('/platform-support').status_code, 404)
        self.assertEqual(self.client.post('/platform-support/1').status_code, 404)
        self.assertEqual(
            self.client.post(
                f'/platform-admin/organization/{first_id}/status',
                data={'status': 'Deactivated'}
            ).status_code,
            404
        )


if __name__ == '__main__':
    unittest.main()
