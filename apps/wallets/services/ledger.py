from collections import defaultdict
from dataclasses import asdict

from django.db import connection
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.audit.services import AuditRecorder
from apps.common.exceptions import DomainError
from apps.events.services import EventPublisher
from apps.wallets.dtos import EntryDTO, JournalDTO, PostingCommand, WalletDTO
from apps.wallets.models import Journal, LedgerEntry, Wallet, financial_write
from apps.wallets.money import Money, checked_units


def wallet_dto(wallet: Wallet) -> WalletDTO:
    return WalletDTO(wallet.currency, Money(wallet.currency, wallet.available_units).amount,
                     Money(wallet.currency, wallet.reserved_units).amount)


def journal_dto(journal: Journal) -> JournalDTO:
    currency = journal.wallet.currency
    return JournalDTO(
        str(journal.pk), currency, journal.kind, journal.reference,
        str(journal.reversal_of_id) if journal.reversal_of_id else None,
        journal.created_at.isoformat(), journal.posted_at.isoformat() if journal.posted_at else None,
        journal.reason, tuple(EntryDTO(e.account, Money(currency, e.amount_units).amount)
                              for e in journal.entries.all()),
    )


class LedgerPoster:
    """Only caller-owned atomic transactions may post; no implicit permissions here."""

    @staticmethod
    def wallet(user, currency: str) -> Wallet:
        Money.scale(currency)
        with financial_write():
            wallet, _created = Wallet.objects.get_or_create(user=user, currency=currency)
        return wallet

    @staticmethod
    def post(actor, wallet: Wallet, command: PostingCommand) -> Journal:
        if not connection.in_atomic_block:
            raise RuntimeError("Posting requires the caller's atomic transaction")
        wallet = Wallet.objects.get(pk=wallet.pk)
        totals: dict[str, int] = defaultdict(int)
        for line in command.lines:
            if line.account not in LedgerEntry.Account.values:
                raise DomainError("invalid_account", _("Unknown ledger account."))
            totals[line.account] += checked_units(line.units)
        totals = {account: checked_units(units) for account, units in totals.items() if units}
        if len(totals) < 2 or sum(totals.values()) != 0:
            raise DomainError("unbalanced_journal", _("Journal entries must balance per currency."))
        available = checked_units(wallet.available_units + totals.get("AVAILABLE", 0))
        reserved = checked_units(wallet.reserved_units + totals.get("RESERVED", 0))
        checked_units(available + reserved)
        if available < 0 or reserved < 0:
            raise DomainError("insufficient_funds", _("Insufficient available funds."), status=409)
        before = asdict(wallet_dto(wallet))
        with financial_write():
            journal = Journal.objects.create(
                wallet=wallet, kind=command.kind, reference=command.reference,
                reason=command.reason, reversal_of_id=command.reversal_of,
            )
            for account, units in totals.items():
                LedgerEntry.objects.create(journal=journal, account=account, amount_units=units)
            journal.posted_at = timezone.now()
            journal.save(update_fields=["posted_at"])
            wallet.available_units = available
            wallet.reserved_units = reserved
            wallet.save(update_fields=["available_units", "reserved_units"])
        after = asdict(wallet_dto(wallet))
        AuditRecorder.record(actor, f"wallets.{command.kind.lower()}", "journal", str(journal.pk),
                             changes={"before": before, "after": after, "reference": command.reference},
                             request_id=command.request_id, reason=command.reason)
        EventPublisher.publish(wallet.user, "wallet.changed", {"journal_id": str(journal.pk), **after})
        return journal
