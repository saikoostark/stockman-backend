"""Finite, fail-closed RBAC/ABAC; grants and constraints never cross groups."""

from dataclasses import dataclass
from types import MappingProxyType

from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils.translation import gettext as _

from apps.common.exceptions import DomainError

from .models import GroupPolicy, User


@dataclass(frozen=True)
class ActionSpec:
    ownership: tuple[str, ...]
    markets: bool = False
    currencies: bool = False


ACTIONS = MappingProxyType({
    "markets.read": ActionSpec(("all",), True, True),
    "wallets.read": ActionSpec(("own",), currencies=True),
    "wallets.fund": ActionSpec(("own",), currencies=True),
    "trading.read": ActionSpec(("own",), True, True),
    "trading.place": ActionSpec(("own",), True, True),
    "trading.cancel": ActionSpec(("own",), True, True),
    "alerts.read": ActionSpec(("own",), True, True),
    "alerts.manage": ActionSpec(("own",), True, True),
    "events.read": ActionSpec(("own",)),
    "audit.read": ActionSpec(("own", "owner_groups", "all"), True, True),
    "access.manage": ActionSpec(("all",)),
    "catalog.manage": ActionSpec(("all",), True, True),
})
MARKETS = ("CRYPTO", "EGX")
CURRENCIES = ("USDT", "EGP")


def permission_codename(action):
    return action.replace(".", "_")


class PolicySchema:
    @staticmethod
    def validate(attributes):
        def invalid():
            raise ValidationError(_("Invalid group policy attributes."), code="invalid_policy")

        if not isinstance(attributes, dict) or set(attributes) - {
            "schema_version", "scopes", "deny_actions",
        }:
            invalid()
        if type(attributes.get("schema_version")) is not int or attributes["schema_version"] != 1:
            invalid()
        scopes = attributes.get("scopes")
        denies = attributes.get("deny_actions", [])
        if not isinstance(scopes, dict) or len(scopes) > len(ACTIONS):
            invalid()
        if not isinstance(denies, list) or any(
            not isinstance(action, str) or action not in ACTIONS for action in denies
        ) or len(set(denies)) != len(denies):
            invalid()
        for action, scope in scopes.items():
            if action not in ACTIONS or not isinstance(scope, dict):
                invalid()
            spec = ACTIONS[action]
            keys = {"ownership", "owner_group_ids"}
            if spec.markets:
                keys.add("markets")
            if spec.currencies:
                keys.add("currencies")
            if set(scope) - keys or scope.get("ownership") not in spec.ownership:
                invalid()
            owner_groups = scope.get("owner_group_ids", [])
            if not isinstance(owner_groups, list) or len(owner_groups) > 100 or any(
                type(value) is not int or value <= 0 for value in owner_groups
            ) or len(set(owner_groups)) != len(owner_groups):
                invalid()
            if scope["ownership"] == "owner_groups":
                if not owner_groups:
                    invalid()
            elif owner_groups:
                invalid()
            for field, values in (("markets", MARKETS), ("currencies", CURRENCIES)):
                if field not in scope:
                    continue
                allowed = scope[field]
                if not isinstance(allowed, list) or any(
                    not isinstance(value, str) or value not in values for value in allowed
                ) or len(set(allowed)) != len(allowed):
                    invalid()
        return attributes

    @staticmethod
    def describe():
        return {
            "schema_version": 1,
            "markets": MARKETS,
            "currencies": CURRENCIES,
            "actions": {
                action: {"ownership": spec.ownership, "markets": spec.markets,
                         "currencies": spec.currencies}
                for action, spec in ACTIONS.items()
            },
        }


class AuthorizationPolicy:
    @staticmethod
    def _grants(user, action):
        # Never trust a prefetched membership set, cached has_perm(), or stale is_active.
        if action not in ACTIONS or not getattr(user, "is_authenticated", False):
            return ()
        if not User.objects.filter(pk=user.pk, is_active=True).exists():
            return ()
        groups = Group.objects.filter(user=user).select_related("policy").prefetch_related(
            "permissions__content_type"
        )
        grants = []
        for group in groups:
            try:
                attributes = group.policy.attributes
                PolicySchema.validate(attributes)
            except (GroupPolicy.DoesNotExist, ValidationError):
                # Corrupt or unknown memberships cannot accidentally drop a veto.
                return ()
            if action in attributes.get("deny_actions", []):
                return ()
            scope = attributes["scopes"].get(action)
            if scope is not None and any(
                permission.codename == permission_codename(action)
                and permission.content_type.app_label == "identity"
                and permission.content_type.model == "grouppolicy"
                for permission in group.permissions.all()
            ):
                grants.append(scope)
        return tuple(grants)

    @classmethod
    def allows(cls, user, action, *, owner_id=None, market=None, currency=None):
        for scope in cls._grants(user, action):
            ownership = scope["ownership"]
            target = user.pk if owner_id is None else owner_id
            if ownership == "own" and target != user.pk:
                continue
            if ownership == "owner_groups" and not User.objects.filter(
                pk=target, groups__pk__in=scope["owner_group_ids"]
            ).exists():
                continue
            if "markets" in scope and market not in scope["markets"]:
                continue
            if "currencies" in scope and currency not in scope["currencies"]:
                continue
            return True
        return False

    @classmethod
    def require(cls, user, action, *, owner_id=None, market=None, currency=None):
        if not cls.allows(user, action, owner_id=owner_id, market=market, currency=currency):
            raise DomainError("permission_denied", _("You do not have permission for this action."), status=403)

    @classmethod
    def scope(cls, user, action, queryset, owner_field="user_id", market_field=None,
              currency_field=None):
        allowed = Q(pk__in=[])
        for scope in cls._grants(user, action):
            clause = Q()
            if scope["ownership"] == "own":
                if not owner_field:
                    continue
                clause &= Q(**{owner_field: user.pk})
            elif scope["ownership"] == "owner_groups":
                if not owner_field:
                    continue
                owners = User.objects.filter(groups__pk__in=scope["owner_group_ids"]).values("pk")
                clause &= Q(**{f"{owner_field}__in": owners})
            if "markets" in scope:
                if not market_field:
                    continue
                clause &= Q(**{f"{market_field}__in": scope["markets"]})
            if "currencies" in scope:
                if not currency_field:
                    continue
                clause &= Q(**{f"{currency_field}__in": scope["currencies"]})
            if not clause.children:
                return queryset.all()
            allowed |= clause
        return queryset.filter(allowed).distinct()

    @classmethod
    def capabilities(cls, user):
        # Hints expose complete alternatives, not an unsafe flattened union.
        return {action: scopes for action in ACTIONS if (scopes := cls._grants(user, action))}
