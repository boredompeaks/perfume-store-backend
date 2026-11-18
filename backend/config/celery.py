"""The Celery app for the scheduled sweeps and outbox drain.

Configuration stays env-driven and is resolved lazily, so importing this module opens
no broker connection.
"""

import os

from celery import Celery

# setdefault, not assignment, so an explicit DJANGO_SETTINGS_MODULE override survives.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

# The app name makes `celery -A config` resolve through `config/__init__.py`'s re-export.
app = Celery("config")

# Passing the settings module keeps configuration lazy (no broker at import), and the
# namespace maps CELERY_* keys to celery's own names.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Autodiscovery is lazy: task modules import at worker boot, not on every management
# command, and an import error surfaces there rather than mid-migration.
app.autodiscover_tasks()
