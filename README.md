# Loan Workspace

A Flask application for lending organizations. Each institution gets a separate workspace for staff accounts, borrowers, and loan applications. Organization access is enabled only after a platform operator approves its registration.

## Run locally

```powershell
python run.py
```

The application starts at `http://127.0.0.1:5000/`. Register an organization from the landing page. Its first administrator can sign in after approval; that administrator creates staff accounts from **Staff accounts**.

## Platform owner company management

The clean database has no default platform operator. On a trusted local terminal, create the first operator:

```powershell
flask --app run create-platform-owner
```

The command prompts for a name, email, and password and requires at least 12 password characters. Sign in with that account to view company registrations, approve or reject pending companies, activate/suspend/deactivate, edit or delete companies, and review platform-wide activity. Company Management shows the details of the administrator who registered each company and provides a direct email link to contact the company. Suspended and rejected companies appear in dedicated sections on Company Management, with actions to reactivate or approve them. The Platform Activity page shows full event details and supports deleting individual entries, exporting the full history as an Excel workbook, or clearing all activity. Model Management is available to the Platform Owner for registering Joblib model artifacts, selecting active model versions, monitoring evaluation metrics and prediction statistics, and reviewing prediction errors. Uploaded artifacts are stored but are not loaded or executed by the current prediction runtime; activation updates the model-version label recorded on new predictions. These operations are restricted to the Platform Owner; organization staff cannot access the platform management routes. Deleting a company permanently deletes its associated workspace records. Do not expose the operator account to organization staff.

Platform Owners can manage company questions and complaints from **Customer support**. Company users can submit requests and view replies or solutions from the **Support** page; they can also rate responses. Replies are saved in the portal and emailed when SMTP is configured. Set `SUPPORT_SMTP_HOST`, `SUPPORT_SMTP_PORT`, `SUPPORT_SMTP_USERNAME`, `SUPPORT_SMTP_PASSWORD`, and `SUPPORT_FROM_EMAIL` to enable email delivery. TLS is enabled by default; set `SUPPORT_SMTP_USE_TLS=false` only when your mail server does not support STARTTLS, or set `SUPPORT_SMTP_USE_SSL=true` for implicit TLS (typically port 465). Assignments are limited to active Platform Owner accounts.

## Database

The active database is `instance/loan_risk_v3_saas.sqlite`. The app creates its schema on startup and does not add demo organizations, users, borrowers, or applications. Set `LOAN_RISK_DATABASE_PATH` to use another SQLite file. Set `SECRET_KEY` to a private, random value before deployment.

Other SQLite files in `instance/` are not used by the current application.

## Credit history

Credit history is captured as a snapshot on each loan application: whether it was checked, prior loan counts and repayments, recent late payments, longest recent delay, and whether a previous default is known. “Not checked,” “No previous loans,” and “History available” are distinct states. These fields are displayed for underwriting review but do not currently affect the prediction score.

## One application per borrower

Each registered borrower may have only one loan application, including when the existing application was declined. The application form identifies borrowers who already have an application, and the server rejects repeat submissions. A database uniqueness constraint also protects databases without pre-existing duplicate applications; existing records are preserved during upgrade.

## WhatsApp approval notices

When an application first changes to **Approved**, the system sends a WhatsApp template message only if the borrower has explicitly opted in. A failed or unconfigured delivery does not undo the lending decision; its status appears on the underwriting page, where an authorized workspace user can retry it. Editing an already-approved decision does not send a duplicate notice. Existing borrowers must give consent from their borrower profile before a notice can be sent.

The current setup uses one shared WhatsApp Business sender for all organizations. Configure `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, and `WHATSAPP_GRAPH_API_VERSION` (for example `v23.0`) as server environment variables. Never commit the access token or paste it into source code.

Configure `WHATSAPP_APPROVAL_TEMPLATES` as a JSON array of Meta-approved templates. Each entry has a `name`, `language`, `label`, exact approved body text, and a `fields` array that maps numbered placeholders to values. Supported fields are `borrower_name`, `application_id`, `loan_amount`, `loan_purpose`, `loan_term`, `interest_rate`, and `organization_name`. Staff select from the configured templates in the underwriting page and see a loan-specific preview before sending. For example, a template body can be `Hello {{1}}, application {{2}} has been approved for {{3}}. Purpose: {{4}}. Loan term: {{5}} months. Interest rate: {{6}}%.` with fields `["borrower_name", "application_id", "loan_amount", "loan_purpose", "loan_term", "interest_rate"]`. The placeholders, order, and wording must match the Meta-approved template exactly. The older `WHATSAPP_APPROVAL_TEMPLATE_NAME` and `WHATSAPP_TEMPLATE_LANGUAGE` settings remain supported and use this six-value message format. A **Sent** status means Meta accepted the API request; this version does not track delivered or read receipts.

## Tests

```powershell
python -m unittest test_app.py
```
