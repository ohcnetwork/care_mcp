"""Run a request through Care's own API views as the MCP caller.

Every tool goes through here, so an MCP client gets exactly what the same user
would get from the REST API: the same viewsets, serializers, querysets,
AuthorizationController checks and error responses. Nothing in this plugin
re-implements Care's permission logic.

This mirrors ``care.emr.utils.batch_requests``, with two differences: the caller
is attached with DRF's forced authentication, so the internal request needs no
credentials of its own, and the response is rendered to plain JSON types.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from django.db import transaction
from django.test.client import RequestFactory
from django.urls import Resolver404, resolve
from rest_framework.renderers import JSONRenderer

logger = logging.getLogger(__name__)

API_PREFIX = "/api/"

# Paths no MCP tool may reach: credential, login and session management, batch
# requests (which would let a caller smuggle methods past the read-only check),
# the schema itself and this plugin's own endpoints.
BLOCKED_PATH_PREFIXES = (
    "/api/v1/auth/",
    "/api/v1/password_",
    "/api/v1/mfa/",
    "/api/v1/otp/",
    "/api/v1/batch_requests/",
    "/api/schema/",
    "/api/care_mcp/",
)
BLOCKED_PATH_FRAGMENTS = (
    "/reset_password",
    "/set_password",
    "/password",
    "service_account_token",
)


class PathNotAllowedError(ValueError):
    pass


@dataclass
class APIResult:
    status_code: int
    data: Any

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300  # noqa: PLR2004


def is_blocked_path(path: str) -> bool:
    lowered = path.lower()
    return lowered.startswith(BLOCKED_PATH_PREFIXES) or any(
        fragment in lowered for fragment in BLOCKED_PATH_FRAGMENTS
    )


def validate_api_path(path: str) -> str:
    """Make sure ``path`` is a Care API path that MCP may call."""
    if not isinstance(path, str) or not path.startswith(API_PREFIX):
        msg = f"{path!r} is not a Care API path."
        raise PathNotAllowedError(msg)
    if any(c in path for c in "?#\\") or ".." in path or "//" in path:
        msg = "Path must not contain '?', '#', '..', '//' or backslashes."
        raise PathNotAllowedError(msg)
    if is_blocked_path(path):
        msg = f"{path} is not available over MCP."
        raise PathNotAllowedError(msg)
    return path


def _clean_query(query: dict | None) -> dict:
    cleaned = {}
    for key, value in (query or {}).items():
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            cleaned[key] = "true" if value else "false"
        elif isinstance(value, list | tuple):
            cleaned[key] = ",".join(str(v) for v in value)
        else:
            cleaned[key] = value
    return cleaned


def _to_json(data: Any) -> Any:
    """Render DRF response data (UUIDs, datetimes, lazy strings…) to JSON types."""
    if data is None:
        return None
    return json.loads(JSONRenderer().render(data))


def call_api(
    user,
    method: str,
    path: str,
    query: dict | None = None,
    body: Any = None,
) -> APIResult:
    """Call a Care API view as ``user`` and return its status and JSON data."""
    path = validate_api_path(path)
    method = method.lower()
    query_string = urlencode(_clean_query(query), doseq=True)
    url = f"{path}?{query_string}" if query_string else path

    factory = RequestFactory()
    if method == "get":
        request = factory.get(url)
    elif method in ("post", "put", "patch", "delete"):
        request = getattr(factory, method)(
            url,
            data=json.dumps(body if body is not None else {}),
            content_type="application/json",
        )
    else:
        msg = f"Unsupported method {method.upper()}."
        raise PathNotAllowedError(msg)

    # Picked up by rest_framework.request.Request, so the view sees this user
    # regardless of its own authentication classes.
    request._force_auth_user = user  # noqa: SLF001
    request.user = user

    try:
        match = resolve(path)
    except Resolver404:
        return APIResult(404, {"detail": f"No Care API route matches {path}"})

    try:
        # A savepoint per call, so a failed write leaves no partial database changes.
        with transaction.atomic():
            response = match.func(request, *match.args, **match.kwargs)
            if hasattr(response, "data"):
                data = _to_json(response.data)
            else:
                # e.g. file downloads; nothing useful to hand a model.
                data = {"detail": "Non-JSON response omitted."}
            if response.status_code >= 400:  # noqa: PLR2004
                # Undo anything a failing write view did before it errored.
                transaction.set_rollback(True)
    except Exception:
        # The route's name rather than the path, whose segments are patient and
        # encounter ids.
        logger.exception(
            "care_mcp: internal API call failed: %s %s",
            method.upper(),
            match.view_name,
        )
        return APIResult(500, {"detail": "Care returned a server error."})
    return APIResult(response.status_code, data)
