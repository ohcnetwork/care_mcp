"""MCP over JSON-RPC 2.0: lifecycle, tools and prompts.

The server is stateless: every request carries its own credentials and nothing
is kept between requests, so it runs behind any number of Care workers and
needs no session store. That is allowed by the Streamable HTTP transport, which
makes session IDs optional.
"""

import json
import logging
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from care_mcp.settings import plugin_settings
from care_mcp.tools import (
    ToolContext,
    ToolError,
    ToolOutput,
    available_tools,
    validate_arguments,
)

logger = logging.getLogger(__name__)

# Read from the installed distribution rather than care_mcp.__version__: when the
# repo is cloned inside Care's checkout, "care_mcp" can resolve to the repo
# directory as a namespace package, which has no __init__ attributes.
try:
    SERVER_VERSION = version("care_mcp")
except PackageNotFoundError:
    SERVER_VERSION = "unknown"

# Newest first. A client asking for one of these gets it back; anything else
# gets the newest, and the client decides whether it can continue.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

INSTRUCTIONS = """\
You are connected to CARE, an open-source electronic medical record and hospital \
management system, as a Care account. Every tool runs with that account's \
permissions: you only see and change what it could in Care's web app.

The tools are generated from Care's OpenAPI (Swagger) schema. Common operations \
have their own tools, named after their operation ids; search_operations, \
get_operation and call_operation reach every other Care API operation.

How Care's data fits together:
- Start with users_getcurrentuser_retrieve to learn the account's facilities.
- Visits and admissions are encounters. Patients currently under care at a \
facility: encounter_list with facility=<id> and live=false (live=false leaves out \
completed, cancelled and discontinued encounters).
- A patient's clinical records (diagnoses, symptoms, allergies, medications, \
observations, forms, reports, notes) live under /patient/<id>/. If the account's \
access comes through an encounter, pass encounter=<id>; without it Care returns 403.
- Diagnoses and symptoms are both conditions. patient_diagnosis_list also returns \
symptoms unless you pass category=encounter_diagnosis,chronic_condition.
- Vitals and lab values are observations; prescriptions are medication requests; \
orders for tests and procedures are service requests.
- Lists return {count, results}; page with limit and offset. Filters that take \
several values take them comma-separated. IDs are UUIDs from earlier results.

This is patient data. Quote values as recorded, say when data is missing rather \
than guessing, and do not repeat identifiers or contact details unless asked.\
"""

PROMPTS = {
    "patient_summary": {
        "title": "Summarise a patient",
        "description": "Clinical summary of a patient, optionally for one encounter.",
        "arguments": [
            {"name": "patient_id", "description": "Patient UUID", "required": True},
            {
                "name": "encounter_id",
                "description": "Encounter UUID (optional)",
                "required": False,
            },
        ],
        "template": (
            "Summarise patient {patient_id}{encounter_clause} for a clinician. Get "
            "the patient, their open encounters, active diagnoses and symptoms, "
            "allergies, active medication requests and the latest observations"
            "{encounter_filter}. Write who the patient is, why they are under care, "
            "active problems, allergies, current medications, and the latest vitals "
            "and results with any abnormal values called out. Say explicitly what "
            "is not recorded."
        ),
    },
    "shift_handover": {
        "title": "Shift handover for a facility",
        "description": "Handover notes for the patients currently admitted at a facility.",
        "arguments": [
            {"name": "facility_id", "description": "Facility UUID", "required": True},
        ],
        "template": (
            "List the inpatient encounters still open at facility {facility_id} "
            "(encounter_list with facility={facility_id}, live=false, "
            "encounter_class=imp). For each patient, read their active diagnoses, "
            "current medication requests and latest observations, passing the "
            "encounter id, and write a short handover entry: location, working "
            "diagnosis, current medications, latest vitals, and anything pending "
            "or concerning."
        ),
    },
}


class JSONRPCError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _negotiate_version(requested) -> str:
    if requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return SUPPORTED_PROTOCOL_VERSIONS[0]


def _truncate(text: str) -> str:
    limit = plugin_settings.CARE_MCP_MAX_RESPONSE_CHARS
    if limit and len(text) > limit:
        omitted = len(text) - limit
        return (
            text[:limit] + f"\n…[truncated {omitted} characters. Narrow the request "
            "with filters, or page with a smaller limit and an offset.]"
        )
    return text


def _as_object(value: Any, what: str) -> dict:
    """An optional object member of a message: absent or null means empty."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise JSONRPCError(INVALID_PARAMS, f"{what} must be an object.")
    return value


def _text_result(data: Any, *, is_error: bool = False, notes=()) -> dict:
    text = data if isinstance(data, str) else json.dumps(data, separators=(",", ":"))
    content = [{"type": "text", "text": _truncate(text)}]
    content.extend({"type": "text", "text": note} for note in notes)
    return {"content": content, "isError": is_error}


def handle_initialize(ctx, params):
    return {
        "protocolVersion": _negotiate_version(params.get("protocolVersion")),
        "capabilities": {
            "tools": {"listChanged": False},
            "prompts": {"listChanged": False},
        },
        "serverInfo": {
            "name": "care",
            "title": "CARE EMR",
            "version": SERVER_VERSION,
        },
        "instructions": INSTRUCTIONS,
    }


def handle_tools_list(ctx, params):
    return {"tools": [t.definition() for t in available_tools(ctx)]}


def handle_tools_call(ctx, params):
    name = params.get("name")
    tool = next((t for t in available_tools(ctx) if t.name == name), None)
    if tool is None:
        raise JSONRPCError(INVALID_PARAMS, f"Unknown tool: {name}")
    arguments = _as_object(params.get("arguments"), "Tool arguments")

    # Bad arguments are reported as a tool error, so the model can correct itself.
    if errors := validate_arguments(tool.input_schema, arguments):
        return _text_result("Invalid arguments:\n" + "\n".join(errors), is_error=True)

    try:
        output = tool.handler(ctx, arguments)
    except ToolError as e:
        logger.info(
            "care_mcp tool=%s user=%s outcome=error", name, ctx.user.external_id
        )
        payload = {"error": e.message}
        if e.data is not None:
            payload["details"] = e.data
        return _text_result(payload, is_error=True)
    logger.info("care_mcp tool=%s user=%s outcome=ok", name, ctx.user.external_id)
    if not isinstance(output, ToolOutput):
        output = ToolOutput(output)
    return _text_result(output.data, notes=output.notes)


def handle_prompts_list(ctx, params):
    return {
        "prompts": [
            {
                "name": name,
                "title": prompt["title"],
                "description": prompt["description"],
                "arguments": prompt["arguments"],
            }
            for name, prompt in PROMPTS.items()
        ]
    }


def handle_prompts_get(ctx, params):
    name = params.get("name")
    prompt = PROMPTS.get(name)
    if prompt is None:
        raise JSONRPCError(INVALID_PARAMS, f"Unknown prompt: {name}")
    arguments = _as_object(params.get("arguments"), "Prompt arguments")
    missing = [
        a["name"]
        for a in prompt["arguments"]
        if a["required"] and not arguments.get(a["name"])
    ]
    if missing:
        raise JSONRPCError(INVALID_PARAMS, f"Missing arguments: {', '.join(missing)}")
    encounter_id = arguments.get("encounter_id")
    text = prompt["template"].format(
        patient_id=arguments.get("patient_id", ""),
        facility_id=arguments.get("facility_id", ""),
        encounter_clause=f" in encounter {encounter_id}" if encounter_id else "",
        encounter_filter=f", passing encounter={encounter_id}" if encounter_id else "",
    )
    return {
        "description": prompt["description"],
        "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
    }


METHODS = {
    "initialize": handle_initialize,
    "ping": lambda ctx, params: {},
    "tools/list": handle_tools_list,
    "tools/call": handle_tools_call,
    "prompts/list": handle_prompts_list,
    "prompts/get": handle_prompts_get,
}


def error_response(id_, code, message, data=None):
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": id_, "error": error}


def handle_message(ctx: ToolContext, message: Any) -> dict | None:  # noqa: PLR0911
    """Handle one JSON-RPC message. Returns None for notifications and responses."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return error_response(None, INVALID_REQUEST, "Invalid JSON-RPC 2.0 message.")

    if "method" not in message:
        # A response to a server-initiated request; this server sends none.
        return None
    method = message["method"]
    if not isinstance(method, str):
        return error_response(
            message.get("id"), INVALID_REQUEST, "method must be a string."
        )
    if "id" not in message:
        # Notifications (notifications/initialized, notifications/cancelled…)
        # need no reply, and this stateless server keeps nothing to update.
        return None

    id_ = message["id"]
    handler = METHODS.get(method)
    if handler is None:
        return error_response(id_, METHOD_NOT_FOUND, f"Method not found: {method}")
    try:
        result = handler(ctx, _as_object(message.get("params"), "params"))
    except JSONRPCError as e:
        return error_response(id_, e.code, e.message, e.data)
    except Exception:
        logger.exception("care_mcp: %s failed", method)
        return error_response(id_, INTERNAL_ERROR, "Internal error.")
    return {"jsonrpc": "2.0", "id": id_, "result": result}
