"""MCP tools, generated from Care's OpenAPI schema.

Two kinds:

- Operation tools: the operations listed in CARE_MCP_TOOLS, each exposed as its
  own tool, named after its Swagger operationId (without "api_v1_") and taking
  that operation's path and query parameters (and body) as arguments.
- Three discovery tools that reach every other operation: search_operations,
  get_operation and call_operation.

Every call runs through Care's own views as the caller (see dispatch.py).
"""

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from care_mcp.dispatch import APIResult, PathNotAllowedError, call_api
from care_mcp.openapi import Operation, get_operation, operations, search
from care_mcp.settings import setting_list

logger = logging.getLogger(__name__)

MAX_TOOL_NAME = 64

# Appended to errors so the model knows how to recover instead of guessing.
STATUS_HINTS = {
    400: "Care rejected the request as invalid; check the arguments.",
    401: "The caller is not authenticated.",
    403: (
        "The account is not allowed to do this. For a patient's clinical records, "
        "pass encounter=<id> of an encounter the account has access to."
    ),
    404: "Not found, or not visible to this account.",
    405: "This operation does not support that method.",
}


class ToolError(Exception):
    """An error the model should see as a failed tool result, not a protocol error."""

    def __init__(self, message: str, data: Any = None):
        super().__init__(message)
        self.message = message
        self.data = data


@dataclass
class ToolOutput:
    """A tool's result, plus notes for the model shown after it."""

    data: Any
    notes: list[str] = field(default_factory=list)


def validate_arguments(schema: dict, arguments: dict) -> list[str]:
    """Check arguments against a tool's input schema; one line per problem."""
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(
        validator.iter_errors(arguments), key=lambda e: [str(p) for p in e.path]
    )
    return [
        f"{'.'.join(str(p) for p in e.path) or '(arguments)'}: {e.message}"
        for e in errors
    ]


@dataclass
class ToolContext:
    user: Any
    allow_writes: bool = False

    def can_call(self, op: Operation) -> bool:
        return op.is_read_only or self.allow_writes

    def check_allowed(self, op: Operation) -> None:
        if not self.can_call(op):
            msg = f"{op.id} changes data, and writes are disabled on this server."
            raise ToolError(msg)

    def run(self, op: Operation, arguments: dict) -> ToolOutput:
        self.check_allowed(op)
        try:
            path, query = op.build_request(arguments)
            result = call_api(
                self.user, op.method, path, query=query, body=arguments.get("body")
            )
        except PathNotAllowedError as e:
            raise ToolError(str(e)) from e
        output = ToolOutput(raise_for_status(result))
        # Care's views read some query parameters their schema does not list
        # (e.g. encounter on thread notes), so these are passed on, not rejected.
        if undeclared := op.undeclared(arguments):
            output.notes.append(
                f"Not in Care's API schema for {op.id}: {', '.join(undeclared)}. "
                "Care may have ignored these parameters; check that the results "
                "are filtered as intended."
            )
        return output


def raise_for_status(result: APIResult) -> Any:
    if result.ok:
        return result.data
    hint = STATUS_HINTS.get(result.status_code, "")
    message = f"Care API returned {result.status_code}. {hint}".strip()
    raise ToolError(message, data=result.data)


@dataclass
class Tool:
    name: str
    title: str
    description: str
    input_schema: dict
    handler: Any
    read_only: bool = True
    destructive: bool = False

    def definition(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {
                "title": self.title,
                "readOnlyHint": self.read_only,
                "destructiveHint": self.destructive,
                "idempotentHint": self.read_only,
                "openWorldHint": False,
            },
        }


def _tool_name(op_id: str) -> str:
    if len(op_id) <= MAX_TOOL_NAME:
        return op_id
    digest = hashlib.sha1(op_id.encode(), usedforsecurity=False).hexdigest()[:8]
    return f"{op_id[: MAX_TOOL_NAME - 9]}_{digest}"


def operation_tool(op: Operation) -> Tool:
    return Tool(
        name=_tool_name(op.id),
        title=op.summary.split(" (")[0],
        description=op.description,
        input_schema=op.input_schema(with_body=True),
        handler=lambda ctx, args: ctx.run(op, args),
        read_only=op.is_read_only,
        destructive=op.method in ("PUT", "PATCH", "DELETE"),
    )


def _callable_operations(ctx: ToolContext) -> list[Operation]:
    return [op for op in operations().values() if ctx.can_call(op)]


def search_operations(ctx: ToolContext, args: dict) -> dict:
    candidates = _callable_operations(ctx)
    if args.get("method"):
        candidates = [op for op in candidates if op.method == args["method"]]
    found = search(args.get("query", ""), candidates, args.get("limit", 20))
    return {
        "count": len(found),
        "operations": [
            {
                "operation_id": op.id,
                "summary": op.summary,
                "params": [
                    p["name"] + ("*" if p.get("in") == "path" else "")
                    for p in op.parameters
                ],
                **({"body": True} if op.body_schema else {}),
            }
            for op in found
        ],
        "note": "* marks path parameters, which are required.",
    }


def _lookup(args: dict) -> Operation:
    op = get_operation(args.get("operation_id", ""))
    if op is None:
        msg = (
            f"Unknown operation: {args.get('operation_id')}. "
            "Use search_operations to find one."
        )
        raise ToolError(msg)
    return op


def describe_operation(ctx: ToolContext, args: dict) -> dict:
    op = _lookup(args)
    details = op.describe()
    if not ctx.can_call(op):
        details["note"] = "Writes are disabled on this server; this cannot be called."
    return details


def call_operation(ctx: ToolContext, args: dict) -> ToolOutput:
    op = _lookup(args)
    ctx.check_allowed(op)
    arguments = dict(args.get("params") or {})
    if "body" in args:
        arguments["body"] = args["body"]
    if errors := validate_arguments(op.input_schema(with_body=True), arguments):
        msg = f"Invalid arguments for {op.id}:\n" + "\n".join(errors)
        raise ToolError(msg)
    return ctx.run(op, arguments)


def discovery_tools(ctx: ToolContext) -> list[Tool]:
    return [
        Tool(
            name="search_operations",
            title="Search Care API operations",
            description=(
                "Find Care API operations by keyword when no dedicated tool fits, "
                'e.g. "location", "schedule", "inventory", "valueset". Returns '
                "operation ids with their parameters; call one with call_operation."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Words to look for."},
                    "method": {"enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 20,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            handler=search_operations,
        ),
        Tool(
            name="get_operation",
            title="Describe a Care API operation",
            description=(
                "Parameters, request body schema and response fields of one Care "
                "API operation, from Care's OpenAPI schema."
            ),
            input_schema={
                "type": "object",
                "properties": {"operation_id": {"type": "string"}},
                "required": ["operation_id"],
                "additionalProperties": False,
            },
            handler=describe_operation,
        ),
        Tool(
            name="call_operation",
            title="Call a Care API operation",
            description=(
                "Call any Care API operation by id. Put path and query parameters in "
                "params and the JSON request body in body. "
                + (
                    "Operations that change data are enabled: only make a change "
                    "the user has clearly asked for, and read the record first."
                    if ctx.allow_writes
                    else "Only operations that read data are available."
                )
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "operation_id": {"type": "string"},
                    "params": {
                        "type": "object",
                        "description": "Path and query parameters by name.",
                    },
                    "body": {"type": "object", "description": "JSON request body."},
                },
                "required": ["operation_id"],
                "additionalProperties": False,
            },
            handler=call_operation,
            read_only=not ctx.allow_writes,
            destructive=ctx.allow_writes,
        ),
    ]


def available_tools(ctx: ToolContext) -> list[Tool]:
    """The tools this connection gets, in the order clients should list them."""
    tools = []
    for op_id in setting_list("CARE_MCP_TOOLS"):
        op = get_operation(op_id)
        if op is None:
            logger.warning("care_mcp: CARE_MCP_TOOLS names unknown operation %s", op_id)
            continue
        if ctx.can_call(op):
            tools.append(operation_tool(op))
    tools.extend(discovery_tools(ctx))
    return tools
