from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from drf_spectacular.generators import SchemaGenerator
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.alerts.dtos import CreateAlertCommand, DeleteAlertCommand, UpdateAlertCommand
from apps.alerts.models import AlertRule
from apps.alerts.policies import AlertConfigurationPolicy
from apps.alerts.selectors import AlertSelector
from apps.alerts.services import CreateAlert, DeleteAlert, EvaluateAlerts, UpdateAlert
from apps.alerts.urls import urlpatterns
from apps.alerts.views import AlertDetailView, AlertListView
from apps.audit.models import AuditEvent
from apps.audit.services import AuditRecorder
from apps.common.exceptions import DomainError
from apps.events.models import Notification, UserEvent
from apps.events.services import EventPublisher, NotificationPublisher
from apps.identity.models import GroupPolicy
from apps.markets.models import Instrument, PriceObservation, Quote

pytestmark = pytest.mark.django_db


@pytest.fixture
def alert_grant():
    def grant(user, *, markets=None, currencies=None):
        group = Group.objects.create(name=f"alerts-{uuid4().hex}")
        scopes = {}
        content_type = ContentType.objects.get_for_model(GroupPolicy)
        for action in ("alerts.read", "alerts.manage"):
            permission, _ = Permission.objects.get_or_create(
                content_type=content_type, codename=action.replace(".", "_"),
                defaults={"name": action},
            )
            group.permissions.add(permission)
            scopes[action] = {
                "ownership": "own", "markets": markets or ["CRYPTO"],
                "currencies": currencies or ["USDT"],
            }
        GroupPolicy.objects.create(group=group, attributes={"schema_version": 1, "scopes": scopes})
        user.groups.add(group)
        return group
    return grant


@pytest.fixture
def alert_user(django_user_model, alert_grant):
    user = django_user_model.objects.create_user(username="alert-owner")
    alert_grant(user)
    return user


@pytest.fixture
def alert_instrument():
    return Instrument.objects.create(
        symbol="ALERTUSDT", name="Alert test asset", market="CRYPTO", currency="USDT",
        provider="test", provider_symbol="ALERTUSDT", price_scale=8, quantity_scale=8,
        quantity_step=1, minimum_quantity=1, minimum_notional=1,
        cadence_seconds=10, delay_seconds=0, data_mode="LIVE", enabled=True,
    )


@pytest.fixture
def alert_clock():
    class Clock:
        now = datetime(2026, 1, 5, 12, tzinfo=UTC)

        def __call__(self):
            return self.now

        def advance(self, seconds=10):
            self.now += timedelta(seconds=seconds)
    return Clock()


@pytest.fixture
def observe(alert_instrument, alert_clock):
    generation = uuid4()

    def create(value, *, source_at=None, received_at=None, history_only=False,
               tradable=True, observation_id=None, generation_id=None):
        source_at = source_at or alert_clock.now
        values = {
            "price_units": int(Decimal(value).scaleb(alert_instrument.price_scale)),
            "source_at": source_at, "received_at": received_at or source_at,
            "generation": generation_id or generation, "tradable": tradable,
            "observation_id": observation_id or uuid4().hex,
        }
        observation = PriceObservation.objects.create(instrument=alert_instrument, **values)
        if not history_only:
            Quote.objects.update_or_create(instrument=alert_instrument, defaults=values)
        return observation
    return create


@pytest.fixture
def make_alert(alert_user, alert_instrument):
    def create(**kwargs):
        values = {"instrument_id": alert_instrument.id, "kind": "PRICE_ABOVE", "threshold": "100",
                  "cooldown_seconds": 0, **kwargs}
        result = CreateAlert.execute(alert_user, CreateAlertCommand(**values), request_id="alert-test")
        return AlertRule.objects.get(pk=result.id)
    return create


def evaluate(clock):
    return EvaluateAlerts.run(clock=clock)


def call_view(user, method, data=None, alert_id=None, query=""):
    path = f"/alerts/{alert_id}/" if alert_id else "/alerts/"
    request = getattr(APIRequestFactory(), method)(path + query, data=data, format="json")
    request.request_id = "alert-api-test"
    force_authenticate(request, user=user)
    if alert_id:
        return AlertDetailView.as_view()(request, alert_id=alert_id)
    return AlertListView.as_view()(request)


@pytest.mark.parametrize("kind,initial,equality,crossing", [
    ("PRICE_ABOVE", "90", "100", "100.00000001"),
    ("PRICE_BELOW", "110", "100", "99.99999999"),
])
def test_price_crosses_only_after_initialization(make_alert, observe, alert_clock,
                                                 kind, initial, equality, crossing):
    rule = make_alert(kind=kind)
    observe(initial)
    assert evaluate(alert_clock) == 0
    alert_clock.advance()
    observe(equality)
    assert evaluate(alert_clock) == 0
    alert_clock.advance()
    observe(crossing)
    assert evaluate(alert_clock) == 1
    assert evaluate(alert_clock) == 0
    rule.refresh_from_db()
    assert rule.last_condition is True
    assert rule.last_triggered_at == alert_clock.now
    notification = Notification.objects.get()
    assert notification.user_id == rule.user_id
    assert notification.translation_key == f"alerts.{kind.lower()}"
    assert notification.parameters == {
        "symbol": "ALERTUSDT", "value": crossing, "threshold": "100", "baseline_at": "", "window": "",
    }
    assert UserEvent.objects.filter(user_id=rule.user_id, kind="notification.created").count() == 1


def test_initial_true_is_silent_then_false_rearms(make_alert, observe, alert_clock):
    make_alert()
    for value, count in [("110", 0), ("120", 0), ("90", 0), ("110", 1)]:
        observe(value)
        assert evaluate(alert_clock) == count
        alert_clock.advance()
    assert Notification.objects.count() == 1


def test_cooldown_suppresses_crossing_without_deferred_delivery(make_alert, observe, alert_clock):
    rule = make_alert(cooldown_seconds=60)
    for value in ["90", "110", "90", "110"]:
        observe(value)
        evaluate(alert_clock)
        alert_clock.advance()
    rule.refresh_from_db()
    assert rule.state_reason == "cooldown"
    assert rule.last_condition is True
    assert Notification.objects.count() == 1
    for _ in range(8):
        observe("110")
        assert evaluate(alert_clock) == 0
        alert_clock.advance()
    observe("90")
    assert evaluate(alert_clock) == 0
    alert_clock.advance()
    observe("110")
    assert evaluate(alert_clock) == 1
    assert Notification.objects.count() == 2


@pytest.mark.parametrize("kind,initial,current,value", [
    ("PERCENT_RISE", "105", "110", "10"),
    ("PERCENT_DROP", "95", "90", "10"),
])
def test_percentage_uses_scaled_history_and_actual_baseline(make_alert, observe, alert_clock,
                                                           kind, initial, current, value):
    rule = make_alert(kind=kind, threshold="10", window_seconds=60)
    baseline = observe("100", source_at=alert_clock.now - timedelta(seconds=60), history_only=True,
                       tradable=False)
    observe(initial)
    assert evaluate(alert_clock) == 0
    alert_clock.advance()
    observe(current)
    assert evaluate(alert_clock) == 1
    rule.refresh_from_db()
    assert rule.baseline_at == baseline.source_at
    assert Notification.objects.get().parameters == {
        "symbol": "ALERTUSDT", "value": value, "threshold": "10",
        "baseline_at": baseline.source_at.isoformat(), "window": "60",
    }


def test_baseline_rolls_and_uses_only_latest_at_or_before_target(make_alert, observe, alert_clock):
    rule = make_alert(kind="PERCENT_RISE", threshold="10", window_seconds=60)
    observe("100", source_at=alert_clock.now - timedelta(seconds=60), history_only=True)
    later = observe("110", source_at=alert_clock.now - timedelta(seconds=50), history_only=True)
    observe("105")
    assert evaluate(alert_clock) == 0
    alert_clock.advance()
    observe("120")  # 9.09% from the new baseline, not 20% from the old one.
    assert evaluate(alert_clock) == 0
    rule.refresh_from_db()
    assert rule.baseline_at == later.source_at
    alert_clock.advance()
    observe("122")
    assert evaluate(alert_clock) == 1


def test_percent_comparison_does_not_round_before_crossing(make_alert, observe, alert_clock):
    make_alert(kind="PERCENT_RISE", threshold="0.33333334", window_seconds=60)
    observe("3", source_at=alert_clock.now - timedelta(seconds=60), history_only=True)
    observe("3")
    assert evaluate(alert_clock) == 0
    alert_clock.advance()
    observe("3.01")
    assert evaluate(alert_clock) == 0


def test_missing_or_old_history_recovers_without_delivery(make_alert, observe, alert_clock):
    rule = make_alert(kind="PERCENT_RISE", threshold="10", window_seconds=60)
    observe("100", source_at=alert_clock.now - timedelta(seconds=71), history_only=True)
    quote = observe("120")
    assert evaluate(alert_clock) == 0
    rule.refresh_from_db()
    assert rule.state_reason == "insufficient_history"
    assert rule.last_observation_id == quote.observation_id
    # Late history is not permission to deliver the already-consumed quote.
    observe("100", source_at=alert_clock.now - timedelta(seconds=60), history_only=True)
    assert evaluate(alert_clock) == 0
    alert_clock.advance()
    observe("120")
    assert evaluate(alert_clock) == 0
    rule.refresh_from_db()
    assert rule.last_condition is True
    assert not Notification.objects.exists()


def test_zero_baseline_is_suspended_without_division(make_alert, observe, alert_clock, monkeypatch):
    rule = make_alert(kind="PERCENT_RISE", threshold="10", window_seconds=60)
    observe("110")
    monkeypatch.setattr("apps.alerts.evaluation.AlertHistorySelector.baseline",
                        lambda *args: SimpleNamespace(price_units=0, source_at=alert_clock.now))
    assert evaluate(alert_clock) == 0
    rule.refresh_from_db()
    assert rule.state_reason == "invalid_baseline"


def test_stale_quote_resets_state_and_recovery_is_quiet(make_alert, observe, alert_clock):
    rule = make_alert()
    observe("90")
    evaluate(alert_clock)
    alert_clock.advance(31)
    assert evaluate(alert_clock) == 0
    rule.refresh_from_db()
    assert rule.state_reason == "stale_quote"
    assert rule.last_condition is None
    observe("110")
    assert evaluate(alert_clock) == 0
    alert_clock.advance()
    observe("90")
    evaluate(alert_clock)
    alert_clock.advance()
    observe("110")
    assert evaluate(alert_clock) == 1


def test_unobserved_worker_gap_also_reinitializes(make_alert, observe, alert_clock):
    make_alert()
    observe("90")
    evaluate(alert_clock)
    alert_clock.advance(120)
    observe("110")
    assert evaluate(alert_clock) == 0


def test_nontradable_observation_cannot_be_replayed_as_live(make_alert, observe, alert_clock):
    rule = make_alert()
    observe("90")
    evaluate(alert_clock)
    alert_clock.advance()
    observe("110", tradable=False)
    assert evaluate(alert_clock) == 0
    Quote.objects.update(tradable=True)
    assert evaluate(alert_clock) == 0
    alert_clock.advance()
    observe("120")
    assert evaluate(alert_clock) == 0
    rule.refresh_from_db()
    assert rule.last_condition is True


def test_generation_change_reestablishes_state(make_alert, observe, alert_clock):
    rule = make_alert()
    observe("90")
    evaluate(alert_clock)
    generation = uuid4()
    alert_clock.advance()
    observe("110", generation_id=generation)
    assert evaluate(alert_clock) == 0
    rule.refresh_from_db()
    assert rule.generation == generation
    assert rule.last_condition is True
    alert_clock.advance()
    observe("90", generation_id=generation)
    evaluate(alert_clock)
    alert_clock.advance()
    observe("110", generation_id=generation)
    assert evaluate(alert_clock) == 1


def test_out_of_order_and_repeated_identity_are_ignored(make_alert, observe, alert_clock):
    rule = make_alert()
    first = observe("90")
    evaluate(alert_clock)
    alert_clock.advance()
    observe("110", source_at=first.source_at - timedelta(seconds=1))
    assert evaluate(alert_clock) == 0
    Quote.objects.update(price_units=11000000000, source_at=alert_clock.now,
                         received_at=alert_clock.now, observation_id=first.observation_id)
    assert evaluate(alert_clock) == 0
    rule.refresh_from_db()
    assert rule.last_condition is False
    assert rule.last_source_at == first.source_at
    alert_clock.advance()
    observe("110")
    assert evaluate(alert_clock) == 1


def test_future_quote_does_not_poison_watermark(make_alert, observe, alert_clock):
    make_alert()
    observe("110", source_at=alert_clock.now + timedelta(days=10))
    assert evaluate(alert_clock) == 0
    observe("90")
    assert evaluate(alert_clock) == 0
    alert_clock.advance()
    observe("110")
    assert evaluate(alert_clock) == 1


@pytest.mark.parametrize("field,value", [("enabled", False), ("is_active", False)])
def test_disabled_instrument_or_owner_recovery_is_silent(make_alert, observe, alert_clock,
                                                         alert_instrument, alert_user, field, value):
    rule = make_alert()
    observe("90")
    evaluate(alert_clock)
    target = alert_instrument if field == "enabled" else alert_user
    setattr(target, field, value)
    target.save(update_fields=[field])
    alert_clock.advance()
    observe("110")
    assert evaluate(alert_clock) == 0
    rule.refresh_from_db()
    assert rule.last_condition is None
    setattr(target, field, True)
    target.save(update_fields=[field])
    alert_clock.advance()
    observe("120")
    assert evaluate(alert_clock) == 0


def test_policy_revocation_is_checked_and_recovery_is_silent(make_alert, observe, alert_clock,
                                                            alert_user):
    rule = make_alert()
    observe("90")
    evaluate(alert_clock)
    group = alert_user.groups.get()
    alert_user.groups.clear()
    alert_clock.advance()
    observe("110")
    assert evaluate(alert_clock) == 0
    rule.refresh_from_db()
    assert rule.state_reason == "policy_denied"
    alert_user.groups.add(group)
    alert_clock.advance()
    observe("120")
    assert evaluate(alert_clock) == 0


def test_durable_notification_failure_rolls_back_evaluation(make_alert, observe, alert_clock,
                                                          monkeypatch):
    rule = make_alert()
    first = observe("90")
    evaluate(alert_clock)
    alert_clock.advance()
    observe("110")
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise RuntimeError("event unavailable")
        patch.setattr(EventPublisher, "publish", fail)
        with pytest.raises(RuntimeError):
            evaluate(alert_clock)
    assert not Notification.objects.exists()
    rule.refresh_from_db()
    assert rule.last_observation_id == first.observation_id
    assert rule.last_condition is False
    assert evaluate(alert_clock) == 1
    assert Notification.objects.count() == UserEvent.objects.count() == 1
    assert evaluate(alert_clock) == 0


def test_publisher_deduplicates_owner_event(alert_user):
    first = NotificationPublisher.publish(alert_user, "alerts.price_above", {}, "alert-test-key")
    second = NotificationPublisher.publish(alert_user, "alerts.price_above", {}, "alert-test-key")
    assert first.id == second.id
    assert UserEvent.objects.count() == 1


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity", "1e2", " 1", "1 ",
                                    "1.123456789", "92233720368.54775808", "1" * 33, 1.2, True])
def test_threshold_input_is_bounded_and_exact(make_alert, value):
    with pytest.raises(DomainError) as error:
        make_alert(threshold=value)
    assert error.value.code == "invalid_alert"
    assert not AlertRule.objects.exists()


def test_threshold_canonicalization_and_configured_cooldown(alert_user, alert_instrument, settings):
    settings.ALERT_COOLDOWN_SECONDS = 777
    result = CreateAlert.execute(alert_user, CreateAlertCommand(
        instrument_id=alert_instrument.id, kind="PRICE_ABOVE", threshold="000123.45000000",
    ))
    assert result.threshold == "123.45"
    assert result.cooldown_seconds == 777


@pytest.mark.parametrize("configuration", [
    {"window_seconds": 10},
    {"kind": "PERCENT_RISE", "window_seconds": None},
    {"kind": "PERCENT_RISE", "window_seconds": 9},
    {"kind": "PERCENT_RISE", "window_seconds": 31 * 86400},
    {"kind": "PERCENT_DROP", "window_seconds": 60, "threshold": "100.00000001"},
    {"kind": "PERCENT_RISE", "window_seconds": 60, "threshold": "1000001"},
    {"kind": "INVALID"}, {"cooldown_seconds": -1}, {"cooldown_seconds": True},
    {"cooldown_seconds": 366 * 86400}, {"enabled": "true"},
])
def test_invalid_configurations_rejected(make_alert, configuration, settings):
    settings.MARKET_HISTORY_DAYS = 30
    with pytest.raises(DomainError):
        make_alert(**configuration)


def test_eod_rejects_intraday_windows_and_allows_calendar_baseline(make_alert, observe, alert_clock,
                                                                  alert_instrument):
    alert_instrument.data_mode = "EOD"
    alert_instrument.cadence_seconds = 86400
    alert_instrument.save()
    for window in (3600, 90000):
        with pytest.raises(DomainError):
            make_alert(kind="PERCENT_RISE", threshold="10", window_seconds=window)
    rule = make_alert(kind="PERCENT_RISE", threshold="10", window_seconds=86400)
    baseline = observe("100", source_at=alert_clock.now - timedelta(days=3), history_only=True)
    observe("105")
    assert evaluate(alert_clock) == 0
    alert_clock.advance(86400)
    observe("120")
    assert evaluate(alert_clock) == 1
    rule.refresh_from_db()
    # The latest at/before yesterday is now the previous day's quote, not the weekend baseline.
    assert rule.baseline_at != baseline.source_at
    assert Notification.objects.get().parameters["window"] == "86400"


def test_delayed_source_time_is_accepted_only_within_declared_delay(make_alert, observe, alert_clock,
                                                                  alert_instrument):
    alert_instrument.data_mode = "DELAYED"
    alert_instrument.delay_seconds = 900
    alert_instrument.save()
    make_alert()
    observe("90", source_at=alert_clock.now - timedelta(seconds=900), received_at=alert_clock.now)
    assert evaluate(alert_clock) == 0
    alert_clock.advance()
    observe("110", source_at=alert_clock.now - timedelta(seconds=900), received_at=alert_clock.now)
    assert evaluate(alert_clock) == 1


def test_tenant_isolation_in_selectors_services_and_http(make_alert, alert_user,
                                                         django_user_model, alert_grant):
    rule = make_alert()
    other = django_user_model.objects.create_user(username="other-alert-owner")
    alert_grant(other)
    assert not AlertSelector.for_user(other).exists()
    for action in [lambda: AlertSelector.detail(other, rule.id),
                   lambda: UpdateAlert.execute(other, rule.id, UpdateAlertCommand(enabled=False)),
                   lambda: DeleteAlert.execute(other, DeleteAlertCommand(rule.id))]:
        with pytest.raises(DomainError) as error:
            action()
        assert error.value.status == 404
    assert call_view(other, "get", alert_id=rule.id).status_code == 404
    assert call_view(other, "patch", {"enabled": False}, alert_id=rule.id).status_code == 404
    assert call_view(other, "delete", alert_id=rule.id).status_code == 404
    assert call_view(alert_user, "get", alert_id=rule.id).status_code == 200


@pytest.mark.parametrize("markets,currencies", [(["EGX"], ["USDT"]), (["CRYPTO"], ["EGP"])])
def test_market_and_currency_abac_apply_to_all_paths(make_alert, alert_user, alert_instrument,
                                                   alert_grant, markets, currencies):
    rule = make_alert()
    alert_user.groups.clear()
    alert_grant(alert_user, markets=markets, currencies=currencies)
    assert not AlertSelector.for_user(alert_user).exists()
    for action in [lambda: make_alert(), lambda: AlertSelector.detail(alert_user, rule.id),
                   lambda: UpdateAlert.execute(alert_user, rule.id, UpdateAlertCommand(enabled=False)),
                   lambda: DeleteAlert.execute(alert_user, DeleteAlertCommand(rule.id))]:
        with pytest.raises(DomainError) as error:
            action()
        assert error.value.status == 403


def test_changing_instrument_rechecks_target_policy(make_alert, alert_user):
    rule = make_alert()
    denied = Instrument.objects.create(
        symbol="DENIED", name="Denied", market="EGX", currency="EGP", provider="test",
        provider_symbol="DENIED", price_scale=2, quantity_scale=0, quantity_step=1,
        minimum_quantity=1, cadence_seconds=86400, data_mode="EOD",
    )
    with pytest.raises(DomainError) as error:
        UpdateAlert.execute(alert_user, rule.id, UpdateAlertCommand(instrument_id=denied.id))
    assert error.value.status == 403
    rule.refresh_from_db()
    assert rule.instrument_id != denied.id


def test_api_crud_pagination_canonical_values_and_audit(alert_user, alert_instrument):
    payload = {"instrument_id": str(alert_instrument.id), "kind": "PRICE_ABOVE", "threshold": "100.00"}
    created = call_view(alert_user, "post", payload)
    assert created.status_code == 201
    assert created.data["errors"] == []
    assert created.data["data"]["threshold"] == "100"
    alert_id = created.data["data"]["id"]
    call_view(alert_user, "post", payload)
    listed = call_view(alert_user, "get", query="?page_size=1")
    assert len(listed.data["data"]) == 1
    assert listed.data["metadata"]["pagination"]["count"] == 2
    patched = call_view(alert_user, "patch", {"threshold": "99.50", "enabled": False}, alert_id=alert_id)
    assert patched.status_code == 200
    assert patched.data["data"]["threshold"] == "99.5"
    assert patched.data["data"]["state_reason"] == "disabled"
    deleted = call_view(alert_user, "delete", alert_id=alert_id)
    assert deleted.status_code == 200
    assert deleted.data["data"]["deleted"] is True
    audits = AuditEvent.objects.filter(target_id=alert_id)
    assert set(audits.values_list("action", flat=True)) == {"alerts.created", "alerts.updated", "alerts.deleted"}
    assert set(audits.values_list("request_id", flat=True)) == {"alert-api-test"}


@pytest.mark.parametrize("payload", [{"threshold": 100.5}, {"threshold": None}, {"user_id": 12}, {}])
def test_patch_rejects_ambiguous_or_unknown_inputs(make_alert, alert_user, payload):
    rule = make_alert()
    assert call_view(alert_user, "patch", payload, alert_id=rule.id).status_code == 400


def test_configuration_edits_reinitialize_without_notification(make_alert, observe, alert_clock,
                                                               alert_user):
    rule = make_alert()
    observe("90")
    evaluate(alert_clock)
    UpdateAlert.execute(alert_user, rule.id, UpdateAlertCommand(threshold="80"))
    assert evaluate(alert_clock) == 0
    alert_clock.advance()
    observe("100")
    assert evaluate(alert_clock) == 0
    rule.refresh_from_db()
    assert rule.last_condition is True
    assert not Notification.objects.exists()


@pytest.mark.parametrize("operation", ["create", "update", "delete"])
def test_api_mutation_audit_is_atomic(make_alert, alert_user, monkeypatch, operation):
    rule = make_alert()
    before = AuditEvent.objects.count()

    def fail(*args, **kwargs):
        raise RuntimeError("audit unavailable")
    monkeypatch.setattr(AuditRecorder, "record", fail)
    with pytest.raises(RuntimeError):
        if operation == "create":
            make_alert(threshold="50")
        elif operation == "update":
            UpdateAlert.execute(alert_user, rule.id, UpdateAlertCommand(threshold="50"))
        else:
            DeleteAlert.execute(alert_user, DeleteAlertCommand(rule.id))
    assert AlertRule.objects.count() == 1
    rule.refresh_from_db()
    assert rule.threshold == "100"
    assert AuditEvent.objects.count() == before


def test_schema_has_explicit_request_and_response_dtos():
    schema = SchemaGenerator(patterns=urlpatterns).get_schema(request=None, public=True)
    assert "/alerts/" in schema["paths"]
    post = schema["paths"]["/alerts/"]["post"]
    assert "requestBody" in post
    assert "201" in post["responses"]
    assert "AlertResponse" in schema["components"]["schemas"]
    assert AlertConfigurationPolicy.threshold(Decimal("1.25"), "PRICE_ABOVE") == "1.25"
