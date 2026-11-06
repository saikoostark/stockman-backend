from rest_framework import serializers

from apps.common.api import EnvelopeSerializer
from apps.wallets.dtos import FundingCommand, ReversalCommand


class DecimalStringField(serializers.CharField):
    """DRF CharField otherwise silently coerces JSON floats into financial input."""

    def to_internal_value(self, data):
        if not isinstance(data, str):
            self.fail("invalid")
        return super().to_internal_value(data)


class FundingSerializer(serializers.Serializer):
    amount = DecimalStringField(max_length=100, trim_whitespace=False)
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")

    def to_command(self, currency, request_id=""):
        return FundingCommand(currency=currency, request_id=request_id, **self.validated_data)


class ReversalSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=500)

    def to_command(self, journal_id, request_id=""):
        return ReversalCommand(str(journal_id), self.validated_data["reason"], request_id)


class WalletSerializer(serializers.Serializer):
    currency = serializers.ChoiceField(choices=["EGP", "USDT"])
    available = serializers.CharField()
    reserved = serializers.CharField()


class EntrySerializer(serializers.Serializer):
    account = serializers.ChoiceField(choices=["AVAILABLE", "RESERVED", "ISSUANCE", "CLEARING"])
    amount = serializers.CharField()


class JournalSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    currency = serializers.CharField()
    kind = serializers.CharField()
    reference = serializers.CharField()
    reversal_of = serializers.UUIDField(allow_null=True)
    created_at = serializers.DateTimeField()
    posted_at = serializers.DateTimeField(allow_null=True)
    reason = serializers.CharField()
    entries = EntrySerializer(many=True)


class WalletListEnvelope(EnvelopeSerializer):
    data = WalletSerializer(many=True)  # type: ignore[assignment]


class JournalEnvelope(EnvelopeSerializer):
    data = JournalSerializer()  # type: ignore[assignment]


class LedgerEnvelope(EnvelopeSerializer):
    data = JournalSerializer(many=True)  # type: ignore[assignment]
