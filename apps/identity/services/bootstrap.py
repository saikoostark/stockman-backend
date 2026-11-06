from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.db import transaction

from apps.audit.services import AuditRecorder
from apps.common.exceptions import DomainError

from ..models import GroupPolicy, User
from ..policies import ACTIONS, permission_codename
from .access import AccessLifecycle
from .sessions import SessionIdentity


class BootstrapIdentity:
    @staticmethod
    def execute(*, username=None, password=None, email=""):
        from django.utils.translation import gettext as _

        if bool(username) != bool(password):
            raise DomainError("bootstrap_credentials", _("Provide both administrator username and password."))
        admin = None
        if username:
            if User.objects.filter(username=username).exists():
                raise DomainError("username_exists", _("This username is already in use."), status=409)
            admin = User(username=username, email=email)
            SessionIdentity.validate_password(password, admin)
            admin.set_password(password)
            admin.full_clean()
        with transaction.atomic():
            content_type = ContentType.objects.get_for_model(GroupPolicy)
            permissions = {
                action: Permission.objects.get_or_create(
                    content_type=content_type, codename=permission_codename(action),
                    defaults={"name": f"Can {action}"},
                )[0]
                for action in ACTIONS
            }
            trader = tuple(action for action in ACTIONS if action not in {
                "audit.read", "access.manage", "catalog.manage",
            })
            templates = {"Trader": trader, "Auditor": ("markets.read", "audit.read"),
                         "Administrator": tuple(ACTIONS)}
            changed = False
            for name, actions in templates.items():
                group, created = Group.objects.get_or_create(name=name)
                if not created:
                    # Existing groups, including intentionally empty policies, are editable.
                    continue
                changed = True
                GroupPolicy.objects.create(group=group, attributes={
                    "schema_version": 1,
                    "scopes": {action: {"ownership": "all" if "all" in ACTIONS[action].ownership else "own"}
                               for action in actions},
                    "deny_actions": [],
                })
                group.permissions.set(permissions[action] for action in actions)
                AuditRecorder.record(None, "access.group.bootstrapped", "group", group.pk,
                                     changes={"template": name}, reason="Initial access templates")
            if admin is not None:
                admin.save()
                admin.groups.add(Group.objects.get(name="Administrator"))
                AccessLifecycle.protect_last_admin()
                AuditRecorder.record(None, "access.admin.bootstrapped", "user", admin.pk,
                                     reason="Explicit local bootstrap")
                changed = True
            if changed:
                AccessLifecycle.invalidate()
            return admin
