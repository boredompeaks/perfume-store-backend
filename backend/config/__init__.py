"""Celery app, so `celery -A config` and `import config` find it.

The standard Django wiring for a Celery app: the app is built in
``config.celery`` and re-exported from the package's ``__init__``, which is
what celery's ``-A config`` argument looks in. Without this line a worker would
have to be started as `celery -A config.celery`, and every other Celery
integration - a WSGI/ASGI entrypoint importing the app, a test importing it -
would have to know that the underscored name is the real one.

The import is deliberately the ONLY thing this package's ``__init__`` does.
It is eager, which is the point (celery's own docs prescribe it), and it is
safe: ``config.celery`` builds the app from ``config.settings`` LAZILY and
opens no socket, so importing the Django settings, importing every model, or
running the test suite never reaches for a broker.
"""

from config.celery import app as celery_app

__all__ = ("celery_app",)
