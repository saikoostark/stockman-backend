from rest_framework import serializers

from apps.common.api import EnvelopeSerializer
from apps.trading.dtos import CancelOrderCommand, PlaceOrderCommand
from apps.wallets.serializers import DecimalStringField


class PlaceOrderSerializer(serializers.Serializer):
    instrument_id = serializers.UUIDField()
    side = serializers.ChoiceField(choices=["BUY", "SELL"])
    order_type = serializers.ChoiceField(choices=["MARKET", "LIMIT"])
    quantity = DecimalStringField(required=False, max_length=100, trim_whitespace=False)
    limit_price = DecimalStringField(required=False, max_length=100, trim_whitespace=False)
    budget = DecimalStringField(required=False, max_length=100, trim_whitespace=False)

    def to_command(self, request_id=""):
        values = {**self.validated_data, "instrument_id": str(self.validated_data["instrument_id"])}
        return PlaceOrderCommand(**values, request_id=request_id)


class CancelOrderSerializer(serializers.Serializer):
    def to_command(self, order_id, request_id=""):
        return CancelOrderCommand(str(order_id), request_id)


class OrderSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    instrument_id = serializers.UUIDField()
    symbol = serializers.CharField()
    currency = serializers.CharField()
    side = serializers.ChoiceField(choices=["BUY", "SELL"])
    order_type = serializers.ChoiceField(choices=["MARKET", "LIMIT"])
    quantity = serializers.CharField(allow_null=True)
    limit_price = serializers.CharField(allow_null=True)
    budget = serializers.CharField(allow_null=True)
    reserved_cash = serializers.CharField()
    reserved_quantity = serializers.CharField()
    status = serializers.ChoiceField(choices=["OPEN", "FILLED", "CANCELLED", "EXPIRED"])
    created_at = serializers.DateTimeField()
    accepted_at = serializers.DateTimeField()
    expires_at = serializers.DateTimeField(allow_null=True)
    closed_at = serializers.DateTimeField(allow_null=True)
    close_reason = serializers.CharField()
    source_watermark = serializers.CharField()
    generation = serializers.CharField()


class FillSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    order_id = serializers.UUIDField()
    instrument_id = serializers.UUIDField()
    symbol = serializers.CharField()
    currency = serializers.CharField()
    side = serializers.CharField()
    quantity = serializers.CharField()
    price = serializers.CharField()
    cash = serializers.CharField()
    cost_basis = serializers.CharField()
    realized_pnl = serializers.CharField()
    fees = serializers.CharField()
    source_at = serializers.DateTimeField()
    source_watermark = serializers.CharField()
    generation = serializers.CharField()
    created_at = serializers.DateTimeField()


class HoldingSerializer(serializers.Serializer):
    instrument_id = serializers.UUIDField()
    symbol = serializers.CharField()
    currency = serializers.CharField()
    available = serializers.CharField()
    reserved = serializers.CharField()
    cost_basis = serializers.CharField()
    realized_pnl = serializers.CharField()
    mark_price = serializers.CharField(allow_null=True)
    market_value = serializers.CharField(allow_null=True)
    unrealized_pnl = serializers.CharField(allow_null=True)
    mark_source_at = serializers.DateTimeField(allow_null=True)
    stale = serializers.BooleanField()


class PortfolioCurrencySerializer(serializers.Serializer):
    currency = serializers.CharField()
    realized_pnl = serializers.CharField()
    unrealized_pnl = serializers.CharField(allow_null=True)
    market_value = serializers.CharField(allow_null=True)
    stale = serializers.BooleanField()
    holdings = HoldingSerializer(many=True)


class OrderEnvelope(EnvelopeSerializer):
    data = OrderSerializer()  # type: ignore[assignment]


class OrderListEnvelope(EnvelopeSerializer):
    data = OrderSerializer(many=True)  # type: ignore[assignment]


class FillListEnvelope(EnvelopeSerializer):
    data = FillSerializer(many=True)  # type: ignore[assignment]


class PortfolioEnvelope(EnvelopeSerializer):
    data = PortfolioCurrencySerializer(many=True)  # type: ignore[assignment]
