"""``manage.py totp_code <username>``: the current code for a local login.

LOCAL TEST HELPER ONLY. Mandatory MFA (R-17.9) makes the second factor a
precondition of every privileged login, including the owner's E2E walk
through ``/admin/``. This command reads the already-enrolled device's secret
and prints the RFC 6238 code valid at ``common.totp.now()`` so that walk can
be driven without a phone. It reuses the production TOTP implementation
(no second algorithm) and deliberately prints neither the secret nor the
otpauth URI -- the factor itself stays the authority; this only tells a
human what the factor currently computes.

It must never run against a live store: printing a live staff code is
equivalent to handing over the second factor. The output is labelled as a
test helper for that reason.
"""

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from accounts.models import TOTPDevice
from common import totp


class Command(BaseCommand):
    help = (
        "Print the current 6-digit TOTP code for a user's active device. "
        "LOCAL TEST HELPER ONLY -- never logs the device secret."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "username",
            help="The user whose confirmed, enabled TOTP device should be read.",
        )

    def handle(self, *args, **options):
        username = options["username"]
        user = User.objects.filter(username=username).first()
        if user is None:
            raise CommandError(f"[totp_code] no user named {username!r}.")
        device = TOTPDevice.active_for(user)
        if device is None:
            raise CommandError(
                f"[totp_code] {username!r} has no confirmed, enabled TOTP "
                "device; enroll one first (the local superadmin gets one from "
                "`manage.py seed_e2e`)."
            )
        at_time = totp.now()
        code = totp.totp(device.secret, at_time)
        self.stdout.write(
            "[totp_code] LOCAL TEST HELPER -- never run against a live store; "
            "the device secret is not printed"
        )
        self.stdout.write(f"  user: {username}")
        self.stdout.write(f"  code: {code}")
        self.stdout.write(
            f"  valid for: {totp.STEP - (at_time % totp.STEP)}s "
            f"(of the {totp.STEP}s step)"
        )
