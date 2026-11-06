from dataclasses import asdict, fields

from django.db import transaction
from django.utils.translation import gettext as _

from apps.audit.services import AuditRecorder
from apps.common.exceptions import DomainError

from .dtos import (
    AlertDTO,
    CreateAlertCommand,
    DeleteAlertCommand,
    DeletedAlertDTO,
    Unset,
    UpdateAlertCommand,
)
from .evaluation import EvaluateAlerts
from .models import AlertRule
from .policies import AlertConfigurationPolicy, default_cooldown
from .selectors import AlertSelector

__all__ = ["CreateAlert", "UpdateAlert", "DeleteAlert", "EvaluateAlerts"]


def validate_configuration(instrument, configuration):
    if configuration["enabled"] and not instrument.enabled:
        raise DomainError("instrument_disabled", _("This instrument is unavailable."))
    configuration["threshold"] = AlertConfigurationPolicy.validate(
        instrument=instrument, **configuration,
    )


class CreateAlert:
    @staticmethod
    def execute(user, command: CreateAlertCommand, request_id="") -> AlertDTO:
        with transaction.atomic():
            instrument = AlertSelector.instrument_for_management(user, command.instrument_id)
            configuration = {
                "kind": command.kind, "threshold": command.threshold,
                "window_seconds": command.window_seconds,
                "cooldown_seconds": default_cooldown() if command.cooldown_seconds is None else command.cooldown_seconds,
                "enabled": command.enabled,
            }
            validate_configuration(instrument, configuration)
            rule = AlertRule.objects.create(
                user=user, instrument=instrument, **configuration,
                state_reason="awaiting_observation" if command.enabled else "disabled",
            )
            result = AlertDTO.from_model(rule)
            AuditRecorder.record(user, "alerts.created", "alert_rule", rule.id,
                                 changes={"after": asdict(result)}, request_id=request_id)
            return result


class UpdateAlert:
    @staticmethod
    def execute(user, alert_id, command: UpdateAlertCommand, request_id="") -> AlertDTO:
        with transaction.atomic():
            rule = AlertSelector.owned(user, alert_id, "alerts.manage")
            before = asdict(AlertDTO.from_model(rule))
            changes = {field.name: getattr(command, field.name) for field in fields(command)
                       if getattr(command, field.name) is not Unset.VALUE}
            if not changes:
                raise DomainError("invalid_alert", _("Supply at least one field to update."))
            instrument = rule.instrument
            if "instrument_id" in changes:
                instrument = AlertSelector.instrument_for_management(user, changes.pop("instrument_id"))
            names = ("kind", "threshold", "window_seconds", "cooldown_seconds", "enabled")
            configuration = {name: changes.get(name, getattr(rule, name)) for name in names}
            validate_configuration(instrument, configuration)
            instrument_changed = instrument.pk != rule.instrument_id
            reset = instrument_changed or any(
                configuration[name] != getattr(rule, name)
                for name in ("kind", "threshold", "window_seconds", "enabled")
            )
            rule.instrument = instrument
            for name, value in configuration.items():
                setattr(rule, name, value)
            if reset:
                rule.last_condition = None
                rule.baseline_at = None
                rule.state_reason = "awaiting_observation" if rule.enabled else "disabled"
            if instrument_changed:
                rule.last_observation_id = ""
                rule.last_source_at = None
                rule.generation = None
            rule.save()
            result = AlertDTO.from_model(rule)
            AuditRecorder.record(user, "alerts.updated", "alert_rule", rule.id,
                                 changes={"before": before, "after": asdict(result)},
                                 request_id=request_id)
            return result


class DeleteAlert:
    @staticmethod
    def execute(user, command: DeleteAlertCommand, request_id="") -> DeletedAlertDTO:
        with transaction.atomic():
            rule = AlertSelector.owned(user, command.alert_id, "alerts.manage")
            result = DeletedAlertDTO(id=rule.id)
            AuditRecorder.record(user, "alerts.deleted", "alert_rule", rule.id,
                                 changes={"before": asdict(AlertDTO.from_model(rule))},
                                 request_id=request_id)
            rule.delete()
            return result
