from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.db import close_old_connections, transaction

from apps.common.exceptions import DomainError
from apps.identity.models import GroupPolicy
from apps.identity.policies import ACTIONS, permission_codename
from apps.wallets.dtos import FundingCommand, PostingCommand, PostingLine, ReversalCommand
from apps.wallets.models import Journal, LedgerEntry, Wallet, financial_write
from apps.wallets.selectors import WalletSelector
from apps.wallets.services import LedgerPoster, ReverseFunding, TopUpWallet, WithdrawFunds
from apps.wallets.services.reconciliation import Reconciliation

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def wallet_actor(user):
    group = Group.objects.create(name="Finance fixture grants")
    content_type = ContentType.objects.get_for_model(GroupPolicy)
    actions = [action for action in ACTIONS if action.startswith(("wallets.", "trading."))]
    GroupPolicy.objects.create(group=group, attributes={"schema_version": 1,
        "scopes": {action: {"ownership": "own"} for action in actions}, "deny_actions": []})
    group.permissions.set([Permission.objects.get_or_create(content_type=content_type,
        codename=permission_codename(action), defaults={"name": action})[0] for action in actions])
    user.groups.add(group)
    return user


def test_funding_withdrawal_idempotency_and_reconciliation(wallet_actor):
    command = FundingCommand("EGP", "100.01", "Practice")
    first = TopUpWallet().execute(wallet_actor, command, "fund-1")
    assert TopUpWallet().execute(wallet_actor, command, "fund-1") == first
    WithdrawFunds().execute(wallet_actor, FundingCommand("EGP", "20.00"), "withdraw-1")
    wallet = Wallet.objects.get(user=wallet_actor, currency="EGP")
    assert (wallet.available_units, wallet.reserved_units) == (8001, 0)
    assert Journal.objects.count() == 2
    assert all(sum(j.entries.values_list("amount_units", flat=True)) == 0 for j in Journal.objects.all())
    assert Reconciliation.run() == ()
    call_command("reconcile_ledger")
    with pytest.raises(DomainError, match="another request"):
        TopUpWallet().execute(wallet_actor, FundingCommand("EGP", "99.00"), "fund-1")


def test_insufficient_funds_and_negative_do_not_write(wallet_actor):
    for amount in ("-1", "0", "0.001", "NaN"):
        with pytest.raises(DomainError):
            TopUpWallet().execute(wallet_actor, FundingCommand("EGP", amount), "invalid")
    with pytest.raises(DomainError) as error:
        WithdrawFunds().execute(wallet_actor, FundingCommand("EGP", "1"), "insufficient")
    assert error.value.code == "insufficient_funds"
    assert not Journal.objects.exists()
    assert not Wallet.objects.exists()


def test_linked_reversals_only_funding_and_only_available_cash(wallet_actor):
    original = TopUpWallet().execute(wallet_actor, FundingCommand("EGP", "100"), "fund")
    withdrawal = WithdrawFunds().execute(wallet_actor, FundingCommand("EGP", "60"), "withdraw")
    with pytest.raises(DomainError) as error:
        ReverseFunding().execute(wallet_actor, ReversalCommand(original.id, "Correction"), "reverse-fund")
    assert error.value.code == "insufficient_funds"
    restored = ReverseFunding().execute(wallet_actor, ReversalCommand(withdrawal.id, "Wrong withdrawal"), "reverse-withdraw")
    assert restored.reversal_of == withdrawal.id
    reverse = ReverseFunding().execute(wallet_actor, ReversalCommand(original.id, "Correction"), "reverse-fund")
    assert ReverseFunding().execute(wallet_actor, ReversalCommand(original.id, "Correction"), "reverse-fund") == reverse
    assert Wallet.objects.get(user=wallet_actor).available_units == 0
    with pytest.raises(DomainError):
        ReverseFunding().execute(wallet_actor, ReversalCommand(original.id, "Again"), "again")
    with pytest.raises(DomainError):
        ReverseFunding().execute(wallet_actor, ReversalCommand(reverse.id, "Reverse a reversal"), "recursive")
    assert Reconciliation.run() == ()


def test_posting_requires_balanced_lines_and_posted_records_immutable(wallet_actor):
    first = TopUpWallet().execute(wallet_actor, FundingCommand("USDT", "1"), "fund")
    journal = Journal.objects.get(pk=first.id)
    entry = journal.entries.first()
    with pytest.raises(DomainError):
        entry.amount_units = 99
        entry.save()
    with pytest.raises(DomainError):
        LedgerEntry.objects.filter(pk=entry.pk).update(amount_units=99)
    with pytest.raises(DomainError):
        journal.delete()
    with pytest.raises(DomainError):
        journal.wallet.available_units = 10
        journal.wallet.save()
    with transaction.atomic(), financial_write():
        with pytest.raises(DomainError):
            LedgerEntry.objects.create(journal=journal, account="CLEARING", amount_units=1)
        with pytest.raises(DomainError):
            LedgerPoster.post(wallet_actor, journal.wallet, PostingCommand("TOPUP", (PostingLine("AVAILABLE", 1), PostingLine("ISSUANCE", -2))))
    assert Reconciliation.run() == ()


def test_ownership_and_replay_reauthorization(wallet_actor, django_user_model):
    first = TopUpWallet().execute(wallet_actor, FundingCommand("EGP", "1"), "fund")
    other = django_user_model.objects.create_user(username="other")
    assert WalletSelector.list(other) == ()
    with pytest.raises(DomainError) as error:
        ReverseFunding().execute(other, ReversalCommand(first.id, "Not mine"), "steal")
    assert error.value.status == 404
    wallet_actor.groups.clear()
    with pytest.raises(DomainError) as error:
        TopUpWallet().execute(wallet_actor, FundingCommand("EGP", "1"), "fund")
    assert error.value.status == 403
    assert Journal.objects.count() == 1


def test_audit_failure_rolls_back_money_and_journal(wallet_actor, monkeypatch):
    def failure(*args, **kwargs):
        raise RuntimeError("audit failure")
    monkeypatch.setattr("apps.wallets.services.ledger.AuditRecorder.record", failure)
    with pytest.raises(RuntimeError):
        TopUpWallet().execute(wallet_actor, FundingCommand("EGP", "10"), "fund")
    assert not Journal.objects.exists()
    assert not Wallet.objects.exists()


def test_concurrent_withdrawals_cannot_overdraw(wallet_actor, django_user_model):
    TopUpWallet().execute(wallet_actor, FundingCommand("EGP", "10"), "fund")
    barrier = Barrier(2)

    def withdraw(number):
        close_old_connections()
        actor = django_user_model.objects.get(pk=wallet_actor.pk)
        barrier.wait(timeout=10)
        try:
            WithdrawFunds().execute(actor, FundingCommand("EGP", "8"), f"withdraw-{number}")
            return "ok"
        except DomainError as exc:
            return exc.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(withdraw, [1, 2]))
    assert sorted(outcomes) == ["insufficient_funds", "ok"]
    assert Wallet.objects.get(user=wallet_actor).available_units == 200
    assert Reconciliation.run() == ()


def test_duplicate_concurrent_funding_has_one_effect(wallet_actor, django_user_model):
    barrier = Barrier(2)

    def fund(_number):
        close_old_connections()
        actor = django_user_model.objects.get(pk=wallet_actor.pk)
        barrier.wait(timeout=10)
        try:
            return TopUpWallet().execute(actor, FundingCommand("EGP", "10"), "same-key").id
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(fund, [1, 2]))
    assert outcomes[0] == outcomes[1]
    assert Journal.objects.count() == 1
    assert Wallet.objects.get(user=wallet_actor).available_units == 1000
