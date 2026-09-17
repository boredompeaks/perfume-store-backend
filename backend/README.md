# Perfume Store — Backend

Django 6.1 / Django REST Framework e-commerce backend (products, session cart, orders with coupons, Razorpay payments, JWT auth with email verification).

## Quick start

```bash
python -m venv venv
venv\Scripts\pip install -r requirements.txt
copy .env.example .env   # then fill in values
venv\Scripts\python manage.py migrate
venv\Scripts\python manage.py runserver
```

## Environment variables

See `.env.example`. Required in production: `DJANGO_SECRET_KEY`, `DJANGO_DEBUG=false`, `DJANGO_ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS`, `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, SMTP vars, `FRONTEND_URL`.

## Project layout

This repository is a **monorepo**:

```
backend/    Django project (this app)
  config/     settings, urls, wsgi/asgi
  accounts/   JWT auth, registration, email verification, password reset
  products/   Product catalog (CRUD, search, filter, pagination)
  cart/       Anonymous session-based cart
  orders/     Checkout, coupons, Razorpay payment creation/verification
  docs/       Audit, architecture, conventions, fix plan, changelog
  requirements.txt
frontend/   Next.js 16 storefront (see frontend/PLAN.md)
BACKEND_REQUESTS.md   cross-repo backend gaps logged by the frontend team
```

## API map

| Prefix | Endpoints |
|---|---|
| `api/accounts/` | register, login (JWT), token/refresh, verify-email, resend-verification, username-available, forgot-username, password-reset, password-reset/confirm |
| `api/products/` | list (search/category/price/ordering/pagination), detail by slug |
| `api/cart/` | get/add items (session), item patch/delete |
| `api/orders/` | list (JWT), checkout, apply-coupon, payment, payment/verify |

## Docs

- `docs/architecture.md` — system design and data flow
- `docs/conventions.md` — coding standards for contributions
- `docs/audit.md` — full technical audit with runtime verification results
- `docs/vulnerabilities.md` — flagged security findings with severity
- `docs/test-gaps.md` — missing unit/e2e coverage, flagged per endpoint
- `docs/fix-plan.md` — phase-by-phase remediation plan
- `docs/changes.md` — running changelog
