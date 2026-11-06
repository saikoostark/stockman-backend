from django.contrib.auth.models import Group
from django.utils.translation import gettext as _

from apps.common.api import page
from apps.common.exceptions import DomainError

from .dtos import CapabilitiesResult, GroupResult, UserResult
from .models import User
from .policies import AuthorizationPolicy, PolicySchema
from .services.sessions import SessionIdentity


class IdentitySelectors:
    @staticmethod
    def me(user):
        return UserResult.from_user(SessionIdentity.current(user))

    @staticmethod
    def capabilities(user):
        current = SessionIdentity.current(user)
        return CapabilitiesResult(current.authorization_revision,
                                  AuthorizationPolicy.capabilities(current), PolicySchema.describe())


class AccessSelectors:
    @staticmethod
    def groups(user, request):
        AuthorizationPolicy.require(user, "access.manage")
        queryset = Group.objects.select_related("policy").prefetch_related(
            "permissions__content_type"
        ).order_by("id")
        return page(queryset, request, GroupResult.from_group)

    @staticmethod
    def group(user, group_id):
        AuthorizationPolicy.require(user, "access.manage")
        try:
            group = Group.objects.select_related("policy").prefetch_related(
                "permissions__content_type"
            ).get(pk=group_id)
        except Group.DoesNotExist as exc:
            raise DomainError("not_found", _("Group not found."), status=404) from exc
        return GroupResult.from_group(group)

    @staticmethod
    def users(user, request):
        AuthorizationPolicy.require(user, "access.manage")
        queryset = User.objects.prefetch_related("groups").order_by("id")
        return page(queryset, request, UserResult.from_user)
