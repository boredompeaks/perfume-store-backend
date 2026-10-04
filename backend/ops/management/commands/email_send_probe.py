"""SPEC-22-10 [R-22.18]: prove mail actually LEAVES, not merely that SMTP is set.

`/health/`'s `smtp_configured` check (`ops/services.py`) proves three env keys
are non-empty. That is configuration, not delivery, and it cannot tell an
operator the difference between "the app can attempt a send" and "every
customer's password-reset mail is landing in a spam folder" - the failure this
command exists to rule out.

Gates, in the order they apply. Every one refuses by name rather than guesses:

* **An allowlisted recipient only.** The recipient must already be in
  `settings.ALERT_RECIPIENTS` - the operator's own staff mailboxes, which
  already receive every admin alert. There is no way to aim this at a customer
  address, and the command never reads a `User.email`, so it cannot leak
  another person's address because it never looks at one. An empty allowlist
  is refused rather than defaulted to some address.
* **Dry run by default.** Nothing is delivered unless `--send` is passed, so
  the safe answer to "what would this do?" is the answer you get.
* **A backend that never speaks SMTP is refused.** console/locmem/dummy accept
  the message and report success, so probing against them would report a
  delivery that never happened - the false green this command must not be.
  The test suite runs on the locmem backend, which is why the suite can never
  send real mail through this command even by accident.
* **One message, deployment facts only.** The body carries the environment,
  host, port, sender and timestamp - no order, no username, no product data.

The send goes through `common.notifications.send_email` (SPEC-19-1's single
send path), so the sender address and the backend are the ones the app really
uses: a probe that used a private code path would be able to pass while the
app's own mail failed.

Exit status: 0 for a dry run and for an accepted send; non-zero (`CommandError`)
for every refusal, and for a send the server rejected - a refused delivery is a
delivery failure and has to be loud.

See `docs/deploy-runbook.md` ("Email delivery verification") for the operator
procedure, the SPF/DKIM/DMARC setup and what to check when mail lands in spam.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from common import notifications

# Backends that accept a message and report success without a single SMTP
# conversation. A probe against one of these is a false green, so it is refused.
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
    """`settings.ALERT_RECIPIENTS` as an ordered, de-duplicated address list.

    Parsed here rather than imported from `ops.alerts` so the command states
    its own contract: the allowlist is the deployment's own staff mailboxes,
    and a customer address cannot be in it by accident.
    """
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
                # The server's own text is the actionable part for an operator
                # (535 auth refused, 550 relay denied, connection refused), and
                # neither smtplib nor Django ever puts the configured
                # credential in it - so it is safe to surface. Exit non-zero: a
                # rejected probe IS a delivery failure, not a warning.
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
        # The allowlist is the whole safety property here: an operator naming
        # anything else is a typo (or an attempt), and both are refused.
        raise CommandError(
            f"refusing: {requested.strip()!r} is not in ALERT_RECIPIENTS, so "
            "this command will not send to it. The probe only ever addresses "
            "the deployment's own staff mailboxes - never a customer's "
            "address."
        )
