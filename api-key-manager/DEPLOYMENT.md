# PhiXtra Portal — Phase 1 Deployment Guide

## What this is
Replacement for the existing portal at portal.phixtra.com.
The /chat endpoint, keys.phixtra.com (app.py), and all AI backend code are UNTOUCHED.

## Files to REPLACE on server
Copy these files into your existing api-key-manager folder:
- portal_app.py            (replaces existing)
- portal_routes.py         (replaces existing)
- portal_admin_routes.py   (replaces existing)
- portal_migrations.py     (replaces existing)

## Files to COPY (new — do not overwrite anything)
- templates/portal/base.html              (new base — replaces portal_base.html)
- templates/portal/admin_base.html        (new)
- templates/portal/home.html              (replaces)
- templates/portal/register.html          (replaces)
- templates/portal/login.html             (replaces)
- templates/portal/forgot.html            (replaces)
- templates/portal/reset.html             (replaces)
- templates/portal/dashboard.html         (replaces)
- templates/portal/onboarding.html        (NEW)
- templates/portal/api_keys.html          (NEW)
- templates/portal/billing.html           (replaces)
- templates/portal/invoices.html          (replaces)
- templates/portal/admin_login.html       (replaces)
- templates/portal/admin_base.html        (NEW)
- templates/portal/admin_customers.html   (replaces)
- templates/portal/admin_customer_detail.html (NEW)
- templates/portal/admin_api_keys.html    (NEW)
- templates/portal/admin_invoices.html    (replaces)
- templates/portal/admin_packages.html    (replaces)

## Files NOT TOUCHED (leave exactly as they are)
- app.py             keys.phixtra.com admin tool — DO NOT TOUCH
- db.py              database connection — DO NOT TOUCH
- invoice_pdf.py     PDF generator — DO NOT TOUCH
- portal_utils.py    email + utilities — DO NOT TOUCH
- trial_jobs.py      trial expiry cron — DO NOT TOUCH
- auth.py            /chat auth — DO NOT TOUCH
- main.py            /chat endpoint — DO NOT TOUCH

## Database changes (automatic on restart)
portal_migrations.py adds these columns to the customers table if missing:
- first_name VARCHAR(100)
- last_name  VARCHAR(100)
- phone_number VARCHAR(30)
- phone_verified TINYINT(1) DEFAULT 0

Also adds new table: onboarding_state
All changes are idempotent — safe to run multiple times.

## .env variables needed (add if missing)
PORTAL_SECRET_KEY=<generate a long random string>
SMTP_HOST=smtp.zeptomail.eu
SMTP_PORT=587
SMTP_USER=emailapikey
SMTP_PASSWORD=<your ZeptoMail API token>
SMTP_FROM=noreply@phixtra.com
STRIPE_SECRET_KEY=<your Stripe secret key>
STRIPE_WEBHOOK_SECRET=<from Stripe dashboard>
INVOICE_DIR=/root/api-key-manager/invoices

## Restart command
sudo systemctl restart portal   (or however you run gunicorn/uwsgi)
