from collections import defaultdict
from typing import Any

from django.db import transaction

from apps.trading.models import AssetMovement, Fill, Order, Position
from apps.wallets.models import Journal, Wallet


class Reconciliation:
    @staticmethod
    @transaction.atomic
    def run() -> tuple[str, ...]:
        """Read a consistent snapshot. Report discrepancies; never silently repair history."""
        problems = []
        cash: defaultdict[Any, defaultdict[Any, int]] = defaultdict(lambda: defaultdict(int))
        for journal in Journal.objects.prefetch_related("entries").order_by("created_at", "id"):
            entries = list(journal.entries.all())
            if journal.posted_at is None:
                problems.append(f"journal {journal.pk}: unposted journal")
                continue
            if len(entries) < 2 or sum(e.amount_units for e in entries) != 0:
                problems.append(f"journal {journal.pk}: unbalanced postings")
            for entry in entries:
                cash[journal.wallet_id][entry.account] += entry.amount_units
        buy_reserves: defaultdict[Any, int] = defaultdict(int)
        sell_reserves: defaultdict[Any, int] = defaultdict(int)
        for order in Order.objects.filter(status="OPEN"):
            buy_reserves[(order.user_id, order.currency)] += order.reserved_cash_units
            sell_reserves[(order.user_id, order.instrument_id)] += order.reserved_quantity_units
        for wallet in Wallet.objects.all():
            ledger = cash[wallet.pk]
            if (wallet.available_units, wallet.reserved_units) != (ledger["AVAILABLE"], ledger["RESERVED"]):
                problems.append(f"wallet {wallet.pk}: ledger/projection discrepancy")
            if wallet.reserved_units != buy_reserves[(wallet.user_id, wallet.currency)]:
                problems.append(f"wallet {wallet.pk}: order reservation discrepancy")
        assets: defaultdict[Any, list[int]] = defaultdict(lambda: [0, 0])
        for movement in AssetMovement.objects.all():
            assets[movement.position_id][0] += movement.available_delta
            assets[movement.position_id][1] += movement.reserved_delta
            if movement.available_delta + movement.reserved_delta + movement.counterparty_delta:
                problems.append(f"movement {movement.pk}: unbalanced asset posting")
        for position in Position.objects.prefetch_related("lots__consumptions"):
            if [position.available_units, position.reserved_units] != assets[position.pk]:
                problems.append(f"position {position.pk}: asset ledger/projection discrepancy")
            if position.reserved_units != sell_reserves[(position.user_id, position.instrument_id)]:
                problems.append(f"position {position.pk}: order reservation discrepancy")
            lots = list(position.lots.all())
            if sum(lot.remaining_quantity_units for lot in lots) != position.available_units + position.reserved_units:
                problems.append(f"position {position.pk}: FIFO quantity discrepancy")
            if sum(lot.remaining_cost_units for lot in lots) != position.cost_basis_units:
                problems.append(f"position {position.pk}: FIFO cost discrepancy")
            realized = sum(Fill.objects.filter(order__user=position.user, order__instrument=position.instrument, order__side="SELL").values_list("realized_pnl_units", flat=True))
            if realized != position.realized_pnl_units:
                problems.append(f"position {position.pk}: realized P&L discrepancy")
            for lot in lots:
                consumed = list(lot.consumptions.all())
                if sum(item.quantity_units for item in consumed) + lot.remaining_quantity_units != lot.original_quantity_units or sum(item.cost_units for item in consumed) + lot.remaining_cost_units != lot.original_cost_units:
                    problems.append(f"lot {lot.pk}: consumption discrepancy")
        for order in Order.objects.select_related("fill"):
            has_fill = hasattr(order, "fill")
            if (order.status == "FILLED") != has_fill:
                problems.append(f"order {order.pk}: fill/status discrepancy")
            if order.status != "OPEN" and (order.reserved_cash_units or order.reserved_quantity_units):
                problems.append(f"order {order.pk}: terminal order retains reserves")
        return tuple(problems)
