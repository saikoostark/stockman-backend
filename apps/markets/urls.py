from django.urls import path

from apps.markets.views import (
    InstrumentDetailView,
    InstrumentHistoryView,
    InstrumentListView,
    ProviderListView,
    WatchlistItemView,
    WatchlistView,
)

app_name = "markets"
urlpatterns = [
    path("instruments/", InstrumentListView.as_view(), name="instrument-list"),
    path("instruments/<uuid:instrument_id>/", InstrumentDetailView.as_view(), name="instrument-detail"),
    path("instruments/<uuid:instrument_id>/history/", InstrumentHistoryView.as_view(), name="instrument-history"),
    path("watchlist/", WatchlistView.as_view(), name="watchlist"),
    path("watchlist/<uuid:instrument_id>/", WatchlistItemView.as_view(), name="watchlist-item"),
    path("providers/", ProviderListView.as_view(), name="providers"),
]
