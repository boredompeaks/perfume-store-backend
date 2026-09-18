from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from rest_framework import serializers


class RegisterSerializer(serializers.ModelSerializer):

    # Strength lives in AUTH_PASSWORD_VALIDATORS via validate_password (below) —
    # the same policy the reset path enforces, instead of a bare min_length.
    password = serializers.CharField(write_only=True)

    class Meta:
        model = User
        fields = [
            'username',
            'email',
            'password',
        ]

    def create(self, validated_data):

        user = User.objects.create_user(
            username=validated_data['username'],
            email=validated_data['email'],
            password=validated_data['password'],
            is_active=False,
        )

        return user

    def validate_username(self, value):
        if User.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError('This username is already taken.')
        return value

    def validate_email(self, value):
        if not value:
            raise serializers.ValidationError('Email is required.')
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError('An account already uses this email address.')
        return value

    def validate(self, attrs):
        password = attrs.get('password')
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
                username=attrs.get('username', ''), email=attrs.get('email', '')
            )
            try:
                validate_password(password, user=candidate)
            except ValidationError as error:
                raise serializers.ValidationError({'password': list(error.messages)})
        return attrs
