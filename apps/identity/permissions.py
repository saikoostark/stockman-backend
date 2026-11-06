from django.middleware.csrf import CsrfViewMiddleware
from django.utils.translation import gettext as _
from drf_spectacular.authentication import SessionScheme
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import BasePermission
from rest_framework.throttling import ScopedRateThrottle

from apps.common.exceptions import DomainError

from .policies import AuthorizationPolicy


class CSRFCheck(CsrfViewMiddleware):
    def _reject(self, request, reason):
        return reason


class ExplicitCSRFSessionAuthentication(SessionAuthentication):
    def authenticate(self, request):
        # DRF normally exempts anonymous session requests: login/register must not.
        if request.method not in {"GET", "HEAD", "OPTIONS", "TRACE"}:
            check = CSRFCheck(lambda request: None)  # type: ignore[arg-type]
            check.process_request(request)
            if check.process_view(request, None, (), {}):  # type: ignore[arg-type]
                raise DomainError("csrf_failed", _("CSRF verification failed."), status=403)
        return super().authenticate(request)


class ExplicitSessionScheme(SessionScheme):
    target_class = "apps.identity.permissions.ExplicitCSRFSessionAuthentication"
    priority = 1


class AccessManagementPermission(BasePermission):
    def has_permission(self, request, view):
        return AuthorizationPolicy.allows(request.user, "access.manage")


class LoginThrottle(ScopedRateThrottle):
    def get_cache_key(self, request, view):
        # IP-scoped even for an existing session: changing accounts cannot reset it.
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}
