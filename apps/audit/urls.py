from django.urls import path

from .views import AuditView

urlpatterns = [path("audit/", AuditView.as_view(), name="audit-list")]
