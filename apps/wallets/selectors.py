from apps.identity.policies import AuthorizationPolicy
from apps.wallets.dtos import WalletDTO
from apps.wallets.models import Journal, Wallet
from apps.wallets.money import Money
from apps.wallets.services.ledger import wallet_dto


class WalletSelector:
    @staticmethod
    def list(actor) -> tuple[WalletDTO, ...]:
        wallets = AuthorizationPolicy.scope(actor, "wallets.read", Wallet.objects.filter(user=actor), currency_field="currency")
        existing = {wallet.currency: wallet_dto(wallet) for wallet in wallets}
        return tuple(existing.get(currency, WalletDTO(currency, Money(currency, 0).amount, Money(currency, 0).amount))
                     for currency in Money.SCALES
                     if AuthorizationPolicy.allows(actor, "wallets.read", owner_id=actor.pk, currency=currency))

    @staticmethod
    def ledger(actor, currency):
        Money.scale(currency)
        AuthorizationPolicy.require(actor, "wallets.read", owner_id=actor.pk, currency=currency)
        return AuthorizationPolicy.scope(actor, "wallets.read", Journal.objects.filter(
            wallet__user=actor, wallet__currency=currency, posted_at__isnull=False,
        ).select_related("wallet").prefetch_related("entries"),
            owner_field="wallet__user_id", currency_field="wallet__currency")
