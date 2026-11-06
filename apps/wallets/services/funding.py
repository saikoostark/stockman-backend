from dataclasses import asdict

from django.db import transaction
from django.utils.translation import gettext as _

from apps.common.exceptions import DomainError
from apps.common.idempotency import Idempotency
from apps.identity.policies import AuthorizationPolicy
from apps.wallets.dtos import (
    EntryDTO,
    FundingCommand,
    JournalDTO,
    PostingCommand,
    PostingLine,
    ReversalCommand,
)
from apps.wallets.models import Journal
from apps.wallets.money import Money
from apps.wallets.services.ledger import LedgerPoster, journal_dto


def result_dto(result) -> JournalDTO:
    if isinstance(result, JournalDTO):
        return result
    return JournalDTO(**{**result, "entries": tuple(EntryDTO(**e) for e in result["entries"])})


def authorize_funding(actor, currency):
    AuthorizationPolicy.require(actor, "wallets.fund", owner_id=actor.pk, currency=currency)


class TopUpWallet:
    kind = Journal.Kind.TOPUP
    sign = 1

    def execute(self, actor, command: FundingCommand, key: str) -> JournalDTO:
        amount = Money.from_decimal(command.currency, command.amount)
        if len(command.reason) > 500:
            raise DomainError("invalid_reason", _("Reason is too long."))
        # Enclose authorization AND replay lookup in the same serialized write transaction.
        with transaction.atomic():
            authorize_funding(actor, command.currency)

            def action():
                authorize_funding(actor, command.currency)
                wallet = LedgerPoster.wallet(actor, command.currency)
                journal = LedgerPoster.post(actor, wallet, PostingCommand(
                    kind=self.kind, lines=(PostingLine("AVAILABLE", amount.units * self.sign),
                                           PostingLine("ISSUANCE", -amount.units * self.sign)),
                    reason=command.reason, request_id=command.request_id,
                ))
                return asdict(journal_dto(journal))

            result = Idempotency.execute(actor, f"wallets.{self.kind.lower()}", key,
                                         {"currency": command.currency, "amount": command.amount,
                                          "reason": command.reason}, action)
            return result_dto(result)


class WithdrawFunds(TopUpWallet):
    kind = Journal.Kind.WITHDRAWAL
    sign = -1


class ReverseFunding:
    def execute(self, actor, command: ReversalCommand, key: str) -> JournalDTO:
        if not command.reason.strip() or len(command.reason) > 500:
            raise DomainError("invalid_reason", _("A correction reason is required."))
        with transaction.atomic():
            original = Journal.objects.select_related("wallet").filter(
                pk=command.journal_id, wallet__user=actor, posted_at__isnull=False,
            ).first()
            if original is None:
                raise DomainError("not_found", _("Journal not found."), status=404)
            authorize_funding(actor, original.wallet.currency)

            def action():
                authorize_funding(actor, original.wallet.currency)
                if original.kind not in (Journal.Kind.TOPUP, Journal.Kind.WITHDRAWAL):
                    raise DomainError("invalid_reversal", _("Only funding journals can be reversed."), status=409)
                if Journal.objects.filter(reversal_of=original).exists():
                    raise DomainError("already_reversed", _("This funding has already been reversed."), status=409)
                journal = LedgerPoster.post(actor, original.wallet, PostingCommand(
                    kind=Journal.Kind.REVERSAL,
                    lines=tuple(PostingLine(e.account, -e.amount_units) for e in original.entries.all()),
                    reversal_of=str(original.pk), reason=command.reason, request_id=command.request_id,
                ))
                return asdict(journal_dto(journal))

            return result_dto(Idempotency.execute(actor, "wallets.reverse", key,
                              {"journal_id": command.journal_id, "reason": command.reason}, action))
