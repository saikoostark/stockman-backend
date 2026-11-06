from django.contrib.auth.models import AbstractUser, Group
from django.db import models
from django.utils.translation import gettext_lazy as _


def default_policy_attributes():
    return {"schema_version": 1, "scopes": {}, "deny_actions": []}


class User(AbstractUser):
    class Locale(models.TextChoices):
        ENGLISH = "en", _("English")
        ARABIC = "ar", _("Arabic")
        GERMAN = "de", _("German")
        SPANISH = "es", _("Spanish")

    locale = models.CharField(max_length=2, choices=Locale.choices, default=Locale.ENGLISH)
    authorization_revision = models.PositiveBigIntegerField(default=0)


class GroupPolicy(models.Model):
    group = models.OneToOneField(Group, on_delete=models.CASCADE, related_name="policy")
    attributes = models.JSONField(default=default_policy_attributes)

    def clean(self):
        from apps.identity.policies import PolicySchema

        super().clean()
        PolicySchema.validate(self.attributes)

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)
