import json
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from typing import Any

from django.core.serializers.json import DjangoJSONEncoder
from django.http import JsonResponse
from django.utils.translation import gettext as _
from drf_spectacular.utils import OpenApiResponse
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView as DRFAPIView
from rest_framework.views import exception_handler as drf_exception_handler

from .exceptions import DomainError


class EnvelopeSerializer(serializers.Serializer):
    data = serializers.JSONField(allow_null=True)  # type: ignore[assignment]
    metadata = serializers.JSONField()
    errors = serializers.ListField(child=serializers.JSONField())  # type: ignore[assignment]


ENVELOPE = OpenApiResponse(response=EnvelopeSerializer)


def json_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))


def envelope(data=None, request=None, metadata=None, errors=None):
    return {
        "data": json_value(data),
        "metadata": {"request_id": getattr(request, "request_id", ""), "pagination": None,
                     **(metadata or {})},
        "errors": errors or [],
    }


class APIView(DRFAPIView):
    def respond(self, data=None, status=200, metadata=None):
        return Response(envelope(data, self.request, metadata), status=status)


def exception_handler(exc, context):
    request = context.get("request")
    if isinstance(exc, DomainError):
        return Response(envelope(request=request, errors=[{
            "code": exc.code, "message": str(exc.message), "fields": exc.fields,
        }]), status=exc.status)
    response = drf_exception_handler(exc, context)
    if response is not None:
        code = getattr(exc, "default_code", "invalid_request")
        detail = response.data
        message = str(detail.get("detail", _("Please check the submitted values."))) if isinstance(detail, dict) else str(detail)
        response.data = envelope(request=request, errors=[{
            "code": code, "message": message,
            "fields": detail if isinstance(detail, dict) and "detail" not in detail else None,
        }])
    return response


def page(queryset, request, mapper):
    try:
        number = max(1, int(request.query_params.get("page", 1)))
        size = min(100, max(1, int(request.query_params.get("page_size", 25))))
    except ValueError as exc:
        raise DomainError("invalid_pagination", _("Invalid pagination values.")) from exc
    count = queryset.count()
    items = [mapper(item) for item in queryset[(number - 1) * size:number * size]]
    return items, {"pagination": {"page": number, "page_size": size, "count": count}}


def csrf_failure(request, reason=""):
    return JsonResponse(envelope(request=request, errors=[{
        "code": "csrf_failed", "message": _("CSRF verification failed."), "fields": None,
    }]), status=403)


def not_found(request, exception=None):
    return JsonResponse(envelope(request=request, errors=[{
        "code": "not_found", "message": _("Resource not found."), "fields": None,
    }]), status=404)


def server_error(request):
    return JsonResponse(envelope(request=request, errors=[{
        "code": "internal_error", "message": _("An unexpected error occurred."), "fields": None,
    }]), status=500)
