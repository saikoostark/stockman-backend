from django.apps import AppConfig
from django.db.backends.signals import connection_created


def configure_sqlite(sender, connection, **kwargs):
    if connection.vendor == "sqlite":
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")


class CommonConfig(AppConfig):
    name = "apps.common"

    def ready(self):
        connection_created.connect(configure_sqlite, dispatch_uid="stockman.sqlite")
