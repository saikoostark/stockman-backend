from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils.translation import gettext as _

from apps.audit.services import AuditRecorder
from apps.common.exceptions import DomainError

from ..dtos import MessageResult, UserResult
from ..models import User
from .access import AccessLifecycle


class SessionIdentity:
    @staticmethod
    def current(user):
        if not getattr(user, "is_authenticated", False):
            raise DomainError("not_authenticated", _("Authentication is required."), status=401)
        try:
            return User.objects.get(pk=user.pk, is_active=True)
        except User.DoesNotExist as exc:
            raise DomainError("not_authenticated", _("Authentication is required."), status=401) from exc

    @staticmethod
    def validate_password(password, user):
        if not isinstance(password, str) or not password or len(password) > 1024:
            raise DomainError("invalid_password", _("Invalid password."))
        try:
            validate_password(password, user)
        except ValidationError as exc:
            raise DomainError("invalid_password", _("Please choose a stronger password."),
                              fields={"password": exc.messages}) from exc

    @staticmethod
    def validate_locale(locale):
        if locale not in User.Locale.values:
            raise DomainError("invalid_locale", _("Unsupported language."))


class RegisterUser:
    @staticmethod
    def execute(command, request):
        SessionIdentity.validate_locale(command.locale)
        user = User(username=command.username, email=command.email, locale=command.locale)
        SessionIdentity.validate_password(command.password, user)
        user.set_password(command.password)
        try:
            user.full_clean(exclude=["password"])
        except ValidationError as exc:
            raise DomainError("invalid_registration", _("Please check your registration details."),
                              fields=exc.message_dict) from exc
        with transaction.atomic():
            try:
                trader = Group.objects.get(name="Trader")
            except Group.DoesNotExist as exc:
                raise DomainError("identity_not_bootstrapped", _("Access templates are not initialized."), status=503) from exc
            try:
                with transaction.atomic():
                    user.save()
            except IntegrityError as exc:
                raise DomainError("username_exists", _("This username is already in use."), status=409) from exc
            user.groups.add(trader)
            # Wallets are created lazily by the finance API, never by registration.
            AuditRecorder.record(user, "auth.registered", "user", user.pk,
                                 changes={"locale": user.locale},
                                 request_id=getattr(request, "request_id", ""))
            AccessLifecycle.invalidate()
            user.refresh_from_db()
            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            return UserResult.from_user(user)


class LoginUser:
    @staticmethod
    def execute(command, request):
        user = authenticate(request, username=command.username, password=command.password)
        with transaction.atomic():
            if user is None:
                AuditRecorder.record(None, "auth.login_failed", "user", "",
                                     request_id=getattr(request, "request_id", ""))
            else:
                current = User.objects.filter(pk=user.pk, is_active=True, password=user.password).first()
                if current is None:
                    AuditRecorder.record(None, "auth.login_failed", "user", "",
                                         request_id=getattr(request, "request_id", ""))
                else:
                    login(request, current)
                    AuditRecorder.record(current, "auth.login", "user", current.pk,
                                         request_id=getattr(request, "request_id", ""))
                    return UserResult.from_user(current)
        raise DomainError("invalid_credentials", _("Invalid username or password."), status=401)


class LogoutUser:
    @staticmethod
    def execute(request):
        with transaction.atomic():
            user = SessionIdentity.current(request.user)
            AuditRecorder.record(user, "auth.logout", "user", user.pk,
                                 request_id=getattr(request, "request_id", ""))
            logout(request)
        return MessageResult(_("You have been signed out."))


class ChangePassword:
    @staticmethod
    def execute(actor, command, request):
        with transaction.atomic():
            user = SessionIdentity.current(actor)
            if not user.check_password(command.current_password):
                raise DomainError("invalid_password", _("The current password is incorrect."))
            SessionIdentity.validate_password(command.new_password, user)
            user.set_password(command.new_password)
            user.save(update_fields=["password"])
            # Retain this browser only. Other sessions fail Django's session hash check.
            update_session_auth_hash(request, user)
            AuditRecorder.record(user, "auth.password_changed", "user", user.pk,
                                 request_id=getattr(request, "request_id", ""))
        return MessageResult(_("Your password has been changed."))


class UpdatePreferences:
    @staticmethod
    def execute(actor, command, *, request_id=""):
        with transaction.atomic():
            user = SessionIdentity.current(actor)
            SessionIdentity.validate_locale(command.locale)
            before = user.locale
            user.locale = command.locale
            user.save(update_fields=["locale"])
            AuditRecorder.record(user, "auth.preferences_changed", "user", user.pk,
                                 changes={"before": {"locale": before}, "after": {"locale": user.locale}},
                                 request_id=request_id)
            return UserResult.from_user(user)
