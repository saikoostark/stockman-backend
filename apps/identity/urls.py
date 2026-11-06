from django.urls import path

from .views import (
    CapabilitiesView,
    CSRFView,
    GroupDetailView,
    GroupsView,
    LoginView,
    LogoutView,
    MembershipView,
    MeView,
    PasswordView,
    PreferencesView,
    RegisterView,
    UsersView,
)

app_name = "identity"
urlpatterns = [
    path("auth/csrf/", CSRFView.as_view(), name="csrf"),
    path("auth/register/", RegisterView.as_view(), name="register"),
    path("auth/login/", LoginView.as_view(), name="login"),
    path("auth/logout/", LogoutView.as_view(), name="logout"),
    path("auth/me/", MeView.as_view(), name="me"),
    path("auth/password/", PasswordView.as_view(), name="password"),
    path("auth/preferences/", PreferencesView.as_view(), name="preferences"),
    path("auth/capabilities/", CapabilitiesView.as_view(), name="capabilities"),
    path("administration/groups/", GroupsView.as_view(), name="groups"),
    path("administration/groups/<int:group_id>/", GroupDetailView.as_view(), name="group-detail"),
    path("administration/users/", UsersView.as_view(), name="users"),
    path("administration/users/<int:user_id>/groups/", MembershipView.as_view(), name="membership"),
]
