from django.core.management.base import BaseCommand, CommandError

from apps.wallets.services.reconciliation import Reconciliation


class Command(BaseCommand):
    help = "Check posted money/assets, reservations, FIFO lots and balance projections without editing them."

    def handle(self, *args, **options):
        problems = Reconciliation.run()
        if problems:
            for problem in problems:
                self.stderr.write(problem)
            raise CommandError(f"Reconciliation failed: {len(problems)} discrepancies")
        self.stdout.write(self.style.SUCCESS("Ledger and position projections reconcile."))
