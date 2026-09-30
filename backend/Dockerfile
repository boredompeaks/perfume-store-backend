# SPEC-2-10a: production image for the Django backend.
#
# Build with backend/ as the build context (the sibling .dockerignore is
# honoured exactly when the context is backend/):
#   docker build -t perfume-backend backend
# Run (all configuration is environment-driven; no secret is baked in):
#   docker run -p 8000:8000 -e DJANGO_SECRET_KEY=... -e DATABASE_URL=... \
#     -e DJANGO_ALLOWED_HOSTS=... perfume-backend
#
# Orchestration (compose/Procfile/deploy workflow) is SPEC-2-10b.

FROM python:3.14-slim

# Unbuffered output so the platform's log collector sees logs line by line,
# and no .pyc files written into the app's read-mostly layers.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first: this layer is cached until requirements.txt changes,
# so ordinary source edits do not re-run pip. No compiler in this image, so
# psycopg[binary]'s prebuilt libpq wheel and Pillow's wheel are the ones used.
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . ./

# Assets are gathered once at build time and shipped inside the image, where
# whitenoise serves them from the app process (SPEC-22-01) - so the image
# needs no separate web server or object store. The env below is
# build-time only and exists so the DEBUG=false settings guard clears; it is
# a fixed non-secret and is not the key the container runs with (the runtime
# key comes from DJANGO_SECRET_KEY in the environment).
RUN DJANGO_DEBUG=false \
    DJANGO_SECRET_KEY=collectstatic-build-only-not-a-real-secret \
    DJANGO_ALLOWED_HOSTS=localhost \
    DJANGO_STATIC_ROOT=/app/staticfiles \
    python manage.py collectstatic --noinput

# Non-root runtime: the app never needs write access to the code or the
# collected assets, and an upload volume can be mounted at /app/media owned
# by this user.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/media \
    && chown -R appuser:appuser /app/media /app/staticfiles
USER appuser

# Default port 8000 for local runs; platforms that inject $PORT (Render,
# Heroku-style) are served on the port they ask for because the CMD expands
# it at runtime.
ENV PORT=8000
EXPOSE 8000

# Shell form so $PORT is expanded by the shell at container start. --bind
# 0.0.0.0 is what makes the port reachable from outside the container;
# --access-logfile - keeps access logs on stdout with the app logs.
CMD ["sh", "-c", "exec gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers 3 --access-logfile - --error-logfile -"]