# Production image for the Django backend; build with backend/ as the context
# so the sibling .dockerignore applies: docker build -t perfume-backend backend
# Multi-stage so the runtime image carries installed packages only, no build
# residue. All configuration is environment-driven; no secret is baked in.

FROM python:3.14-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

# Dependencies first, so ordinary source edits do not re-run pip. --prefix
# installs into a private tree copied over /usr/local (where the base image's
# interpreter lives), keeping shebangs and site-packages paths valid.
# requirements.txt only points at requirements.lock (resolved relative to the
# file), so the lock must be copied too; omitting it fails the build loudly
# rather than installing unpinned.
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependency tree from the builder, on the interpreter location it was
# installed for; pip's upgrade and any build tooling stay behind.
COPY --from=builder /install /usr/local

COPY . ./

# Assets are gathered at build time and served by whitenoise from the app
# process, so the image needs no separate web server or object store.
#
# The env below is BUILD-TIME ONLY: these are non-debug boot guards, so
# collectstatic cannot run without them, and none is a real credential or
# database (DJANGO_ENV=ci, a placeholder secret, sqlite under /tmp that nothing
# reads, and localhost hosts/origins for a layer that serves no request). The
# runtime env is injected by compose/platform. backend/tests/
# test_deployment_contract.py pins this env by booting the app from it.
RUN DJANGO_DEBUG=false \
    DJANGO_ENV=ci \
    DJANGO_SECRET_KEY=collectstatic-build-only-not-a-real-secret \
    DJANGO_ALLOWED_HOSTS=localhost \
    CSRF_TRUSTED_ORIGINS=http://localhost:3000 \
    DATABASE_URL=sqlite:////tmp/build.sqlite3 \
    DJANGO_STATIC_ROOT=/app/staticfiles \
    python manage.py collectstatic --noinput

# Non-root runtime; only /app/media needs write access (upload volume).
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/media \
    && chown -R appuser:appuser /app/media /app/staticfiles
USER appuser

# Default port; platforms that inject $PORT are served on it via the CMD.
ENV PORT=8000
EXPOSE 8000

# Probe the app's own /health/ (DB + media mount; 503 while degraded), so a
# degraded storefront reports unhealthy, not merely "process is up".
# Host is the first ALLOWED_HOSTS entry (an unlisted Host is a 400) and
# X-Forwarded-Proto keeps SECURE_SSL_REDIRECT from bouncing the loopback probe
# to an https:// the container does not serve. docker-compose.yml defines its
# own healthcheck with its own timings.
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import os, sys, urllib.request; \
host = (os.environ.get('DJANGO_ALLOWED_HOSTS') or '127.0.0.1').split(',')[0].strip(); \
req = urllib.request.Request('http://127.0.0.1:' + os.environ.get('PORT', '8000') + '/health/', headers={'Host': host, 'X-Forwarded-Proto': 'https'}); \
sys.exit(0 if urllib.request.urlopen(req, timeout=5).status == 200 else 1)"

# Shell form so $PORT expands at container start; --bind 0.0.0.0 makes the
# port reachable outside, and --access-logfile - keeps logs on stdout.
CMD ["sh", "-c", "exec gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers 3 --access-logfile - --error-logfile -"]
