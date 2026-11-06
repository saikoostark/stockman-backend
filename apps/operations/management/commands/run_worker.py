import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from apps.common.exceptions import DomainError
from apps.markets.services import MarketDataIngestor
from apps.operations.services import RetentionService, WorkerCoordinator


class Command(BaseCommand):
    help = "Poll public market data and evaluate paper orders and alert rules."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        coordinator = WorkerCoordinator()
        ingestor = MarketDataIngestor()
        try:
            coordinator.acquire()
            last_retention = 0.0
            while True:
                ingestor.refresh()
                coordinator.effects()
                if time.monotonic() - last_retention > 3600:
                    RetentionService.run()
                    last_retention = time.monotonic()
                close_old_connections()
                if options["once"]:
                    break
                time.sleep(max(1, settings.MARKET_POLL_SECONDS))
        except KeyboardInterrupt:
            self.stdout.write("Worker stopped.")
        except DomainError as exc:
            raise CommandError(str(exc.message)) from exc
        finally:
            coordinator.release()
