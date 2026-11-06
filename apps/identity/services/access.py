import json
from dataclasses import asdict

from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.contrib.sessions.models import Session
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.audit.services import AuditRecorder
from apps.common.exceptions import DomainError
from apps.events.services import EventPublisher

from ..dtos import GroupResult, UserResult
from ..models import GroupPolicy, User
from ..policies import ACTIONS, AuthorizationPolicy, PolicySchema, permission_codename


class PolicyInput:
    @staticmethod
    def reason(value):
        if not isinstance(value, str) or not value.strip() or len(value) > 500:
            raise DomainError("reason_required", _("A reason is required for access changes."))

    @staticmethod
    def name(value):
        if not isinstance(value, str) or not value.strip() or len(value) > 150:
            raise DomainError("invalid_group", _("Invalid group name."))

    @staticmethod
    def attributes(value):
        try:
            attributes = json.loads(value)
            PolicySchema.validate(attributes)
        except (TypeError, ValueError, ValidationError) as exc:
            raise DomainError("invalid_policy", _("Invalid group policy attributes.")) from exc
        ids = {pk for scope in attributes["scopes"].values() for pk in scope.get("owner_group_ids", [])}
        if ids and Group.objects.filter(pk__in=ids).count() != len(ids):
            raise DomainError("invalid_groups", _("One or more groups do not exist."))
        return attributes

    @staticmethod
    def permissions(actions):
        if len(actions) != len(set(actions)) or any(action not in ACTIONS for action in actions):
            raise DomainError("invalid_permissions", _("Only registered permissions are allowed."))
        content_type = ContentType.objects.get_for_model(GroupPolicy)
        permissions = list(Permission.objects.filter(
            content_type=content_type, codename__in=[permission_codename(action) for action in actions]
        ))
        if len(permissions) != len(actions):
            raise DomainError("identity_not_bootstrapped", _("Access templates are not initialized."), status=503)
        return permissions

    @staticmethod
    def grants_have_scopes(permissions, attributes):
        actions = {action for action in ACTIONS if permission_codename(action) in {
            permission.codename for permission in permissions
        }}
        if actions - set(attributes["scopes"]):
            raise DomainError("invalid_policy", _("Every granted action requires an explicit scope."))


class AccessLifecycle:
    @staticmethod
    def protect_last_admin():
        if not any(AuthorizationPolicy.allows(user, "access.manage") for user in
                   User.objects.filter(is_active=True).iterator(chunk_size=100)):
            raise DomainError("last_access_admin", _("The last active access administrator cannot be removed."), status=409)

    @staticmethod
    def invalidate():
        # Scope changes affect owners as well as direct members. Global revisions
        # deliberately avoid an incomplete dependency traversal.
        User.objects.update(authorization_revision=F("authorization_revision") + 1)
        # An unavailable revocation implementation MUST roll back access changes.
        from apps.trading.services import AccessRevocation

        AccessRevocation.cancel_unauthorized()
        for user in User.objects.filter(is_active=True).iterator(chunk_size=100):
            EventPublisher.publish(user, "authorization.changed", {
                "authorization_revision": user.authorization_revision,
            })

    @staticmethod
    def revoke_sessions(user):
        for session in Session.objects.filter(expire_date__gt=timezone.now()).iterator(chunk_size=100):
            if str(session.get_decoded().get("_auth_user_id")) == str(user.pk):
                session.delete()


class CreateGroup:
    @staticmethod
    def execute(actor, command, *, request_id=""):
        with transaction.atomic():
            AuthorizationPolicy.require(actor, "access.manage")
            PolicyInput.reason(command.reason)
            PolicyInput.name(command.name)
            attributes = PolicyInput.attributes(command.attributes_json)
            permissions = PolicyInput.permissions(command.permissions)
            PolicyInput.grants_have_scopes(permissions, attributes)
            try:
                with transaction.atomic():
                    group = Group.objects.create(name=command.name)
            except IntegrityError as exc:
                raise DomainError("group_exists", _("A group with this name already exists."), status=409) from exc
            GroupPolicy.objects.create(group=group, attributes=attributes)
            group.permissions.set(permissions)
            result = GroupResult.from_group(group)
            AccessLifecycle.protect_last_admin()
            AuditRecorder.record(actor, "access.group.created", "group", group.pk,
                                 changes={"before": None, "after": asdict(result)},
                                 request_id=request_id, reason=command.reason)
            AccessLifecycle.invalidate()
            return result


class UpdateGroup:
    @staticmethod
    def execute(actor, command, *, request_id=""):
        with transaction.atomic():
            AuthorizationPolicy.require(actor, "access.manage")
            PolicyInput.reason(command.reason)
            if all(value is None for value in (command.name, command.permissions, command.attributes_json)):
                raise DomainError("no_changes", _("Provide at least one change."))
            try:
                group = Group.objects.get(pk=command.group_id)
            except Group.DoesNotExist as exc:
                raise DomainError("not_found", _("Group not found."), status=404) from exc
            before = asdict(GroupResult.from_group(group))
            attributes = PolicyInput.attributes(
                command.attributes_json if command.attributes_json is not None
                else json.dumps(before["attributes"])
            )
            permissions = PolicyInput.permissions(
                command.permissions if command.permissions is not None else before["permissions"]
            )
            PolicyInput.grants_have_scopes(permissions, attributes)
            if command.name is not None:
                PolicyInput.name(command.name)
                group.name = command.name
                try:
                    with transaction.atomic():
                        group.save(update_fields=["name"])
                except IntegrityError as exc:
                    raise DomainError("group_exists", _("A group with this name already exists."), status=409) from exc
            GroupPolicy.objects.update_or_create(group=group, defaults={"attributes": attributes})
            group.permissions.set(permissions)
            # A fresh object avoids reverse one-to-one and prefetch caches.
            group = Group.objects.get(pk=group.pk)
            result = GroupResult.from_group(group)
            AccessLifecycle.protect_last_admin()
            AuditRecorder.record(actor, "access.group.updated", "group", group.pk,
                                 changes={"before": before, "after": asdict(result)},
                                 request_id=request_id, reason=command.reason)
            AccessLifecycle.invalidate()
            return result


class UpdateMembership:
    @staticmethod
    def execute(actor, command, *, request_id=""):
        with transaction.atomic():
            AuthorizationPolicy.require(actor, "access.manage")
            PolicyInput.reason(command.reason)
            if command.group_ids is None and command.is_active is None:
                raise DomainError("no_changes", _("Provide at least one change."))
            if command.is_active is not None and type(command.is_active) is not bool:
                raise DomainError("invalid_active", _("Invalid active status."))
            try:
                user = User.objects.get(pk=command.user_id)
            except User.DoesNotExist as exc:
                raise DomainError("not_found", _("User not found."), status=404) from exc
            before = {"group_ids": list(user.groups.values_list("pk", flat=True)), "is_active": user.is_active}
            if command.group_ids is not None:
                ids = command.group_ids
                if len(ids) > 100 or any(type(pk) is not int or pk <= 0 for pk in ids) or len(set(ids)) != len(ids):
                    raise DomainError("invalid_groups", _("Invalid group selection."))
                groups = list(Group.objects.filter(pk__in=ids))
                if len(groups) != len(ids):
                    raise DomainError("invalid_groups", _("One or more groups do not exist."))
                user.groups.set(groups)
            if command.is_active is not None:
                user.is_active = command.is_active
                user.save(update_fields=["is_active"])
                if not user.is_active:
                    AccessLifecycle.revoke_sessions(user)
            AccessLifecycle.protect_last_admin()
            after = {"group_ids": list(user.groups.values_list("pk", flat=True)), "is_active": user.is_active}
            AuditRecorder.record(actor, "access.membership.updated", "user", user.pk,
                                 changes={"before": before, "after": after},
                                 request_id=request_id, reason=command.reason)
            AccessLifecycle.invalidate()
            user.refresh_from_db()
            return UserResult.from_user(user)
