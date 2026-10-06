"""Care's API as its own OpenAPI schema describes it.

The tools are generated from the schema drf-spectacular builds for Care's
Swagger docs, so every endpoint Care and its plugins publish is reachable with
no hand-written tool code, and a new endpoint is available as soon as it ships.
Better descriptions in that schema mean better tools here.
"""

import copy
import logging
import re
from dataclasses import dataclass
from functools import cache, cached_property

from django.urls import Resolver404, resolve
from drf_spectacular.generators import SchemaGenerator

from care_mcp.dispatch import API_PREFIX, PathNotAllowedError, is_blocked_path
from care_mcp.settings import setting_list

logger = logging.getLogger(__name__)

METHODS = ("get", "post", "put", "patch", "delete")
REF_PREFIX = "#/components/schemas/"
MAX_REF_DEPTH = 6

# Path parameters are ids, slugs and usernames. Anything else could change
# which route the request reaches.
SAFE_PATH_VALUE = re.compile(r"^[\w.@+:-]+$")
PATH_PARAMETER = re.compile(r"{(\w+)}")
# Stands in for every path parameter when resolving a path to its view.
PLACEHOLDER_ID = "00000000-0000-0000-0000-000000000000"

ACTION_LABELS = {
    "list": "List",
    "retrieve": "Get",
    "create": "Create",
    "update": "Replace",
    "partial_update": "Update",
    "destroy": "Delete",
}


def short_id(operation_id: str) -> str:
    """'api_v1_patient_allergy_intolerance_list' → 'patient_allergy_intolerance_list'"""
    for prefix in ("api_v1_", "api_"):
        if operation_id.startswith(prefix):
            return operation_id[len(prefix) :]
    return operation_id


def to_json_schema(schema):
    """OpenAPI 3.0 schema → JSON Schema: resolve nothing, fix `nullable`, drop titles."""
    if isinstance(schema, list):
        return [to_json_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    result = {}
    for key, value in schema.items():
        if key in ("title", "nullable", "readOnly"):
            continue
        if key == "properties":
            result[key] = {name: to_json_schema(s) for name, s in value.items()}
        else:
            result[key] = to_json_schema(value)
    if schema.get("nullable") and isinstance(result.get("type"), str):
        result["type"] = [result["type"], "null"]
    return result


class Resolver:
    """Inlines $refs into components, with a depth limit and cycle protection."""

    def __init__(self, components: dict):
        self.components = components

    def resolve(self, schema, depth=0, seen=()):
        if isinstance(schema, list):
            return [self.resolve(s, depth, seen) for s in schema]
        if not isinstance(schema, dict):
            return schema
        ref = schema.get("$ref")
        if isinstance(ref, str) and ref.startswith(REF_PREFIX):
            name = ref[len(REF_PREFIX) :]
            if name in seen or depth >= MAX_REF_DEPTH:
                return {"type": "object", "description": f"{name} (not expanded)"}
            target = self.components.get(name, {})
            return self.resolve(copy.deepcopy(target), depth + 1, (*seen, name))
        return {k: self.resolve(v, depth, seen) for k, v in schema.items()}


def _json_body_schema(operation: dict):
    content = operation.get("requestBody", {}).get("content", {})
    for media_type in ("application/json", *content):
        if media_type in content:
            return content[media_type].get("schema")
    return None


def _response_schema(operation: dict):
    for status in ("200", "201"):
        content = operation.get("responses", {}).get(status, {}).get("content", {})
        if "application/json" in content:
            return content["application/json"].get("schema")
    return None


def view_action(path: str, method: str) -> str:
    """The viewset action Care routes an operation to: "list", "retrieve"… or a
    custom @action such as "search". The operationId cannot tell these apart:
    drf-spectacular names POST /patient/search/ "patient_search_create"."""
    try:
        match = resolve(PATH_PARAMETER.sub(PLACEHOLDER_ID, path))
    except Resolver404:
        return ""
    return (getattr(match.func, "actions", None) or {}).get(method.lower(), "")


@dataclass
class Operation:
    id: str
    operation_id: str
    method: str
    path: str
    tag: str
    action: str
    raw: dict
    resolver: Resolver

    @property
    def parameters(self) -> list[dict]:
        return self.raw.get("parameters", [])

    @property
    def path_params(self) -> list[dict]:
        return [p for p in self.parameters if p.get("in") == "path"]

    @property
    def query_params(self) -> list[dict]:
        return [p for p in self.parameters if p.get("in") == "query"]

    @property
    def declared_params(self) -> set[str]:
        return {p["name"] for p in self.parameters}

    @property
    def is_read_only(self) -> bool:
        return self.method == "GET" or self.id in setting_list(
            "CARE_MCP_READ_ONLY_OPERATIONS"
        )

    @cached_property
    def body_schema(self):
        schema = _json_body_schema(self.raw)
        return to_json_schema(self.resolver.resolve(schema)) if schema else None

    @cached_property
    def response_schema(self) -> dict:
        """The success response, with $refs expanded two levels deep."""
        schema = _response_schema(self.raw)
        if not schema:
            return {}
        return self.resolver.resolve(schema, depth=MAX_REF_DEPTH - 2)

    @property
    def is_paginated(self) -> bool:
        return "results" in self.response_schema.get("properties", {})

    @property
    def response_fields(self) -> dict:
        """Top-level response fields as {name: type}, list results unwrapped."""
        schema = self.response_schema
        if self.is_paginated:
            schema = schema["properties"]["results"].get("items", {})
        elif schema.get("type") == "array":
            schema = schema.get("items", {})
        return {
            name: _type_label(prop)
            for name, prop in schema.get("properties", {}).items()
        }

    @property
    def summary(self) -> str:
        what = self.tag.replace("-", " ").replace("_", " ")
        if label := ACTION_LABELS.get(self.action):
            line = f"{label} {what}"
        elif self.action:
            line = f"{what.capitalize()}: {self.action.replace('_', ' ')}"
        else:
            line = what.capitalize()
        return f"{line} ({self.method} {self.path})"

    @property
    def description(self) -> str:
        parts = [self.summary + "."]
        if self.raw.get("description"):
            parts.append(self.raw["description"].strip())
        if self.is_paginated:
            parts.append("Returns {count, results}; page with limit and offset.")
        return " ".join(parts)

    def param_schema(self, param: dict) -> dict:
        schema = to_json_schema(self.resolver.resolve(param.get("schema", {})))
        if param.get("description"):
            schema = {**schema, "description": param["description"]}
        return schema

    def input_schema(self, *, with_body: bool) -> dict:
        """Flat JSON Schema for this operation's arguments: path and query
        parameters by name, plus `body` for operations that take one."""
        properties = {}
        required = []
        for param in self.path_params:
            properties[param["name"]] = self.param_schema(param)
            required.append(param["name"])
        for param in self.query_params:
            properties[param["name"]] = self.param_schema(param)
            if param.get("required"):
                required.append(param["name"])
        if with_body and self.body_schema:
            properties["body"] = self.body_schema
            if self.raw.get("requestBody", {}).get("required"):
                required.append("body")
        schema = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        return schema

    def build_request(self, arguments: dict) -> tuple[str, dict]:
        """Split flat arguments into the request path and query parameters."""
        values = {k: v for k, v in arguments.items() if k != "body"}

        def fill(match):
            name = match.group(1)
            value = str(values.pop(name, ""))
            if not value:
                msg = f"Missing path parameter: {name}"
                raise PathNotAllowedError(msg)
            if not SAFE_PATH_VALUE.match(value) or value in (".", ".."):
                msg = f"Invalid value for path parameter {name}."
                raise PathNotAllowedError(msg)
            return value

        return PATH_PARAMETER.sub(fill, self.path), values

    def describe(self) -> dict:
        """What get_operation returns: enough to call the operation correctly."""
        details = {
            "operation_id": self.id,
            "method": self.method,
            "path": self.path,
            "description": self.description,
            "path_params": {p["name"]: self.param_schema(p) for p in self.path_params},
            "query_params": {
                p["name"]: self.param_schema(p) for p in self.query_params
            },
        }
        if self.body_schema:
            details["body"] = self.body_schema
        if self.response_fields:
            key = "result_item_fields" if self.is_paginated else "response_fields"
            details[key] = self.response_fields
        return details

    def undeclared(self, arguments: dict) -> list[str]:
        """Arguments that are not parameters in Care's schema for this operation."""
        return sorted(set(arguments) - self.declared_params - {"body"})

    def search_text(self) -> str:
        names = " ".join(p["name"] for p in self.parameters)
        text = f"{self.id} {self.path} {self.tag} {self.raw.get('description', '')}"
        return f"{text} {names}".lower().replace("-", "_")


def _type_label(schema: dict) -> str:
    if "enum" in schema:
        return "one of " + "|".join(str(v) for v in schema["enum"][:12])
    kind = schema.get("type", "object")
    if isinstance(kind, list):
        kind = "/".join(kind)
    if schema.get("format"):
        kind = f"{kind}({schema['format']})"
    return kind


@cache
def operations() -> dict[str, Operation]:
    """Every Care API operation MCP may use, keyed by short operation id.

    Built once per process from the same generator that serves /api/schema/.
    """
    schema = SchemaGenerator().get_schema(request=None, public=True)
    resolver = Resolver(schema.get("components", {}).get("schemas", {}))
    found = {}
    for path, item in schema.get("paths", {}).items():
        if not path.startswith(API_PREFIX) or is_blocked_path(path):
            continue
        for method, raw in item.items():
            if method not in METHODS or "operationId" not in raw:
                continue
            op_id = short_id(raw["operationId"])
            if op_id in found:
                op_id = raw["operationId"]
            found[op_id] = Operation(
                id=op_id,
                operation_id=raw["operationId"],
                method=method.upper(),
                path=path,
                tag=(raw.get("tags") or [""])[0],
                action=view_action(path, method),
                raw=raw,
                resolver=resolver,
            )
    logger.info("care_mcp: indexed %d Care API operations", len(found))
    return found


def get_operation(operation_id: str) -> Operation | None:
    index = operations()
    return index.get(operation_id) or index.get(short_id(operation_id or ""))


def search(query: str, candidates, limit: int) -> list[Operation]:
    """Rank operations by how many of the query's words they mention."""
    words = [w for w in re.split(r"[^a-z0-9]+", query.lower().replace("-", "_")) if w]
    scored = []
    for op in candidates:
        text = op.search_text()
        score = sum(1 for w in words if w in text)
        if words and not score:
            continue
        # Prefer matches in the operation id, then shorter (more general) paths.
        id_hits = sum(1 for w in words if w in op.id)
        scored.append((-score, -id_hits, len(op.path), op.id, op))
    scored.sort(key=lambda s: s[:4])
    return [s[-1] for s in scored[:limit]]
