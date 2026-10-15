from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from common import totp
from common.permissions import is_privileged

from . import mfa_trust
from .models import (
    MFA_CODE_INVALID,
    MFA_CODE_REQUIRED,
    MFA_ENROLLMENT_REQUIRED,
    TOTPDevice,
)

# SPEC-20-10: raised by the storefront serializer only, so it lives beside
# its single raiser rather than in accounts.models with the login/admin
# messages (that module is the shared R-17.9 wording and stays untouched).
MFA_STAFF_LOGIN_REQUIRED = (
    "This is a staff account. Staff sign in on the staff sign-in page, "
    "where multi-factor authentication is part of the login."
)


class RegisterSerializer(serializers.ModelSerializer):
    # Strength lives in AUTH_PASSWORD_VALIDATORS via validate_password (below) —
    # the same policy the reset path enforces, instead of a bare min_length.
    password = serializers.CharField(write_only=True)

    class Meta:
        model = User
        fields = [
            "username",
            "email",
            "password",
        ]

    def create(self, validated_data):

        user = User.objects.create_user(
            username=validated_data["username"],
            email=validated_data["email"],
            password=validated_data["password"],
            is_active=False,
        )

        return user

    def validate_username(self, value):
        if User.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError("This username is already taken.")
        return value

    def validate_email(self, value):
        if not value:
            raise serializers.ValidationError("Email is required.")
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError(
                "An account already uses this email address."
            )
        return value

    def validate(self, attrs):
        password = attrs.get("password")
        if password:
            # Conventions.md: registration and reset must run the same
            # validate_password policy. The reset path validates against the
            # target user; here the account does not exist yet, so build a
            # transient one from the submitted attributes — that is what makes
            # UserAttributeSimilarityValidator see the username/email being
            # registered. Errors surface under the `password` key as a list of
            # messages, the exact field-error shape the reset endpoint and the
            # frontend's fieldErrors renderer already handle.
            candidate = User(
                username=attrs.get("username", ""), email=attrs.get("email", "")
            )
            try:
                validate_password(password, user=candidate)
            except ValidationError as error:
                raise serializers.ValidationError({"password": list(error.messages)})
        return attrs


class MFATokenObtainPairSerializer(TokenObtainPairSerializer):
    """Login serializer with mandatory TOTP for privileged roles (SPEC-17-05).

    BACKEND_REQUESTS.md defines no MFA contract, so the simpler
    single-request shape is used and declared: privileged users
    (:func:`common.permissions.is_privileged` — superusers and
    ``staff.manage`` holders) must include a valid ``totp`` code in the
    login body; everyone else is untouched. Failures raise after
    ``super().validate()`` has accepted the password, so LoginView's
    existing rejection path audits them as AUTH_LOGIN_FAILED and no tokens
    are ever minted without the second factor. Rollout: a privileged user
    with no confirmed device is blocked at login (mandatory means
    mandatory) with the enrollment path named in the message.

    SPEC-20-8: the ONLY change to the rule above is the guard around it —
    a device the user explicitly trusted, whose TTL has not lapsed, and that
    this very request is coming from, skips the challenge. Everything inside
    that guard (require a code, verify, consume the replay counter) is
    byte-for-byte the pre-SPEC-20-8 code path, so an untrusted device cannot
    be made to skip a code, and trust can never make a code replayable.
    """

    # Optional at the field level so non-privileged logins stay
    # byte-compatible; enforcement adds the requirement in validate().
    totp = serializers.CharField(required=False)

    def validate(self, attrs):
        data = super().validate(attrs)
        if is_privileged(self.user):
            device = TOTPDevice.active_for(self.user)
            if device is None:
                raise serializers.ValidationError({"totp": [MFA_ENROLLMENT_REQUIRED]})
            if not mfa_trust.is_trusted_device(
                self.context["request"], self.user, device
            ):
                code = attrs.get("totp")
                if not code:
                    raise serializers.ValidationError({"totp": [MFA_CODE_REQUIRED]})
                counter = totp.verify_code(
                    device.secret,
                    code,
                    at_time=totp.now(),
                    last_used_counter=device.last_used_counter,
                )
                if counter is None:
                    raise serializers.ValidationError({"totp": [MFA_CODE_INVALID]})
                # RFC 6238 §5.2: the consumed counter is the replay watermark.
                device.last_used_counter = counter
                device.save(update_fields=["last_used_counter"])
        return data


class StorefrontTokenObtainPairSerializer(TokenObtainPairSerializer):
    """Customer storefront login — no ``totp`` field at all (SPEC-20-10).

    The storefront has no TOTP: R-17.9 binds privileged roles only, so a
    customer was being shown, and could post, an authentication-code field
    that no server check ever read. This door declares the parent's explicit
    ``username``/``password`` and nothing more, which makes ``totp`` a
    non-field in both directions: the storefront form cannot render it, and
    a body that includes it is ignored rather than half-honoured.

    A privileged account is REFUSED here instead of being authenticated.
    This serializer has no factor to demand, so letting one through would
    turn the customer door into a way around the very rule that made it
    necessary — the caller is pointed at the door that does enforce R-17.9.
    The answer reveals nothing the caller has not already proven with a
    correct password.
    """

    def validate(self, attrs):
        data = super().validate(attrs)
        if is_privileged(self.user):
            raise PermissionDenied(MFA_STAFF_LOGIN_REQUIRED)
        return data
