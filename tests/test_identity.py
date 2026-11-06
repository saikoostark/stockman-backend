import json
from pathlib import Path

import pytest
from django.contrib.auth.models import AnonymousUser, Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import connection
from django.urls import include, path
from django.utils import translation
from rest_framework.test import APIClient

from apps.audit.models import AuditEvent
from apps.common.exceptions import DomainError
from apps.identity.dtos import CreateGroupCommand, MembershipCommand, PatchGroupCommand
from apps.identity.models import GroupPolicy, User
from apps.identity.policies import ACTIONS, AuthorizationPolicy, PolicySchema, permission_codename
from apps.identity.services import BootstrapIdentity, CreateGroup, UpdateGroup, UpdateMembership

urlpatterns = [path("api/v1/", include("apps.identity.urls"))]
pytestmark = pytest.mark.django_db
PASSWORD = "Long-Random-Test-Phrase!72"


@pytest.fixture(scope="session")
def django_db_modify_db_settings():
    from django.conf import settings

    settings.DATABASES["default"]["TEST"]["NAME"] = ":memory:"


@pytest.fixture(autouse=True)
def identity_settings(settings):
    settings.ROOT_URLCONF = __name__
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    settings.SESSION_COOKIE_SECURE = False
    settings.CSRF_COOKIE_SECURE = False
    cache.clear()
    translation.activate("en")
    yield
    translation.deactivate()
    cache.clear()


@pytest.fixture
def user():
    return User.objects.create_user(username="member", password=PASSWORD)


@pytest.fixture
def group_factory():
    content_type = ContentType.objects.get_for_model(GroupPolicy)
    for action in ACTIONS:
        Permission.objects.get_or_create(
            content_type=content_type, codename=permission_codename(action),
            defaults={"name": action},
        )

    def create(name, grants=(), scopes=None, denies=()):
        group = Group.objects.create(name=name)
        GroupPolicy.objects.create(group=group, attributes={
            "schema_version": 1, "scopes": scopes or {}, "deny_actions": list(denies),
        })
        content_type = ContentType.objects.get_for_model(GroupPolicy)
        for action in grants:
            permission, _ = Permission.objects.get_or_create(
                content_type=content_type, codename=permission_codename(action),
                defaults={"name": action},
            )
            group.permissions.add(permission)
        return group
    return create


@pytest.fixture
def administrator(group_factory):
    group = group_factory("Access", ["access.manage"], {"access.manage": {"ownership": "all"}})
    admin = User.objects.create_user(username="access-admin", password=PASSWORD)
    admin.groups.add(group)
    return admin


@pytest.fixture
def bootstrapped():
    BootstrapIdentity.execute()


@pytest.fixture
def client():
    return APIClient(enforce_csrf_checks=True)


def csrf(client):
    response = client.get("/api/v1/auth/csrf/")
    assert response.status_code == 200
    assert "csrftoken" in response.cookies
    return response.json()["data"]["csrf_token"]


def post(client, endpoint, payload, **headers):
    return client.post(f"/api/v1/{endpoint}/", payload, format="json",
                       HTTP_X_CSRFTOKEN=csrf(client), **headers)


def patch(client, endpoint, payload, **headers):
    return client.patch(f"/api/v1/{endpoint}/", payload, format="json",
                        HTTP_X_CSRFTOKEN=csrf(client), **headers)


def test_user_defaults_and_safe_password_repr(user):
    from apps.identity.dtos import LoginCommand

    assert user.locale == "en"
    assert user.authorization_revision == 0
    assert PASSWORD not in repr(LoginCommand(user.username, PASSWORD))


def test_no_flattened_user_permission_or_superuser_bypass(user, group_factory):
    grant = group_factory("private", ["wallets.read"], {"wallets.read": {"ownership": "own"}})
    user.user_permissions.set(grant.permissions.all())
    user.is_staff = user.is_superuser = True
    user.save()
    assert user.has_perm("identity.wallets_read")
    assert not AuthorizationPolicy.allows(user, "wallets.read", owner_id=user.pk)
    assert not AuthorizationPolicy.allows(AnonymousUser(), "wallets.read")
    assert not AuthorizationPolicy.allows(user, "unregistered.action")
    assert not AuthorizationPolicy.scope(user, "wallets.read", User.objects.all(), "pk").exists()
    with pytest.raises(DomainError) as failure:
        AuthorizationPolicy.require(user, "wallets.read")
    assert failure.value.status == 403


def test_same_group_permission_scope_binding(user, group_factory):
    grant = group_factory("grant", ["trading.place"], {
        "trading.place": {"ownership": "own", "markets": ["CRYPTO"], "currencies": ["USDT"]},
    })
    attributes_only = group_factory("wide", [], {"trading.place": {"ownership": "own"}})
    user.groups.add(grant, attributes_only)
    assert AuthorizationPolicy.allows(user, "trading.place", market="CRYPTO", currency="USDT")
    assert not AuthorizationPolicy.allows(user, "trading.place", market="EGX", currency="EGP")
    assert not AuthorizationPolicy.allows(user, "trading.place", market="CRYPTO", currency="EGP")
    assert not AuthorizationPolicy.allows(user, "trading.place", market="EGX", currency="USDT")
    assert not AuthorizationPolicy.allows(user, "trading.place")
    assert not AuthorizationPolicy.allows(user, "trading.place", owner_id=user.pk + 1,
                                          market="CRYPTO", currency="USDT")


def test_groups_are_or_and_dimensions_are_and(user, group_factory):
    crypto = group_factory("crypto", ["trading.read"], {
        "trading.read": {"ownership": "own", "markets": ["CRYPTO"], "currencies": ["USDT"]},
    })
    egx = group_factory("egx", ["trading.read"], {
        "trading.read": {"ownership": "own", "markets": ["EGX"], "currencies": ["EGP"]},
    })
    user.groups.add(crypto, egx)
    assert AuthorizationPolicy.allows(user, "trading.read", market="EGX", currency="EGP")
    assert AuthorizationPolicy.allows(user, "trading.read", market="CRYPTO", currency="USDT")
    assert not AuthorizationPolicy.allows(user, "trading.read", market="CRYPTO", currency="EGP")


def test_denies_veto_even_without_permission(user, group_factory):
    allow = group_factory("allow", ["wallets.read"], {"wallets.read": {"ownership": "own"}})
    deny = group_factory("deny", denies=["wallets.read"])
    user.groups.add(allow, deny)
    assert not AuthorizationPolicy.allows(user, "wallets.read")
    assert not AuthorizationPolicy.scope(user, "wallets.read", User.objects.all(), "pk").exists()
    user.groups.remove(deny)
    assert AuthorizationPolicy.allows(user, "wallets.read")


def test_permission_without_scope_is_not_a_grant(user, group_factory):
    user.groups.add(group_factory("unscoped", ["wallets.read"]))
    assert not AuthorizationPolicy.allows(user, "wallets.read")


def test_unknown_or_missing_policy_fails_closed(user, group_factory):
    grant = group_factory("allow", ["wallets.read"], {"wallets.read": {"ownership": "own"}})
    corrupt = group_factory("corrupt")
    user.groups.add(grant, corrupt)
    GroupPolicy.objects.filter(group=corrupt).update(attributes={"schema_version": 99})
    assert not AuthorizationPolicy.allows(user, "wallets.read")
    user.groups.remove(corrupt)
    user.groups.add(Group.objects.create(name="missing-policy"))
    assert not AuthorizationPolicy.allows(user, "wallets.read")


def test_stale_membership_and_active_caches_are_not_trusted(user, group_factory):
    group = group_factory("allow", ["events.read"], {"events.read": {"ownership": "own"}})
    user.groups.add(group)
    cached = User.objects.prefetch_related("groups__permissions", "groups__policy").get(pk=user.pk)
    assert AuthorizationPolicy.allows(cached, "events.read")
    User.objects.filter(pk=user.pk).update(is_active=False)
    assert not AuthorizationPolicy.allows(cached, "events.read")
    User.objects.filter(pk=user.pk).update(is_active=True)
    user.groups.clear()
    assert not AuthorizationPolicy.allows(cached, "events.read")


@pytest.mark.parametrize("action", [name for name, spec in ACTIONS.items() if spec.ownership == ("own",)])
@pytest.mark.parametrize("ownership", ["all", "owner_groups"])
def test_private_actions_reject_wider_ownership(action, ownership):
    with pytest.raises(ValidationError):
        PolicySchema.validate({"schema_version": 1, "scopes": {
            action: {"ownership": ownership, "owner_group_ids": [1]},
        }})


@pytest.mark.parametrize("attributes", [
    {}, {"schema_version": True, "scopes": {}}, {"schema_version": 2, "scopes": {}},
    {"schema_version": 1, "scopes": {}, "expression": "True"},
    {"schema_version": 1, "scopes": {}, "deny_actions": ["unknown"]},
    {"schema_version": 1, "scopes": {}, "deny_actions": [["audit.read"]]},
    {"schema_version": 1, "scopes": {"unknown": {"ownership": "all"}}},
    {"schema_version": 1, "scopes": {"audit.read": {"ownership": "owner_groups"}}},
    {"schema_version": 1, "scopes": {"audit.read": {"ownership": "all", "owner_group_ids": [1]}}},
    {"schema_version": 1, "scopes": {"audit.read": {"ownership": "owner_groups", "owner_group_ids": [True]}}},
    {"schema_version": 1, "scopes": {"wallets.read": {"ownership": "own", "currencies": ["USD"]}}},
    {"schema_version": 1, "scopes": {"wallets.read": {"ownership": "own", "markets": ["EGX"]}}},
    {"schema_version": 1, "scopes": {"access.manage": {"ownership": "own"}}},
    {"schema_version": 1, "scopes": {"access.manage": {"ownership": "all", "markets": ["EGX"]}}},
    {"schema_version": 1, "scopes": {"trading.place": {"ownership": "own", "markets": "CRYPTO"}}},
    {"schema_version": 1, "scopes": {"trading.place": {"ownership": "own", "currencies": ["USDT", "USDT"]}}},
])
def test_strict_policy_validation(attributes):
    with pytest.raises(ValidationError):
        PolicySchema.validate(attributes)


def test_group_owner_scope_and_queryset_agree(user, group_factory):
    owners = group_factory("owners")
    allowed = User.objects.create_user(username="allowed")
    other = User.objects.create_user(username="other")
    allowed.groups.add(owners)
    auditor = group_factory("audit", ["audit.read"], {
        "audit.read": {"ownership": "owner_groups", "owner_group_ids": [owners.pk]},
    })
    user.groups.add(auditor)
    assert AuthorizationPolicy.allows(user, "audit.read", owner_id=allowed.pk)
    assert not AuthorizationPolicy.allows(user, "audit.read", owner_id=other.pk)
    assert list(AuthorizationPolicy.scope(user, "audit.read", User.objects.all(), "pk")) == [allowed]
    allowed.groups.clear()
    assert not AuthorizationPolicy.allows(user, "audit.read", owner_id=allowed.pk)


def test_scope_dimensions_and_missing_fields_fail_closed(user, group_factory):
    user.first_name = "CRYPTO"
    user.username = "USDT"
    user.save()
    group = group_factory("dimensions", ["trading.read"], {
        "trading.read": {"ownership": "own", "markets": ["CRYPTO"], "currencies": ["USDT"]},
    })
    user.groups.add(group)
    User.objects.create_user(username="other", first_name="CRYPTO")
    assert list(AuthorizationPolicy.scope(user, "trading.read", User.objects.all(),
                                         "pk", "first_name", "username")) == [user]
    assert not AuthorizationPolicy.scope(user, "trading.read", User.objects.all(), "pk").exists()
    user.username = "EGP"
    user.save()
    assert not AuthorizationPolicy.scope(user, "trading.read", User.objects.all(),
                                         "pk", "first_name", "username").exists()


def test_empty_allowlist_denies(user, group_factory):
    user.groups.add(group_factory("empty", ["wallets.read"], {
        "wallets.read": {"ownership": "own", "currencies": []},
    }))
    assert not AuthorizationPolicy.allows(user, "wallets.read", currency="USDT")


def test_signup_session_csrf_and_no_elevation(client, bootstrapped):
    payload = {"username": "new-user", "password": PASSWORD, "locale": "ar"}
    rejected = client.post("/api/v1/auth/register/", payload, format="json")
    assert rejected.status_code == 403
    assert rejected.json()["errors"][0]["code"] == "csrf_failed"
    response = post(client, "auth/register", payload)
    assert response.status_code == 201, response.json()
    user = User.objects.get(username="new-user")
    assert user.check_password(PASSWORD)
    assert user.locale == "ar"
    assert list(user.groups.values_list("name", flat=True)) == ["Trader"]
    assert not user.is_staff and not user.is_superuser
    assert client.cookies["sessionid"]["httponly"]
    assert client.get("/api/v1/auth/me/").json()["data"]["id"] == user.pk
    assert not AuthorizationPolicy.allows(user, "access.manage")
    assert AuditEvent.objects.filter(action="auth.registered", actor=user).exists()
    from apps.wallets.models import Wallet

    assert not Wallet.objects.filter(user=user).exists()


@pytest.mark.parametrize("extra", [{"groups": [1]}, {"is_superuser": True}, {"authorization_revision": 99}])
def test_registration_rejects_privileged_fields(client, bootstrapped, extra):
    result = post(client, "auth/register", {"username": "attempt", "password": PASSWORD, **extra})
    assert result.status_code == 400
    assert not User.objects.filter(username="attempt").exists()


def test_signup_validates_password_and_locale(client, bootstrapped):
    result = post(client, "auth/register", {"username": "weak", "password": "123"})
    assert result.status_code == 400
    assert result.json()["errors"][0]["code"] == "invalid_password"
    result = post(client, "auth/register", {"username": "lang", "password": PASSWORD, "locale": "fr"})
    assert result.status_code == 400
    assert not User.objects.filter(username__in=["weak", "lang"]).exists()


def test_login_explicit_csrf_failure_audit_and_logout(client, user):
    assert client.get("/api/v1/auth/me/").status_code == 403
    payload = {"username": user.username, "password": PASSWORD}
    assert client.post("/api/v1/auth/login/", payload, format="json").status_code == 403
    failure = post(client, "auth/login", {**payload, "password": "wrong"})
    assert failure.status_code == 401
    assert AuditEvent.objects.filter(action="auth.login_failed").exists()
    assert post(client, "auth/login", payload).status_code == 200
    assert client.post("/api/v1/auth/logout/").status_code == 403
    assert post(client, "auth/logout", {}).status_code == 200
    assert client.get("/api/v1/auth/me/").status_code == 403
    assert AuditEvent.objects.filter(action="auth.logout", actor=user).exists()


def test_csrf_rejects_wrong_origin_and_token(client, user):
    token = csrf(client)
    payload = {"username": user.username, "password": PASSWORD}
    response = client.post("/api/v1/auth/login/", payload, format="json",
                           HTTP_X_CSRFTOKEN=token, HTTP_ORIGIN="https://attacker.invalid")
    assert response.status_code == 403
    response = client.post("/api/v1/auth/login/", payload, format="json",
                           HTTP_X_CSRFTOKEN="a" * 64)
    assert response.status_code == 403


def test_login_throttle_is_scoped(client, user, monkeypatch):
    from apps.identity.permissions import LoginThrottle

    monkeypatch.setattr(LoginThrottle, "THROTTLE_RATES", {"login": "2/min"})
    for _ in range(2):
        assert post(client, "auth/login", {"username": user.username, "password": "wrong"}).status_code == 401
    response = post(client, "auth/login", {"username": user.username, "password": PASSWORD})
    assert response.status_code == 429
    assert "Retry-After" in response


def test_password_change_keeps_current_session_and_invalidates_other(client, user):
    other = APIClient(enforce_csrf_checks=True)
    credentials = {"username": user.username, "password": PASSWORD}
    assert post(client, "auth/login", credentials).status_code == 200
    assert post(other, "auth/login", credentials).status_code == 200
    response = post(client, "auth/password", {
        "current_password": PASSWORD, "new_password": "Different-Secret-Phrase!94",
    })
    assert response.status_code == 200, response.json()
    assert client.get("/api/v1/auth/me/").status_code == 200
    assert other.get("/api/v1/auth/me/").status_code == 403
    user.refresh_from_db()
    assert user.check_password("Different-Secret-Phrase!94")
    event = AuditEvent.objects.get(action="auth.password_changed")
    assert PASSWORD not in json.dumps(event.changes)
    assert "Different-Secret" not in json.dumps(event.changes)


@pytest.mark.parametrize("locale", ["en", "ar", "de", "es"])
def test_preferences_persist_and_messages_translate(client, user, locale):
    client.force_login(user)
    response = patch(client, "auth/preferences", {"locale": locale})
    assert response.status_code == 200
    user.refresh_from_db()
    assert user.locale == locale
    assert AuditEvent.objects.filter(action="auth.preferences_changed", actor=user).exists()
    response = post(client, "auth/password", {"current_password": "wrong", "new_password": PASSWORD},
                    HTTP_ACCEPT_LANGUAGE=locale)
    error = response.json()["errors"][0]
    assert error["code"] == "invalid_password"
    if locale == "en":
        assert error["message"] == "The current password is incorrect."
    else:
        assert error["message"] != "The current password is incorrect."
    assert client.get("/api/v1/auth/me/").json()["data"]["locale"] == locale


def test_saved_locale_used_without_header_and_explicit_header_wins(client, user):
    user.locale = "de"
    user.save()
    client.force_login(user)
    data = {"current_password": "wrong", "new_password": PASSWORD}
    saved = post(client, "auth/password", data).json()["errors"][0]["message"]
    explicit = post(client, "auth/password", data, HTTP_ACCEPT_LANGUAGE="en").json()["errors"][0]["message"]
    assert saved != explicit
    assert explicit == "The current password is incorrect."


def test_group_create_edit_membership_audit_and_global_revisions(administrator, user):
    result = CreateGroup.execute(administrator, CreateGroupCommand(
        "Funding", ("wallets.fund",), json.dumps({"schema_version": 1, "scopes": {
            "wallets.fund": {"ownership": "own", "currencies": ["USDT"]},
        }}), "Approved funding access",
    ), request_id="request-42")
    UpdateMembership.execute(administrator, MembershipCommand(user.pk, "Assign group", (result.id,)))
    assert AuthorizationPolicy.allows(user, "wallets.fund", currency="USDT")
    assert not AuthorizationPolicy.allows(user, "wallets.fund", currency="EGP")
    before = dict(User.objects.values_list("pk", "authorization_revision"))
    UpdateGroup.execute(administrator, PatchGroupCommand(result.id, "Remove funding", permissions=()))
    assert not AuthorizationPolicy.allows(user, "wallets.fund", currency="USDT")
    assert all(revision == before[pk] + 1 for pk, revision in
               User.objects.values_list("pk", "authorization_revision"))
    event = AuditEvent.objects.get(action="access.group.created", target_id=str(result.id))
    assert event.reason == "Approved funding access"
    assert event.request_id == "request-42"
    assert event.changes["after"]["permissions"] == ["wallets.fund"]


def test_last_admin_membership_and_deactivation_are_rolled_back(administrator):
    for command in [MembershipCommand(administrator.pk, "Remove", ()),
                    MembershipCommand(administrator.pk, "Deactivate", is_active=False)]:
        with pytest.raises(DomainError) as failure:
            UpdateMembership.execute(administrator, command)
        assert failure.value.code == "last_access_admin"
        assert AuthorizationPolicy.allows(administrator, "access.manage")
    assert not AuditEvent.objects.filter(action="access.membership.updated").exists()


def test_last_admin_permission_and_deny_edits_are_rolled_back(administrator):
    group = administrator.groups.get()
    for command in [PatchGroupCommand(group.pk, "Revoke", permissions=()), PatchGroupCommand(
        group.pk, "Deny", attributes_json=json.dumps({"schema_version": 1,
            "scopes": {"access.manage": {"ownership": "all"}}, "deny_actions": ["access.manage"]}),
    )]:
        with pytest.raises(DomainError) as failure:
            UpdateGroup.execute(administrator, command)
        assert failure.value.code == "last_access_admin"
        assert AuthorizationPolicy.allows(administrator, "access.manage")


def test_superuser_cannot_rescue_last_effective_admin(administrator):
    User.objects.create_superuser(username="flags-only", password=PASSWORD)
    with pytest.raises(DomainError, match="last active"):
        UpdateMembership.execute(administrator, MembershipCommand(administrator.pk, "Remove", ()))


def test_access_changes_require_reason_and_reauthorize(administrator, user):
    with pytest.raises(DomainError) as failure:
        UpdateMembership.execute(administrator, MembershipCommand(user.pk, "  ", ()))
    assert failure.value.code == "reason_required"
    administrator.groups.clear()
    with pytest.raises(DomainError) as failure:
        UpdateMembership.execute(administrator, MembershipCommand(user.pk, "Stale grant", ()))
    assert failure.value.code == "permission_denied"


def test_access_revocation_is_atomic_and_failure_rolls_back(administrator, user, monkeypatch):
    from apps.trading.services import AccessRevocation

    before = dict(User.objects.values_list("pk", "authorization_revision"))

    def fail():
        assert connection.in_atomic_block
        assert not User.objects.get(pk=user.pk).is_active
        raise RuntimeError("release failed")

    monkeypatch.setattr(AccessRevocation, "cancel_unauthorized", fail)
    with pytest.raises(RuntimeError, match="release failed"):
        UpdateMembership.execute(administrator, MembershipCommand(user.pk, "Deactivate", is_active=False))
    user.refresh_from_db()
    assert user.is_active
    assert dict(User.objects.values_list("pk", "authorization_revision")) == before
    assert not AuditEvent.objects.filter(action="access.membership.updated").exists()


def test_owner_membership_edit_invalidates_unrelated_auditor(administrator, user, group_factory):
    owner_group = group_factory("owners")
    user.groups.add(owner_group)
    audit_group = group_factory("audit", ["audit.read"], {
        "audit.read": {"ownership": "owner_groups", "owner_group_ids": [owner_group.pk]},
    })
    auditor = User.objects.create_user(username="auditor")
    auditor.groups.add(audit_group)
    assert AuthorizationPolicy.allows(auditor, "audit.read", owner_id=user.pk)
    revision = auditor.authorization_revision
    UpdateMembership.execute(administrator, MembershipCommand(user.pk, "Owner moved", ()))
    auditor.refresh_from_db()
    assert auditor.authorization_revision == revision + 1
    assert not AuthorizationPolicy.allows(auditor, "audit.read", owner_id=user.pk)


def test_deactivation_deletes_sessions_and_reactivation_does_not_restore(client, administrator, user):
    client.force_login(user)
    UpdateMembership.execute(administrator, MembershipCommand(user.pk, "Suspend", is_active=False))
    UpdateMembership.execute(administrator, MembershipCommand(user.pk, "Restore", is_active=True))
    assert client.get("/api/v1/auth/me/").status_code == 403


def test_admin_api_authorization_pagination_and_validation(client, administrator, user, group_factory):
    client.force_login(user)
    assert client.get("/api/v1/administration/users/").status_code == 403
    assert patch(client, f"administration/users/{user.pk}/groups", {
        "group_ids": list(administrator.groups.values_list("pk", flat=True)), "reason": "Escalate",
    }).status_code == 403
    client.force_login(administrator)
    for index in range(4):
        group_factory(f"paged-{index}")
    result = client.get("/api/v1/administration/groups/?page_size=2").json()
    assert len(result["data"]) == 2
    assert result["metadata"]["pagination"]["count"] == 5
    assert client.get("/api/v1/administration/users/?page_size=500").json()["metadata"]["pagination"]["page_size"] == 100
    payload = {"name": "API group", "permissions": ["events.read"], "attributes": {
        "schema_version": 1, "scopes": {"events.read": {"ownership": "own"}},
    }, "reason": "Approved"}
    created = post(client, "administration/groups", payload)
    assert created.status_code == 201
    group_id = created.json()["data"]["id"]
    assert client.get(f"/api/v1/administration/groups/{group_id}/").status_code == 200
    assert patch(client, f"administration/groups/{group_id}", {"name": "Renamed"}).status_code == 400
    assert patch(client, f"administration/groups/{group_id}", {
        "name": "Renamed", "reason": "Clarify label",
    }).status_code == 200
    assert patch(client, f"administration/users/{user.pk}/groups", {
        "group_ids": [group_id], "is_active": False, "reason": "Suspend",
    }).status_code == 200


def test_capabilities_expose_scopes_and_schema_without_claiming_elevation(client, user, group_factory):
    user.groups.add(group_factory("events", ["events.read"], {"events.read": {"ownership": "own"}}))
    client.force_login(user)
    result = client.get("/api/v1/auth/capabilities/")
    assert result.status_code == 200
    data = result.json()["data"]
    assert data["actions"] == {"events.read": [{"ownership": "own"}]}
    assert data["policy_schema"]["actions"]["access.manage"]["ownership"] == ["all"]
    assert data["authorization_revision"] == user.authorization_revision


def test_bootstrap_is_editable_and_does_not_reset_templates(bootstrapped):
    trader = Group.objects.get(name="Trader")
    trader.permissions.clear()
    policy = trader.policy
    policy.attributes = {"schema_version": 1, "scopes": {}, "deny_actions": ["trading.place"]}
    policy.save()
    BootstrapIdentity.execute()
    trader.refresh_from_db()
    assert not trader.permissions.exists()
    assert trader.policy.attributes["deny_actions"] == ["trading.place"]
    assert User.objects.count() == 0


def test_bootstrap_admin_requires_explicit_valid_credentials(monkeypatch):
    from django.core.management.base import CommandError

    for name in ("STOCKMAN_ADMIN_USERNAME", "STOCKMAN_ADMIN_PASSWORD", "STOCKMAN_ADMIN_EMAIL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(CommandError):
        call_command("bootstrap", username="local-admin")
    assert not User.objects.exists()
    call_command("bootstrap", username="local-admin", password=PASSWORD)
    admin = User.objects.get(username="local-admin")
    assert admin.check_password(PASSWORD)
    assert AuthorizationPolicy.allows(admin, "access.manage")
    assert not admin.is_superuser


def test_identity_catalogs_have_matching_keys_and_real_translations():
    from babel.messages.pofile import read_po

    root = Path(__file__).resolve().parents[1] / "apps" / "identity" / "locale"
    catalogs = []
    for language in ("ar", "de", "es"):
        with (root / language / "LC_MESSAGES" / "django.po").open("rb") as stream:
            catalog = read_po(stream, locale=language)
        messages = {message.id: message.string for message in catalog if message.id}
        assert all(messages.values())
        catalogs.append(set(messages))
        with translation.override(language):
            assert translation.gettext("Invalid username or password.") != "Invalid username or password."
    assert catalogs[0] == catalogs[1] == catalogs[2]
