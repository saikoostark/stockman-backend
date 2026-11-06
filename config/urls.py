from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from apps.operations.views import HealthView

urlpatterns = [
    path("api/v1/", include("apps.identity.urls")),
    path("api/v1/", include("apps.markets.urls")),
    path("api/v1/", include("apps.wallets.urls")),
    path("api/v1/", include("apps.trading.urls")),
    path("api/v1/", include("apps.alerts.urls")),
    path("api/v1/", include("apps.events.urls")),
    path("api/v1/", include("apps.audit.urls")),
    path("api/v1/health/", HealthView.as_view(), name="health"),
    path("api/v1/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/v1/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="docs"),
]
handler404 = "apps.common.api.not_found"
handler500 = "apps.common.api.server_error"
