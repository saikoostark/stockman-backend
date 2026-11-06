from django.db import models


class WorkerLease(models.Model):
    name = models.CharField(max_length=40, primary_key=True)
    token = models.CharField(max_length=36)
    generation = models.PositiveBigIntegerField(default=0)
    expires_at = models.DateTimeField()
    heartbeat_at = models.DateTimeField()
