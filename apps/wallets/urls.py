from django.urls import path

from apps.wallets.views import LedgerView, ReversalView, TopUpView, WalletListView, WithdrawalView

app_name = "wallets"
urlpatterns = [
    path("wallets/", WalletListView.as_view(), name="list"),
    path("wallets/<str:currency>/topups/", TopUpView.as_view(), name="topup"),
    path("wallets/<str:currency>/withdrawals/", WithdrawalView.as_view(), name="withdraw"),
    path("wallets/<str:currency>/ledger/", LedgerView.as_view(), name="ledger"),
    path("wallets/journals/<uuid:journal_id>/reverse/", ReversalView.as_view(), name="reverse"),
]
