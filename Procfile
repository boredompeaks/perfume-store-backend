# SPEC-2-10b: the platform entry point, mirroring the container exactly.
#
# gunicorn against the same WSGI app, bound the same way, with the same worker
# count and the same stdout/stderr logging flags as the CMD in
# backend/Dockerfile - so a platform deploy, a bare-host run and the container
# all serve traffic through one command and cannot drift apart.
#
# There is NO `release:` line here. Release-time migrations run in exactly one
# place, scripts/release.sh (SPEC-22-07); a second migrate path in this file
# would be a second thing to keep correct.
#
# Platform Procfiles run commands through a shell from the app root, so the
# `cd backend` is what puts config.wsgi on the path (the repository is a
# monorepo: the Django project lives in backend/). $PORT is supplied by the
# platform and defaults to the image's own default.
web: cd backend && exec gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers 3 --access-logfile - --error-logfile -
