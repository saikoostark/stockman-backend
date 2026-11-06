from rest_framework import serializers

from apps.markets.dtos import CatalogCommand, InstrumentFilter, WatchCommand


class InstrumentFilterSerializer(serializers.Serializer):
    market = serializers.ChoiceField(choices=["CRYPTO", "EGX"], required=False)
    currency = serializers.ChoiceField(choices=["USDT", "EGP"], required=False)
    search = serializers.CharField(max_length=80, required=False)
    enabled = serializers.BooleanField(required=False)

    def validate(self, attrs):
        # BooleanField injects False for an omitted value with QueryDict input.
        if self.initial_data.get("enabled") is None:
            attrs.pop("enabled", None)
        return attrs

    def command(self):
        return InstrumentFilter(**self.validated_data)


class HistoryFilterSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=["observations", "candles"], default="observations")
    before = serializers.DateTimeField(required=False)


class WatchSerializer(serializers.Serializer):
    instrument_id = serializers.UUIDField()

    def command(self):
        return WatchCommand(**self.validated_data)


class CatalogSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=160, required=False)
    enabled = serializers.BooleanField(required=False)
    watched = serializers.BooleanField(required=False)
    reason = serializers.CharField(max_length=500, allow_blank=True, required=False, default="")

    def validate(self, attrs):
        for name in ("enabled", "watched"):
            if self.initial_data.get(name) is None:
                attrs.pop(name, None)
        return attrs

    def command(self, instrument_id):
        return CatalogCommand(instrument_id=instrument_id, **self.validated_data)
