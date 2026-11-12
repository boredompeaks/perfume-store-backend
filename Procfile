# Platform entry point; mirrors backend/Dockerfile CMD so deploys cannot drift.
# No `release:` line: migrations run only in scripts/release.sh.
# The platform runs this from the repo root, so `cd backend` puts config.wsgi
# on the path; $PORT is supplied by the platform.
web: cd backend && exec gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers 3 --access-logfile - --error-logfile -
