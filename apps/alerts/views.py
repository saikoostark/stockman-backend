from drf_spectacular.utils import OpenApiParameter, extend_schema

from apps.common.api import APIView, EnvelopeSerializer, page

from .dtos import AlertDTO, CreateAlertCommand, DeleteAlertCommand, UpdateAlertCommand
from .selectors import AlertSelector
from .serializers import (
    AlertListResponseSerializer,
    AlertResponseSerializer,
    CreateAlertInputSerializer,
    DeletedAlertResponseSerializer,
    UpdateAlertInputSerializer,
)
from .services import CreateAlert, DeleteAlert, UpdateAlert

ERROR_RESPONSES = {400: EnvelopeSerializer, 403: EnvelopeSerializer, 404: EnvelopeSerializer}


class AlertListView(APIView):
    @extend_schema(
        tags=["Alerts"], responses={200: AlertListResponseSerializer, **ERROR_RESPONSES},
        parameters=[OpenApiParameter("page", int), OpenApiParameter("page_size", int)],
    )
    def get(self, request):
        data, metadata = page(AlertSelector.for_user(request.user), request, AlertDTO.from_model)
        return self.respond(data, metadata=metadata)

    @extend_schema(
        tags=["Alerts"], request=CreateAlertInputSerializer,
        responses={201: AlertResponseSerializer, **ERROR_RESPONSES},
    )
    def post(self, request):
        serializer = CreateAlertInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = CreateAlert.execute(
            request.user, CreateAlertCommand(**serializer.validated_data),
            request_id=getattr(request, "request_id", ""),
        )
        return self.respond(result, status=201)


class AlertDetailView(APIView):
    @extend_schema(tags=["Alerts"], responses={200: AlertResponseSerializer, **ERROR_RESPONSES})
    def get(self, request, alert_id):
        return self.respond(AlertSelector.detail(request.user, alert_id))

    @extend_schema(
        tags=["Alerts"], request=UpdateAlertInputSerializer,
        responses={200: AlertResponseSerializer, **ERROR_RESPONSES},
    )
    def patch(self, request, alert_id):
        serializer = UpdateAlertInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = UpdateAlert.execute(
            request.user, alert_id, UpdateAlertCommand(**serializer.validated_data),
            request_id=getattr(request, "request_id", ""),
        )
        return self.respond(result)

    @extend_schema(
        tags=["Alerts"], request=None,
        responses={200: DeletedAlertResponseSerializer, **ERROR_RESPONSES},
    )
    def delete(self, request, alert_id):
        result = DeleteAlert.execute(
            request.user, DeleteAlertCommand(alert_id=alert_id),
            request_id=getattr(request, "request_id", ""),
        )
        return self.respond(result)
