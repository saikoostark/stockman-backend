from datetime import timedelta

import pytest
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from rest_framework.test import APIClient

from apps.common.exceptions import DomainError
from apps.identity.models import GroupPolicy, User
from apps.markets.dtos import CatalogCommand
from apps.markets.models import Instrument, PriceObservation, ProviderHealth, Quote, Watchlist
from apps.markets.services import UpdateCatalog

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def market_urls(settings):
    settings.ROOT_URLCONF = "apps.markets.urls"
    settings.EGX_ACCESS_VERIFIED = False
    settings.EODHD_API_TOKEN = ""
    settings.EGX_SYMBOLS = []
    settings.BINANCE_SYMBOLS = ["BTCUSDT"]


@pytest.fixture
def users():
    content_type = ContentType.objects.get_for_model(GroupPolicy)
    def create(name, *, markets=None, admin=False):
        actor = User.objects.create_user(username=name, password="market-test-password")
        group = Group.objects.create(name=f"market-test-{name}")
        actions = ["markets.read"] + (["catalog.manage"] if admin else [])
        for action in actions:
            permission, _ = Permission.objects.get_or_create(content_type=content_type, codename=action.replace(".", "_"), defaults={"name": action})
            group.permissions.add(permission)
        scope = {"ownership": "all"}
        if markets is not None:
            scope["markets"] = markets
        GroupPolicy.objects.create(group=group, attributes={"schema_version": 1, "scopes": {action: scope for action in actions}})
        actor.groups.add(group)
        return actor
    return create


@pytest.fixture
def instruments():
    crypto = Instrument.objects.create(symbol="BTCUSDT", name="Bitcoin / USDT", market="CRYPTO", currency="USDT", provider="BINANCE", provider_symbol="BTCUSDT", price_scale=2, quantity_scale=3, quantity_step=1, minimum_quantity=1, minimum_notional=500000000)
    egx = Instrument.objects.create(symbol="COMI", name="Commercial International Bank", market="EGX", currency="EGP", provider="EODHD", provider_symbol="COMI.EGX", price_scale=2, quantity_scale=0, quantity_step=1, minimum_quantity=1, minimum_notional=0, data_mode="EOD", cadence_seconds=86400, venue_timezone="Africa/Cairo")
    ProviderHealth.objects.create(provider="BINANCE", status="AVAILABLE", reason="", last_success_at=timezone.now())
    return crypto, egx


def client_for(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


def test_instrument_list_is_scoped_before_counting_and_filtering(users, instruments):
    client = client_for(users("crypto", markets=["CRYPTO"]))
    response = client.get("/instruments/?page_size=500")
    assert response.status_code == 200
    body = response.json()
    assert body["metadata"]["pagination"]["count"] == 1
    assert body["metadata"]["pagination"]["page_size"] == 100
    assert [row["symbol"] for row in body["data"]] == ["BTCUSDT"]
    assert client.get("/instruments/?market=EGX").json()["data"] == []
    assert client.get(f"/instruments/{instruments[1].id}/").status_code == 404
    assert client.get("/instruments/?currency=USDT&search=bitcoin").json()["data"][0]["symbol"] == "BTCUSDT"
    assert client.get("/instruments/?market=INVALID").status_code == 400


def test_quote_contract_uses_decimal_strings_and_original_source_age(users, instruments):
    instrument = instruments[0]
    old = timezone.now() - timedelta(minutes=2)
    Quote.objects.create(instrument=instrument, price_units=12345, source_at=old, received_at=timezone.now(), observation_id="old-tick", tradable=True)
    response = client_for(users("reader")).get(f"/instruments/{instrument.id}/")
    data = response.json()["data"]
    assert data["quote"]["price"] == "123.45"
    assert data["quote"]["stale"]
    assert not data["quote"]["tradable"]
    assert data["quote"]["source"] == "BINANCE"
    assert data["quote"]["currency"] == "USDT"
    assert data["quantity_step"] == "0.001"
    assert data["minimum_notional"] == "5.00000000"
    assert set(response.json()) == {"data", "metadata", "errors"}


def test_provider_status_honestly_disables_egx_without_credentials(users):
    response = client_for(users("reader")).get("/providers/")
    assert response.status_code == 200
    provider = next(row for row in response.json()["data"] if row["provider"] == "EODHD")
    assert provider["status"] == "UNAVAILABLE"
    assert provider["reason"] == "EGX_ACCESS_NOT_VERIFIED"
    assert not provider["entitlement_verified"]
    assert "not been verified" in provider["message"]
    assert "api_token" not in response.content.decode()


def test_watchlists_are_owner_only_even_for_all_scope(users, instruments):
    owner, other = users("owner"), users("other")
    one, two = client_for(owner), client_for(other)
    instrument = instruments[0]
    response = one.post("/watchlist/", {"instrument_id": str(instrument.id), "user_id": other.id}, format="json")
    assert response.status_code == 201
    assert Watchlist.objects.get().user_id == owner.id
    assert two.get("/watchlist/").json()["data"] == []
    assert two.delete(f"/watchlist/{instrument.id}/").status_code == 200
    assert Watchlist.objects.filter(user=owner).exists()
    assert one.post("/watchlist/", {"instrument_id": str(instrument.id)}, format="json").status_code == 201
    assert Watchlist.objects.count() == 1
    assert one.delete("/watchlist/", {"instrument_id": str(instrument.id)}, format="json").status_code == 200
    assert not Watchlist.objects.exists()


def test_watchlist_policy_rechecks_current_market_grant(users, instruments):
    actor = users("crypto", markets=["CRYPTO"])
    client = client_for(actor)
    assert client.post("/watchlist/", {"instrument_id": str(instruments[1].id)}, format="json").status_code == 403
    assert not Watchlist.objects.exists()


def test_history_is_bounded_and_scoped_without_provider_requests(users, instruments):
    now = timezone.now()
    for i in range(4):
        PriceObservation.objects.create(instrument=instruments[0], price_units=10000 + i, source_at=now - timedelta(seconds=i), received_at=now, observation_id=f"tick:{i}", tradable=False)
    client = client_for(users("crypto", markets=["CRYPTO"]))
    response = client.get(f"/instruments/{instruments[0].id}/history/?page_size=2")
    assert response.status_code == 200
    assert response.json()["metadata"]["pagination"]["count"] == 4
    assert len(response.json()["data"]) == 2
    assert response.json()["data"][0]["price"] == "100.00"
    assert client.get(f"/instruments/{instruments[1].id}/history/").status_code == 404
    assert client.get(f"/instruments/{instruments[0].id}/history/?kind=candles").json()["data"] == []


def test_catalog_mutations_require_policy_and_record_audit(users, instruments):
    from apps.audit.models import AuditEvent
    instrument = instruments[0]
    denied = client_for(users("reader"))
    response = denied.patch(f"/instruments/{instrument.id}/", {"enabled": False}, format="json")
    assert response.status_code == 403
    instrument.refresh_from_db()
    assert instrument.enabled
    admin = client_for(users("manager", admin=True))
    response = admin.patch(f"/instruments/{instrument.id}/", {"name": "Bitcoin", "enabled": False, "watched": False, "reason": "Catalog review"}, format="json")
    assert response.status_code == 200
    instrument.refresh_from_db()
    assert not instrument.enabled and not instrument.watched
    assert instrument.name == "Bitcoin"
    assert AuditEvent.objects.filter(action="catalog.updated", target_id=str(instrument.id)).exists()


def test_catalog_service_cannot_reenable_a_metadata_halt(users, instruments):
    actor = users("manager", admin=True)
    instrument = instruments[0]
    instrument.enabled = False
    instrument.halted_reason = "METADATA_CHANGED"
    instrument.save()
    with pytest.raises(DomainError) as exc:
        UpdateCatalog.execute(actor, CatalogCommand(instrument.id, enabled=True))
    assert exc.value.code == "instrument_halted"


def test_market_endpoints_require_authentication(instruments):
    client = APIClient()
    assert client.get("/instruments/").status_code == 403
    assert client.get("/watchlist/").status_code == 403
