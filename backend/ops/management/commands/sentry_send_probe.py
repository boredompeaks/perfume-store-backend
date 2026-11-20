"""Send (or dry-run) one error event to the configured Sentry project, to prove error reporting leaves this deployment.

Reads `settings.SENTRY_DSN` but never prints it; a missing or refused DSN and a
raising transport exit non-zero with a CommandError. Dry run by default.
"""

import sys

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from common import sentry as sentry_support

PROBE_MESSAGE = "Sentry delivery probe: handled exception"


def _capture_probe_event() -> bool:
    """Capture one handled exception through the client's transport.

    The scope-level capture swallows transport exceptions (it reports them as
    internal errors), so the probe calls the client directly: a raising
    transport must fail this command instead of disappearing into a log
    (T-20261118-010).
    """
    import sentry_sdk
    from sentry_sdk.utils import event_from_exception

    try:
        raise RuntimeError(PROBE_MESSAGE)
    except RuntimeError:
        event, hint = event_from_exception(sys.exc_info())
        return sentry_sdk.get_client().capture_event(event, hint) is not None


class Command(BaseCommand):
    help = (
        "Send (or dry-run) one error event to the configured Sentry project, "
        "to prove error reporting leaves this deployment. Dry run by default."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--send",
            action="store_true",
            help="Actually emit the event. Without this flag nothing is sent.",
        )

    def handle(self, *args, **options):
        dsn = (settings.SENTRY_DSN or "").strip()
        if not dsn:
            raise CommandError(
                "SENTRY_DSN is empty, so no error event can leave this "
                "process: set SENTRY_DSN to the project's ingest DSN and "
                "re-run. This command never prints the value."
            )

        self.stdout.write(
            "\n".join(
                [
                    f"environment: {settings.SENTRY_ENVIRONMENT}",
                    f"release:     {settings.SENTRY_RELEASE or '<unset>'}",
                    f"sample rate: {settings.SENTRY_SAMPLE_RATE}",
                    f"traces rate: {settings.SENTRY_TRACES_SAMPLE_RATE}",
                    "dsn:         configured (value never printed)",
                ]
            )
        )
        if not options["send"]:
            self.stdout.write(
                "dry run: nothing was sent. Re-run with --send to emit one probe event."
            )
            return

        live = sentry_support.init_sentry(
            dsn=dsn,
            environment=settings.SENTRY_ENVIRONMENT,
            release=settings.SENTRY_RELEASE,
            sample_rate=settings.SENTRY_SAMPLE_RATE,
            traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
        )
        if not live:
            raise CommandError(
                "SENTRY_DSN is set but the sentry-sdk refused it: check the "
                "DSN value and that the sentry-sdk package is installed. The "
                "value was not printed."
            )

        import sentry_sdk

        try:
            accepted = _capture_probe_event()
            sentry_sdk.flush(timeout=5)
        except Exception as exc:
            # Never echo the transport text: an ingest URL can carry the DSN.
            raise CommandError(
                "Sentry probe delivery failed: the configured transport "
                f"raised {type(exc).__name__}. Check SENTRY_DSN and the "
                "network path to the ingest host; the DSN was not printed."
            ) from None
        if not accepted:
            raise CommandError(
                "Sentry probe delivery failed: the SDK accepted no event. "
                "Check SENTRY_DSN and SENTRY_SAMPLE_RATE; the DSN was not "
                "printed."
            )
        self.stdout.write(
            "one probe event accepted by the SDK transport. Accepted is not "
            "delivered: confirm it in the Sentry project's Issues stream."
        )
