from django.apps import AppConfig


class OpsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "ops"
    verbose_name = "Store operations"

    def ready(self):
        # Import is the registration: ops.checks decorates its Error checks with
        # django.core.checks.register when this module loads (task T-20261119-001).
        from . import checks  # noqa: F401
