from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class RegisterCommand:
    username: str
    password: str = field(repr=False)
    email: str = ""
    locale: str = "en"


@dataclass(frozen=True, slots=True)
class LoginCommand:
    username: str
    password: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class PasswordCommand:
    current_password: str = field(repr=False)
    new_password: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class PreferencesCommand:
    locale: str


@dataclass(frozen=True, slots=True)
class CreateGroupCommand:
    name: str
    permissions: tuple[str, ...]
    attributes_json: str
    reason: str


@dataclass(frozen=True, slots=True)
class PatchGroupCommand:
    group_id: int
    reason: str
    name: str | None = None
    permissions: tuple[str, ...] | None = None
    attributes_json: str | None = None


@dataclass(frozen=True, slots=True)
class MembershipCommand:
    user_id: int
    reason: str
    group_ids: tuple[int, ...] | None = None
    is_active: bool | None = None


@dataclass(frozen=True, slots=True)
class UserResult:
    id: int
    username: str
    email: str
    locale: str
    is_active: bool
    authorization_revision: int
    group_ids: tuple[int, ...]

    @classmethod
    def from_user(cls, user):
        return cls(user.pk, user.username, user.email, user.locale, user.is_active,
                   user.authorization_revision, tuple(group.pk for group in user.groups.all()))


@dataclass(frozen=True, slots=True)
class GroupResult:
    id: int
    name: str
    permissions: tuple[str, ...]
    attributes: dict[str, Any]

    @classmethod
    def from_group(cls, group):
        from .models import GroupPolicy, default_policy_attributes
        from .policies import ACTIONS, permission_codename

        registered = {permission_codename(action): action for action in ACTIONS}
        permissions = tuple(sorted(
            registered[permission.codename] for permission in group.permissions.all()
            if permission.codename in registered
            and permission.content_type.app_label == "identity"
            and permission.content_type.model == "grouppolicy"
        ))
        try:
            attributes = group.policy.attributes
        except GroupPolicy.DoesNotExist:
            attributes = default_policy_attributes()
        return cls(group.pk, group.name, permissions, attributes)


@dataclass(frozen=True, slots=True)
class MessageResult:
    message: str


@dataclass(frozen=True, slots=True)
class CSRFResult:
    csrf_token: str


@dataclass(frozen=True, slots=True)
class CapabilitiesResult:
    authorization_revision: int
    actions: dict[str, Any]
    policy_schema: dict[str, Any]
