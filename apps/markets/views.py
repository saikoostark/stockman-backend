from drf_spectacular.utils import OpenApiParameter, extend_schema

from apps.common.api import ENVELOPE, APIView, page
from apps.markets.dtos import WatchCommand
from apps.markets.selectors import MarketSelectors
from apps.markets.serializers import (
    CatalogSerializer,
    HistoryFilterSerializer,
    InstrumentFilterSerializer,
    WatchSerializer,
)
from apps.markets.services import ManageWatchlist, UpdateCatalog

PAGINATION = [OpenApiParameter("page", int), OpenApiParameter("page_size", int, description="1-100; default 25.")]


class InstrumentListView(APIView):
    @extend_schema(parameters=[InstrumentFilterSerializer, *PAGINATION], responses={200: ENVELOPE}, tags=["Markets"])
    def get(self, request):
        filters = InstrumentFilterSerializer(data=request.query_params)
        filters.is_valid(raise_exception=True)
        selectors = MarketSelectors()
        data, metadata = page(selectors.instruments(request.user, filters.command()), request, selectors.describe)
        return self.respond(data, metadata=metadata)


class InstrumentDetailView(APIView):
    @extend_schema(responses={200: ENVELOPE, 404: ENVELOPE}, tags=["Markets"])
    def get(self, request, instrument_id):
        selectors = MarketSelectors()
        return self.respond(selectors.describe(selectors.instrument(request.user, instrument_id)))

    @extend_schema(request=CatalogSerializer, responses={200: ENVELOPE, 403: ENVELOPE, 409: ENVELOPE}, tags=["Catalog administration"])
    def patch(self, request, instrument_id):
        serializer = CatalogSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = UpdateCatalog.execute(request.user, serializer.command(instrument_id), request_id=getattr(request, "request_id", ""))
        return self.respond(result)


class InstrumentHistoryView(APIView):
    @extend_schema(parameters=[HistoryFilterSerializer, *PAGINATION], responses={200: ENVELOPE, 404: ENVELOPE}, tags=["Markets"])
    def get(self, request, instrument_id):
        serializer = HistoryFilterSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        filters = serializer.validated_data
        query = MarketSelectors.history(request.user, instrument_id, **filters)
        mapper = MarketSelectors.candle if filters["kind"] == "candles" else MarketSelectors.observation
        data, metadata = page(query, request, mapper)
        return self.respond(data, metadata=metadata)


class WatchlistView(APIView):
    @extend_schema(parameters=PAGINATION, responses={200: ENVELOPE}, tags=["Markets"])
    def get(self, request):
        selectors = MarketSelectors()
        data, metadata = page(selectors.watchlist(request.user), request, selectors.watch)
        return self.respond(data, metadata=metadata)

    @extend_schema(request=WatchSerializer, responses={201: ENVELOPE, 403: ENVELOPE, 404: ENVELOPE}, tags=["Markets"])
    def post(self, request):
        serializer = WatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return self.respond(ManageWatchlist.add(request.user, serializer.command()), status=201)

    @extend_schema(request=WatchSerializer, responses={200: ENVELOPE}, tags=["Markets"])
    def delete(self, request):
        serializer = WatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ManageWatchlist.remove(request.user, serializer.command())
        return self.respond()


class WatchlistItemView(APIView):
    @extend_schema(responses={200: ENVELOPE}, tags=["Markets"])
    def delete(self, request, instrument_id):
        ManageWatchlist.remove(request.user, WatchCommand(instrument_id))
        return self.respond()


class ProviderListView(APIView):
    @extend_schema(responses={200: ENVELOPE}, tags=["Markets"])
    def get(self, request):
        return self.respond(MarketSelectors().providers(request.user))
