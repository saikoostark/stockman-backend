from django.urls import path

from apps.trading.views import CancelOrderView, FillListView, OrderListView, PortfolioView

app_name = "trading"
urlpatterns = [
    path("orders/", OrderListView.as_view(), name="orders"),
    path("orders/<uuid:order_id>/cancel/", CancelOrderView.as_view(), name="cancel"),
    path("fills/", FillListView.as_view(), name="fills"),
    path("portfolio/", PortfolioView.as_view(), name="portfolio"),
]
