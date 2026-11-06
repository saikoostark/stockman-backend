import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, ClassVar

from django.conf import settings
from django.db import connection, models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.common.exceptions import DomainError

_financial_write = ContextVar("financial_write", default=False)


@contextmanager
def financial_write():
    """Internal posting capability, not an authorization or database security boundary."""
    if not connection.in_atomic_block:
        raise RuntimeError("Financial writes require an atomic transaction")
    token = _financial_write.set(True)
    try:
        yield
    finally:
        _financial_write.reset(token)


def require_financial_write():
    if not _financial_write.get() or not connection.in_atomic_block:
        raise DomainError("immutable_finance", _("Financial records can only change through posting."), status=409)


class FinancialQuerySet(models.QuerySet):
    def update(self, **kwargs):
        require_financial_write()
        return super().update(**kwargs)

    def delete(self):
        raise DomainError("immutable_finance", _("Financial records cannot be deleted."), status=409)

    def bulk_create(self, *args, **kwargs):
        require_financial_write()
        return super().bulk_create(*args, **kwargs)

    def bulk_update(self, *args, **kwargs):
        require_financial_write()
        return super().bulk_update(*args, **kwargs)


class FinancialModel(models.Model):
    objects = FinancialQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        require_financial_write()
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise DomainError("immutable_finance", _("Financial records cannot be deleted."), status=409)


class AppendOnlyQuerySet(FinancialQuerySet):
    def update(self, **kwargs):
        raise DomainError("immutable_finance", _("Posted records cannot be edited."), status=409)

    def bulk_update(self, *args, **kwargs):
        raise DomainError("immutable_finance", _("Posted records cannot be edited."), status=409)

    def bulk_create(self, *args, **kwargs):
        raise DomainError("immutable_finance", _("Posted records must be created individually."), status=409)


class AppendOnlyModel(FinancialModel):
    objects: ClassVar[models.Manager[Any]] = AppendOnlyQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise DomainError("immutable_finance", _("Posted records cannot be edited."), status=409)
        return super().save(*args, **kwargs)


class Wallet(FinancialModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="wallets")
    currency = models.CharField(max_length=4, choices=[("EGP", "EGP"), ("USDT", "USDT")])
    available_units = models.BigIntegerField(default=0)
    reserved_units = models.BigIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "currency"], name="wallet_user_currency"),
            models.CheckConstraint(condition=Q(available_units__gte=0), name="wallet_available_nonnegative"),
            models.CheckConstraint(condition=Q(reserved_units__gte=0), name="wallet_reserved_nonnegative"),
            models.CheckConstraint(condition=Q(currency__in=["EGP", "USDT"]), name="wallet_currency_supported"),
        ]


class JournalQuerySet(FinancialQuerySet):
    def update(self, **kwargs):
        raise DomainError("immutable_finance", _("Use the journal posting operation."), status=409)

    def bulk_create(self, *args, **kwargs):
        raise DomainError("immutable_finance", _("Use the journal posting operation."), status=409)


class Journal(FinancialModel):
    class Kind(models.TextChoices):
        TOPUP = "TOPUP"
        WITHDRAWAL = "WITHDRAWAL"
        REVERSAL = "REVERSAL"
        RESERVE = "RESERVE"
        RELEASE = "RELEASE"
        SETTLEMENT = "SETTLEMENT"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    wallet = models.ForeignKey(Wallet, on_delete=models.PROTECT, related_name="journals")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    reference = models.CharField(max_length=64, blank=True)
    reversal_of = models.OneToOneField("self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversal")
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    posted_at = models.DateTimeField(null=True, blank=True, editable=False)
    reason = models.CharField(max_length=500, blank=True)
    objects: ClassVar[models.Manager[Any]] = JournalQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [models.CheckConstraint(
            condition=(Q(kind="REVERSAL", reversal_of__isnull=False) | (~Q(kind="REVERSAL") & Q(reversal_of__isnull=True))),
            name="journal_reversal_link",
        )]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            previous = type(self).objects.get(pk=self.pk)
            if previous.posted_at is not None:
                raise DomainError("immutable_finance", _("Posted journals cannot be edited."), status=409)
            for field in self._meta.concrete_fields:
                if field.name != "posted_at" and getattr(previous, field.attname) != getattr(self, field.attname):
                    raise DomainError("immutable_finance", _("Journal headers cannot be edited."), status=409)
            if self.posted_at is None or self.entries.count() < 2 or sum(self.entries.values_list("amount_units", flat=True)) != 0:
                raise DomainError("unbalanced_journal", _("A posted journal must balance."), status=409)
        elif self.posted_at is not None:
            raise DomainError("unbalanced_journal", _("Create entries before posting a journal."), status=409)
        return super().save(*args, **kwargs)


class LedgerEntry(AppendOnlyModel):
    class Account(models.TextChoices):
        AVAILABLE = "AVAILABLE"
        RESERVED = "RESERVED"
        ISSUANCE = "ISSUANCE"
        CLEARING = "CLEARING"

    id = models.BigAutoField(primary_key=True)
    journal = models.ForeignKey(Journal, on_delete=models.PROTECT, related_name="entries")
    account = models.CharField(max_length=12, choices=Account.choices)
    amount_units = models.BigIntegerField()

    class Meta:
        constraints = [
            models.CheckConstraint(condition=~Q(amount_units=0), name="ledger_amount_nonzero"),
            models.CheckConstraint(condition=Q(account__in=["AVAILABLE", "RESERVED", "ISSUANCE", "CLEARING"]), name="ledger_account_valid"),
            models.UniqueConstraint(fields=["journal", "account"], name="ledger_journal_account"),
        ]

    def save(self, *args, **kwargs):
        if Journal.objects.filter(pk=self.journal_id, posted_at__isnull=False).exists():
            raise DomainError("immutable_finance", _("Posted journal entries cannot change."), status=409)
        return super().save(*args, **kwargs)
