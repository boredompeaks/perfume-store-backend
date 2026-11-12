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
    Address,
    TOTPDevice,
)

# Raised only by the storefront serializer, so it lives beside its single raiser.
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
            # Same validate_password policy as reset. The account does not exist
            # yet, so build a transient user from the submitted attributes for
            # UserAttributeSimilarityValidator; errors go under `password`.
            candidate = User(
                username=attrs.get("username", ""), email=attrs.get("email", "")
            )
            try:
                validate_password(password, user=candidate)
            except ValidationError as error:
                raise serializers.ValidationError({"password": list(error.messages)})
        return attrs


class MFATokenObtainPairSerializer(TokenObtainPairSerializer):
    """Login serializer with mandatory TOTP for privileged roles.

    After ``super().validate()`` accepts the password, privileged users
    (superusers and ``staff.manage`` holders) must include a valid ``totp`` code,
    so no tokens are minted without the second factor and the existing rejection
    path audits the failure. A device this browser explicitly trusted may skip
    the challenge, but the code/replay path behind that guard is unchanged.
    """

    # Optional at the field level so non-privileged logins stay byte-compatible;
    # enforcement adds the requirement in validate().
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


class AddressSerializer(serializers.ModelSerializer):
    """Saved address, explicit fields and explicit caps.

    ``user`` is deliberately absent: the owner comes from the request, never from
    the body, and the declared ``max_length`` values keep absurd values a 400
    instead of a truncated column.
    """

    label = serializers.CharField(max_length=40, required=False, allow_blank=True)
    line1 = serializers.CharField(max_length=255)
    line2 = serializers.CharField(max_length=255, required=False, allow_blank=True)
    city = serializers.CharField(max_length=100)
    state = serializers.CharField(max_length=100, required=False, allow_blank=True)
    postal_code = serializers.CharField(max_length=20)
    country = serializers.CharField(max_length=100)
    phone = serializers.CharField(max_length=32, required=False, allow_blank=True)

    class Meta:
        model = Address
        fields = [
            "id",
            "label",
            "line1",
            "line2",
            "city",
            "state",
            "postal_code",
            "country",
            "phone",
            "is_default",
        ]
        read_only_fields = ["id"]


class StorefrontTokenObtainPairSerializer(TokenObtainPairSerializer):
    """Customer storefront login — no ``totp`` field at all.

    The storefront has no TOTP, and declaring only ``username``/``password``
    makes ``totp`` a non-field in both directions. A privileged account is
    refused here rather than authenticated, so the customer door cannot bypass
    the mandatory second factor; the answer reveals nothing a correct password
    has not already proved.
    """

    def validate(self, attrs):
        data = super().validate(attrs)
        if is_privileged(self.user):
            raise PermissionDenied(MFA_STAFF_LOGIN_REQUIRED)
        return data
