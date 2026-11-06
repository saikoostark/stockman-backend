from django.utils.translation import gettext as _
from rest_framework import serializers

from apps.common.api import EnvelopeSerializer

from .models import AlertRule
from .policies import MAX_COOLDOWN_SECONDS


class DecimalStringField(serializers.CharField):
    def to_internal_value(self, data):
        if not isinstance(data, str):
            raise serializers.ValidationError(_("Use a decimal string, not a JSON number."))
        return super().to_internal_value(data)


class UpdateAlertInputSerializer(serializers.Serializer):
    instrument_id = serializers.UUIDField(required=False)
    kind = serializers.ChoiceField(choices=AlertRule.Kind.choices, required=False)
    threshold = DecimalStringField(max_length=32, trim_whitespace=False, required=False)
    window_seconds = serializers.IntegerField(min_value=1, allow_null=True, required=False)
    cooldown_seconds = serializers.IntegerField(min_value=0, max_value=MAX_COOLDOWN_SECONDS, required=False)
    enabled = serializers.BooleanField(required=False)

    def to_internal_value(self, data):
        result = super().to_internal_value(data)
        unknown = set(data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError({name: _("Unknown field.") for name in sorted(unknown)})
        return result


class CreateAlertInputSerializer(UpdateAlertInputSerializer):
    instrument_id = serializers.UUIDField()
    kind = serializers.ChoiceField(choices=AlertRule.Kind.choices)
    threshold = DecimalStringField(max_length=32, trim_whitespace=False)


class AlertSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    instrument_id = serializers.UUIDField()
    symbol = serializers.CharField()
    market = serializers.CharField()
    currency = serializers.CharField()
    kind = serializers.ChoiceField(choices=AlertRule.Kind.choices)
    threshold = serializers.CharField()
    window_seconds = serializers.IntegerField(allow_null=True)
    cooldown_seconds = serializers.IntegerField()
    enabled = serializers.BooleanField()
    last_condition = serializers.BooleanField(allow_null=True)
    last_observation_id = serializers.CharField(allow_blank=True)
    generation = serializers.UUIDField(allow_null=True)
    last_source_at = serializers.DateTimeField(allow_null=True)
    last_triggered_at = serializers.DateTimeField(allow_null=True)
    baseline_at = serializers.DateTimeField(allow_null=True)
    state_reason = serializers.CharField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()


class AlertEnvelopeSerializer(serializers.Serializer):
    metadata = serializers.JSONField()
    errors = serializers.ListField(child=serializers.JSONField())  # type: ignore[assignment]


class AlertResponseSerializer(AlertEnvelopeSerializer):
    data = AlertSerializer()  # type: ignore[assignment]


class AlertListResponseSerializer(AlertEnvelopeSerializer):
    data = AlertSerializer(many=True)  # type: ignore[assignment]


class DeletedAlertSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    deleted = serializers.BooleanField()


class DeletedAlertResponseSerializer(EnvelopeSerializer):
    data = DeletedAlertSerializer()  # type: ignore[assignment]
