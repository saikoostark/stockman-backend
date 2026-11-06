from django.middleware.csrf import get_token
from django.utils import translation
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import ensure_csrf_cookie
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.permissions import AllowAny, IsAuthenticated

from apps.common.api import ENVELOPE, APIView

from .dtos import CSRFResult
from .permissions import (
    AccessManagementPermission,
    ExplicitCSRFSessionAuthentication,
    LoginThrottle,
)
from .selectors import AccessSelectors, IdentitySelectors
from .serializers import (
    CreateGroupSerializer,
    LoginSerializer,
    MembershipSerializer,
    PasswordSerializer,
    PatchGroupSerializer,
    PreferencesSerializer,
    RegisterSerializer,
)
from .services import (
    ChangePassword,
    CreateGroup,
    LoginUser,
    LogoutUser,
    RegisterUser,
    UpdateGroup,
    UpdateMembership,
    UpdatePreferences,
)

PAGINATION = [OpenApiParameter("page", int), OpenApiParameter("page_size", int)]


class IdentityAPIView(APIView):
    authentication_classes = [ExplicitCSRFSessionAuthentication]
    permission_classes = [IsAuthenticated]

    def perform_authentication(self, request):
        super().perform_authentication(request)
        if not request.META.get("HTTP_ACCEPT_LANGUAGE") and request.user.is_authenticated:
            translation.activate(request.user.locale)
            request.LANGUAGE_CODE = request.user.locale
            request._request.LANGUAGE_CODE = request.user.locale

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store, private"
        return response


@method_decorator(ensure_csrf_cookie, name="dispatch")
class CSRFView(IdentityAPIView):
    permission_classes = [AllowAny]

    @extend_schema(request=None, responses={200: ENVELOPE}, tags=["Authentication"])
    def get(self, request):
        return self.respond(CSRFResult(get_token(request)))


class RegisterView(IdentityAPIView):
    permission_classes = [AllowAny]
    throttle_classes = [LoginThrottle]
    throttle_scope = "login"

    @extend_schema(request=RegisterSerializer, responses={201: ENVELOPE, 400: ENVELOPE, 403: ENVELOPE}, tags=["Authentication"])
    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return self.respond(RegisterUser.execute(serializer.command(), request), status=201)


class LoginView(IdentityAPIView):
    permission_classes = [AllowAny]
    throttle_classes = [LoginThrottle]
    throttle_scope = "login"

    @extend_schema(request=LoginSerializer, responses={200: ENVELOPE, 401: ENVELOPE, 403: ENVELOPE, 429: ENVELOPE}, tags=["Authentication"])
    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return self.respond(LoginUser.execute(serializer.command(), request))


class LogoutView(IdentityAPIView):
    @extend_schema(request=None, responses={200: ENVELOPE, 403: ENVELOPE}, tags=["Authentication"])
    def post(self, request):
        return self.respond(LogoutUser.execute(request))


class MeView(IdentityAPIView):
    @extend_schema(request=None, responses={200: ENVELOPE, 403: ENVELOPE}, tags=["Authentication"])
    def get(self, request):
        return self.respond(IdentitySelectors.me(request.user))


class PasswordView(IdentityAPIView):
    @extend_schema(request=PasswordSerializer, responses={200: ENVELOPE, 400: ENVELOPE, 403: ENVELOPE}, tags=["Authentication"])
    def post(self, request):
        serializer = PasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return self.respond(ChangePassword.execute(request.user, serializer.command(), request))


class PreferencesView(IdentityAPIView):
    @extend_schema(request=PreferencesSerializer, responses={200: ENVELOPE, 400: ENVELOPE, 403: ENVELOPE}, tags=["Authentication"])
    def patch(self, request):
        serializer = PreferencesSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return self.respond(UpdatePreferences.execute(request.user, serializer.command(),
                            request_id=getattr(request, "request_id", "")))


class CapabilitiesView(IdentityAPIView):
    @extend_schema(request=None, responses={200: ENVELOPE, 403: ENVELOPE}, tags=["Authentication"])
    def get(self, request):
        return self.respond(IdentitySelectors.capabilities(request.user))


class GroupsView(IdentityAPIView):
    permission_classes = [IsAuthenticated, AccessManagementPermission]

    @extend_schema(request=None, parameters=PAGINATION, responses={200: ENVELOPE, 403: ENVELOPE}, tags=["Access administration"])
    def get(self, request):
        items, metadata = AccessSelectors.groups(request.user, request)
        return self.respond(items, metadata=metadata)

    @extend_schema(request=CreateGroupSerializer, responses={201: ENVELOPE, 400: ENVELOPE, 403: ENVELOPE, 409: ENVELOPE}, tags=["Access administration"])
    def post(self, request):
        serializer = CreateGroupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return self.respond(CreateGroup.execute(request.user, serializer.command(),
                            request_id=getattr(request, "request_id", "")), status=201)


class GroupDetailView(IdentityAPIView):
    permission_classes = [IsAuthenticated, AccessManagementPermission]

    @extend_schema(request=None, responses={200: ENVELOPE, 403: ENVELOPE, 404: ENVELOPE}, tags=["Access administration"])
    def get(self, request, group_id):
        return self.respond(AccessSelectors.group(request.user, group_id))

    @extend_schema(request=PatchGroupSerializer, responses={200: ENVELOPE, 400: ENVELOPE, 403: ENVELOPE, 409: ENVELOPE}, tags=["Access administration"])
    def patch(self, request, group_id):
        serializer = PatchGroupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return self.respond(UpdateGroup.execute(request.user, serializer.command(group_id),
                            request_id=getattr(request, "request_id", "")))


class UsersView(IdentityAPIView):
    permission_classes = [IsAuthenticated, AccessManagementPermission]

    @extend_schema(request=None, parameters=PAGINATION, responses={200: ENVELOPE, 403: ENVELOPE}, tags=["Access administration"])
    def get(self, request):
        items, metadata = AccessSelectors.users(request.user, request)
        return self.respond(items, metadata=metadata)


class MembershipView(IdentityAPIView):
    permission_classes = [IsAuthenticated, AccessManagementPermission]

    @extend_schema(
        request=MembershipSerializer,
        responses={200: ENVELOPE, 400: ENVELOPE, 403: ENVELOPE, 404: ENVELOPE, 409: ENVELOPE},
        description="Replace memberships and/or change active status. A reason is required.",
        tags=["Access administration"],
    )
    def patch(self, request, user_id):
        serializer = MembershipSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return self.respond(UpdateMembership.execute(request.user, serializer.command(user_id),
                            request_id=getattr(request, "request_id", "")))
