from drf_spectacular.utils import OpenApiParameter, extend_schema

from apps.common.api import APIView, page
from apps.trading.selectors import TradingSelector
from apps.trading.serializers import (
    CancelOrderSerializer,
    FillListEnvelope,
    OrderEnvelope,
    OrderListEnvelope,
    PlaceOrderSerializer,
    PortfolioEnvelope,
)
from apps.trading.services.execution import fill_dto
from apps.trading.services.orders import CancelOrder, PlaceOrder, order_dto

IDEMPOTENCY = OpenApiParameter("Idempotency-Key", str, OpenApiParameter.HEADER, required=True)
PAGINATION = [OpenApiParameter("page", int), OpenApiParameter("page_size", int)]


class OrderListView(APIView):
    @extend_schema(responses=OrderListEnvelope, parameters=PAGINATION, tags=["Trading"])
    def get(self, request):
        items, metadata = page(TradingSelector.orders(request.user), request, order_dto)
        return self.respond(items, metadata=metadata)

    @extend_schema(request=PlaceOrderSerializer, responses={201: OrderEnvelope}, parameters=[IDEMPOTENCY], tags=["Trading"])
    def post(self, request):
        serializer = PlaceOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = PlaceOrder().execute(request.user, serializer.to_command(getattr(request, "request_id", "")), request.headers.get("Idempotency-Key", ""))
        return self.respond(result, status=201)


class CancelOrderView(APIView):
    @extend_schema(request=CancelOrderSerializer, responses=OrderEnvelope, parameters=[IDEMPOTENCY], tags=["Trading"])
    def post(self, request, order_id):
        serializer = CancelOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = CancelOrder().execute(request.user, serializer.to_command(order_id, getattr(request, "request_id", "")), request.headers.get("Idempotency-Key", ""))
        return self.respond(result)


class FillListView(APIView):
    @extend_schema(responses=FillListEnvelope, parameters=PAGINATION, tags=["Trading"])
    def get(self, request):
        items, metadata = page(TradingSelector.fills(request.user), request, fill_dto)
        return self.respond(items, metadata=metadata)


class PortfolioView(APIView):
    @extend_schema(responses=PortfolioEnvelope, tags=["Trading"])
    def get(self, request):
        return self.respond(TradingSelector.portfolio(request.user))
