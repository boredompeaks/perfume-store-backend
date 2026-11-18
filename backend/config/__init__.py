"""Re-export the Celery app so ``celery -A config`` finds it; the eager import opens no socket."""

from config.celery import app as celery_app

__all__ = ("celery_app",)
