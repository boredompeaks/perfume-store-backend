"""Deployment checks for the two alerting channels that fail closed (AUD-006).

Registered from ``ops.apps.OpsConfig.ready``. A production-shaped boot with
neither an error-tracking DSN nor an alert recipient currently looks healthy;
these Error-level checks make ``manage.py check``, ``makemigrations`` and
``migrate`` exit non-zero instead. Commands that filter checks by tag, notably
``collectstatic`` (which runs only ``Tags.staticfiles``), do not evaluate these
untagged checks and therefore do not abort. Messages name the key and the
runbook section only - never a DSN, a DSN fragment or a recipient address. A
host may deliberately bypass them with ``SILENCED_SYSTEM_CHECKS`` or
``--skip-checks``; docs/deploy-runbook.md documents that decision.
"""

from django.conf import settings
from django.core.checks import Error, register

from .alerts import _recipients

# The app's two non-deployed environment names (the same allowlist seed_demo
# refuses outside of). Kept local: importing a management command from an app
# config would drag its model imports into every boot, and common/ is outside
# this task's scope.
_NON_PRODUCTION_ENVIRONMENTS = frozenset({"local", "ci"})


def _production_shaped() -> bool:
    """A real deployment is a non-debug boot that names an environment outside
    the local/ci allowlist; both signals are the app's existing boot guards."""
    return (
        not settings.DEBUG and settings.DJANGO_ENV not in _NON_PRODUCTION_ENVIRONMENTS
    )


@register()
def _alerting_configuration_check(app_configs, **kwargs):
    """One Error per silent channel when the boot is production-shaped."""
    if not _production_shaped():
        return []

    errors = []
    if not settings.SENTRY_DSN.strip():
        errors.append(
            Error(
                "SENTRY_DSN is unset in a production-shaped deployment, so "
                "error events never leave this process. Set SENTRY_DSN to the "
                "project's ingest DSN (docs/deploy-runbook.md, 'Error tracking "
                "and uptime').",
                hint=(
                    "Then run 'python manage.py sentry_send_probe --send' and "
                    "confirm the probe event in the Sentry project."
                ),
                id="ops.E001",
            )
        )
    if not _recipients():
        errors.append(
            Error(
                "ALERT_RECIPIENTS resolves to no addresses in a "
                "production-shaped deployment, so every admin alert is "
                "silently disabled. Set ALERT_RECIPIENTS to your staff "
                "mailboxes (docs/deploy-runbook.md, 'Email delivery "
                "verification').",
                hint=(
                    "Then run 'python manage.py email_send_probe --send' and "
                    "confirm the mail arrives."
                ),
                id="ops.E002",
            )
        )
    return errors
