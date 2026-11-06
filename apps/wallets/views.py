from drf_spectacular.utils import OpenApiParameter, extend_schema

from apps.common.api import APIView, page
from apps.wallets.selectors import WalletSelector
from apps.wallets.serializers import (
    FundingSerializer,
    JournalEnvelope,
    LedgerEnvelope,
    ReversalSerializer,
    WalletListEnvelope,
)
from apps.wallets.services.funding import ReverseFunding, TopUpWallet, WithdrawFunds
from apps.wallets.services.ledger import journal_dto

IDEMPOTENCY = OpenApiParameter("Idempotency-Key", str, OpenApiParameter.HEADER, required=True)
PAGINATION = [OpenApiParameter("page", int), OpenApiParameter("page_size", int)]


class WalletListView(APIView):
    @extend_schema(responses=WalletListEnvelope, tags=["Wallets"])
    def get(self, request):
        return self.respond(WalletSelector.list(request.user))


class TopUpView(APIView):
    @extend_schema(request=FundingSerializer, responses={201: JournalEnvelope}, parameters=[IDEMPOTENCY], tags=["Wallets"])
    def post(self, request, currency):
        serializer = FundingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = TopUpWallet().execute(request.user, serializer.to_command(currency, getattr(request, "request_id", "")), request.headers.get("Idempotency-Key", ""))
        return self.respond(result, status=201)


class WithdrawalView(APIView):
    @extend_schema(request=FundingSerializer, responses={201: JournalEnvelope}, parameters=[IDEMPOTENCY], tags=["Wallets"])
    def post(self, request, currency):
        serializer = FundingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = WithdrawFunds().execute(request.user, serializer.to_command(currency, getattr(request, "request_id", "")), request.headers.get("Idempotency-Key", ""))
        return self.respond(result, status=201)


class LedgerView(APIView):
    @extend_schema(responses=LedgerEnvelope, parameters=PAGINATION, tags=["Wallets"])
    def get(self, request, currency):
        items, metadata = page(WalletSelector.ledger(request.user, currency), request, journal_dto)
        return self.respond(items, metadata=metadata)


class ReversalView(APIView):
    @extend_schema(request=ReversalSerializer, responses={201: JournalEnvelope}, parameters=[IDEMPOTENCY], tags=["Wallets"])
    def post(self, request, journal_id):
        serializer = ReversalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = ReverseFunding().execute(request.user, serializer.to_command(journal_id, getattr(request, "request_id", "")), request.headers.get("Idempotency-Key", ""))
        return self.respond(result, status=201)
