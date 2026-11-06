import json

from django.contrib.auth.validators import UnicodeUsernameValidator
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from .dtos import (
    CreateGroupCommand,
    LoginCommand,
    MembershipCommand,
    PasswordCommand,
    PatchGroupCommand,
    PreferencesCommand,
    RegisterCommand,
)
from .models import User
from .policies import ACTIONS, PolicySchema


class StrictSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if isinstance(data, dict) and set(data) - set(self.fields):
            raise serializers.ValidationError({
                "non_field_errors": [_("Unknown fields are not allowed.")],
            })
        return super().to_internal_value(data)


class RegisterSerializer(StrictSerializer):
    username = serializers.CharField(max_length=150, validators=[UnicodeUsernameValidator()])
    password = serializers.CharField(write_only=True, trim_whitespace=False, max_length=1024)
    email = serializers.EmailField(required=False, allow_blank=True, default="", max_length=254)
    locale = serializers.ChoiceField(choices=User.Locale.choices, default="en")

    def command(self):
        return RegisterCommand(**self.validated_data)


class LoginSerializer(StrictSerializer):
    username = serializers.CharField(max_length=150)
    password = serializers.CharField(write_only=True, trim_whitespace=False, max_length=1024)

    def command(self):
        return LoginCommand(**self.validated_data)


class PasswordSerializer(StrictSerializer):
    current_password = serializers.CharField(write_only=True, trim_whitespace=False, max_length=1024)
    new_password = serializers.CharField(write_only=True, trim_whitespace=False, max_length=1024)

    def command(self):
        return PasswordCommand(**self.validated_data)


class PreferencesSerializer(StrictSerializer):
    locale = serializers.ChoiceField(choices=User.Locale.choices)

    def command(self):
        return PreferencesCommand(**self.validated_data)


class GroupFieldsSerializer(StrictSerializer):
    name = serializers.CharField(max_length=150)
    permissions = serializers.ListField(
        child=serializers.ChoiceField(choices=tuple(ACTIONS)), max_length=len(ACTIONS)
    )
    attributes = serializers.JSONField()
    reason = serializers.CharField(max_length=500)

    def validate_attributes(self, value):
        try:
            PolicySchema.validate(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc
        return value

    def validate_permissions(self, value):
        if len(set(value)) != len(value):
            raise serializers.ValidationError(_("Duplicate permissions are not allowed."))
        return value


class CreateGroupSerializer(GroupFieldsSerializer):
    def command(self):
        values = self.validated_data
        return CreateGroupCommand(values["name"], tuple(values["permissions"]),
                                  json.dumps(values["attributes"]), values["reason"])


class PatchGroupSerializer(GroupFieldsSerializer):
    name = serializers.CharField(max_length=150, required=False)
    permissions = serializers.ListField(
        child=serializers.ChoiceField(choices=tuple(ACTIONS)),
        max_length=len(ACTIONS), required=False,
    )
    attributes = serializers.JSONField(required=False)

    def validate(self, attrs):
        if not set(attrs) & {"name", "permissions", "attributes"}:
            raise serializers.ValidationError(_("Provide at least one change."))
        return attrs

    def command(self, group_id):
        values = self.validated_data
        return PatchGroupCommand(
            group_id, values["reason"], values.get("name"),
            tuple(values["permissions"]) if "permissions" in values else None,
            json.dumps(values["attributes"]) if "attributes" in values else None,
        )


class MembershipSerializer(StrictSerializer):
    group_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), max_length=100, required=False,
    )
    is_active = serializers.BooleanField(required=False)
    reason = serializers.CharField(max_length=500)

    def validate(self, attrs):
        if not set(attrs) & {"group_ids", "is_active"}:
            raise serializers.ValidationError(_("Provide at least one change."))
        if "group_ids" in attrs and len(set(attrs["group_ids"])) != len(attrs["group_ids"]):
            raise serializers.ValidationError(_("Duplicate groups are not allowed."))
        return attrs

    def command(self, user_id):
        values = self.validated_data
        return MembershipCommand(
            user_id, values["reason"],
            tuple(values["group_ids"]) if "group_ids" in values else None,
            values.get("is_active"),
        )
