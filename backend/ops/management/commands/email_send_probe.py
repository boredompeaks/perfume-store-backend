"""Send (or dry-run) one delivery-probe email to an allowlisted operator address, to prove real mail leaves this deployment.

Only ever addresses `settings.ALERT_RECIPIENTS`, dry-runs by default, and refuses a non-SMTP backend; a rejected send exits non-zero.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from common import notifications

# Backends that report success without a single SMTP conversation; probing one
# is a false green, so it is refused.
NON_DELIVERING_BACKENDS = frozenset(
    {
        "django.core.mail.backends.console.EmailBackend",
        "django.core.mail.backends.dummy.EmailBackend",
        "django.core.mail.backends.locmem.EmailBackend",
    }
)

PROBE_TEMPLATE = "email_probe"
PROBE_SUBJECT = "Perfume Store delivery probe"


def allowlisted_recipients():
    """`settings.ALERT_RECIPIENTS` as an ordered, de-duplicated address list."""
    recipients = []
    for raw in settings.ALERT_RECIPIENTS.split(","):
        address = raw.strip()
        if address and address not in recipients:
            recipients.append(address)
    return recipients


class Command(BaseCommand):
    help = (
        "Send (or dry-run) one delivery-probe email to an allowlisted operator "
        "address, to prove real mail leaves this deployment. Dry run by default."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--send",
            action="store_true",
            help="Actually deliver. Without this flag nothing is sent.",
        )
        parser.add_argument(
            "--recipient",
            help=(
                "One ALERT_RECIPIENTS entry to probe. Default: every allowlisted entry."
            ),
        )

    def handle(self, *args, **options):
        allowlist = allowlisted_recipients()
        if not allowlist:
            raise CommandError(
                "no probe recipient is allowlisted: set ALERT_RECIPIENTS to "
                "your own staff mailboxes first (.env.example documents the "
                "key). This command will not send to any other address."
            )
        recipients = self._recipients(options["recipient"], allowlist)

        backend = settings.EMAIL_BACKEND
        if backend in NON_DELIVERING_BACKENDS:
            raise CommandError(
                f"refusing to probe: EMAIL_BACKEND={backend} never speaks "
                "SMTP, so a 'sent' result would report a delivery that never "
                "happened. Point EMAIL_BACKEND at the SMTP backend first."
            )

        self.stdout.write(
            "\n".join(
                [
                    f"environment: {settings.DJANGO_ENV}",
                    f"backend:     {backend}",
                    f"server:      {settings.EMAIL_HOST}:{settings.EMAIL_PORT}"
                    f" (tls={settings.EMAIL_USE_TLS})",
                    f"from:        {settings.DEFAULT_FROM_EMAIL}",
                    "recipients:  " + ", ".join(recipients),
                ]
            )
        )
        if not options["send"]:
            self.stdout.write(
                "dry run: nothing was sent. Re-run with --send to deliver."
            )
            return

        context = {
            "environment": settings.DJANGO_ENV,
            "server": f"{settings.EMAIL_HOST}:{settings.EMAIL_PORT}",
            "from_address": settings.DEFAULT_FROM_EMAIL,
            "sent_at": timezone.now(),
        }
        for address in recipients:
            try:
                notifications.send_email(
                    PROBE_TEMPLATE, context, PROBE_SUBJECT, address
                )
            except Exception as exc:
                # The server's text (535 auth refused, 550 relay denied,
                # connection refused) is the actionable part and never carries
                # the configured credential, so it is safe to surface. Exit
                # non-zero: a rejected probe IS a delivery failure.
                raise CommandError(
                    f"probe delivery to {address} failed: {type(exc).__name__}: {exc}"
                )
        self.stdout.write(
            f"probe accepted by the server for {len(recipients)} recipient(s). "
            "Accepted is not delivered: check each recipient's inbox AND spam "
            "folder, then the SPF/DKIM/DMARC section of docs/deploy-runbook.md."
        )

    def _recipients(self, requested, allowlist):
        """The whole allowlist, or the one allowlisted entry the operator named."""
        if not requested:
            return allowlist
        wanted = requested.strip().lower()
        for address in allowlist:
            if address.lower() == wanted:
                return [address]
        # The allowlist is the whole safety property: anything else is a typo
        # (or an attempt), and both are refused.
        raise CommandError(
            f"refusing: {requested.strip()!r} is not in ALERT_RECIPIENTS, so "
            "this command will not send to it. The probe only ever addresses "
            "the deployment's own staff mailboxes - never a customer's "
            "address."
        )
