import os

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.utils.translation import gettext as _

from apps.common.exceptions import DomainError
from apps.identity.services import BootstrapIdentity


class Command(BaseCommand):
    help = "Seed editable access templates; optionally create an explicitly named administrator."

    def add_arguments(self, parser):
        parser.add_argument("--username", default=None)
        parser.add_argument("--password", default=None)
        parser.add_argument("--email", default=None)

    def handle(self, *args, **options):
        try:
            BootstrapIdentity.execute(
                username=options["username"] or os.environ.get("STOCKMAN_ADMIN_USERNAME"),
                password=options["password"] or os.environ.get("STOCKMAN_ADMIN_PASSWORD"),
                email=options["email"] or os.environ.get("STOCKMAN_ADMIN_EMAIL", ""),
            )
        except (DomainError, ValidationError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(_("Access templates initialized.")))
