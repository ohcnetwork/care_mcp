import json

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.http.response import Http404
from django.urls import reverse
from django_ratelimit.core import is_ratelimited
from rest_framework import status
from rest_framework.authentication import TokenAuthentication
from rest_framework.exceptions import PermissionDenied, Throttled, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from care_mcp.authentication import ServiceAccountBearerAuthentication
from care_mcp.protocol import (
    INVALID_REQUEST,
    PARSE_ERROR,
    SUPPORTED_PROTOCOL_VERSIONS,
    error_response,
    handle_message,
)
from care_mcp.settings import plugin_settings, setting_list
from care_mcp.tools import ToolContext
from config.authentication import CustomJWTAuthentication

# JSON-RPC batches (sent only by 2025-03-26 clients) are capped, and every
# message in one counts against the rate limit, so a batch cannot multiply a
# single request's allowance.
MAX_BATCH_MESSAGES = 20


class MCPView(APIView):
    """The MCP endpoint (Streamable HTTP transport, JSON responses).

    Authenticates like Care's own API: a service-account token
    (``Authorization: Token <token>`` or ``Bearer <token>``) for MCP clients, or
    a Care JWT for clients running inside Care's web app.
    """

    authentication_classes = [
        ServiceAccountBearerAuthentication,
        CustomJWTAuthentication,
        TokenAuthentication,
    ]
    permission_classes = [IsAuthenticated]

    def initial(self, request, *args, **kwargs):
        if not plugin_settings.CARE_MCP_ENABLED:
            raise Http404
        # Reject browser pages on other origins before doing any work.
        origin = request.headers.get("Origin")
        allowed = {o.rstrip("/") for o in setting_list("CARE_MCP_ALLOWED_ORIGINS")}
        if origin and origin.rstrip("/") not in allowed:
            raise PermissionDenied("Origin not allowed.")
        super().initial(request, *args, **kwargs)

        version = request.headers.get("MCP-Protocol-Version")
        if version and version not in SUPPORTED_PROTOCOL_VERSIONS:
            raise ValidationError(
                {"detail": f"Unsupported MCP-Protocol-Version: {version}"}
            )
        self.check_rate_limit(request)

    def check_rate_limit(self, request):
        """Count one message against the caller's rate limit."""
        rate = plugin_settings.CARE_MCP_RATE_LIMIT
        if (
            rate
            and not settings.DISABLE_RATELIMIT
            and is_ratelimited(
                request._request,  # noqa: SLF001
                group="care_mcp",
                key="user",
                rate=rate,
                increment=True,
            )
        ):
            raise Throttled

    def post(self, request, *args, **kwargs):
        try:
            payload = json.loads(request.body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return JsonResponse(
                error_response(None, PARSE_ERROR, "Parse error."), status=400
            )

        ctx = ToolContext(
            user=request.user, allow_writes=plugin_settings.CARE_MCP_ALLOW_WRITES
        )
        # JSON-RPC batches were dropped in protocol 2025-06-18 but older clients
        # may still send them.
        if isinstance(payload, list):
            if not 0 < len(payload) <= MAX_BATCH_MESSAGES:
                message = f"A batch must hold 1 to {MAX_BATCH_MESSAGES} messages."
                return JsonResponse(
                    error_response(None, INVALID_REQUEST, message), status=400
                )
            # initial() counted the first message; count the rest before any runs.
            for _ in payload[1:]:
                self.check_rate_limit(request)
            responses = [r for m in payload if (r := handle_message(ctx, m))]
            if not responses:
                return HttpResponse(status=status.HTTP_202_ACCEPTED)
            return JsonResponse(responses, safe=False)

        response = handle_message(ctx, payload)
        if response is None:
            return HttpResponse(status=status.HTTP_202_ACCEPTED)
        return JsonResponse(response)


class ConfigView(APIView):
    """What a client (or Care's frontend) needs to connect."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(
            {
                "enabled": plugin_settings.CARE_MCP_ENABLED,
                "endpoint": request.build_absolute_uri(reverse("care-mcp-endpoint")),
                "allow_writes": plugin_settings.CARE_MCP_ALLOW_WRITES,
                "protocol_versions": list(SUPPORTED_PROTOCOL_VERSIONS),
            }
        )
