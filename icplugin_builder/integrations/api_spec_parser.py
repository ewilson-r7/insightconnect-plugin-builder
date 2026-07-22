"""OpenAPI spec parser for extracting per-action endpoint details.

Given raw OpenAPI JSON/YAML content and an action name, this module finds the
most likely matching endpoint and extracts the HTTP method, path, query parameters,
request body schema, response schema, and expected success status code.

The matching is fuzzy — it maps InsightConnect action names (snake_case like
``get_client``, ``create_hunt``) to OpenAPI path/operation combinations by
comparing the action name against the operationId, path segments, and summary.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = ["EndpointDetails", "parse_openapi_spec", "find_endpoint_for_action"]


@dataclass
class EndpointDetails:
    """Extracted API endpoint details for a single action."""

    http_method: str = ""
    path: str = ""
    query_params: List[str] = field(default_factory=list)
    request_body: str = ""
    response_shape: str = ""
    success_status: int = 200
    summary: str = ""


def parse_openapi_spec(content: str) -> Optional[Dict[str, Any]]:
    """Parse an OpenAPI spec from JSON or YAML string.

    Returns the parsed dict or None if parsing fails.
    """
    if not content or not content.strip():
        return None

    # Try JSON first (most common for attached specs)
    try:
        return json.loads(content)
    except (json.JSONDecodeError, ValueError):
        pass

    # Try YAML
    try:
        from ruamel.yaml import YAML

        yaml = YAML()
        result = yaml.load(content)
        if isinstance(result, dict):
            return result
    except Exception:
        pass

    return None


def find_endpoint_for_action(spec: Dict[str, Any], action_name: str) -> Optional[EndpointDetails]:
    """Find the best-matching endpoint for an action name in an OpenAPI spec.

    Uses fuzzy matching against operationId, path segments, and summary to find
    the endpoint that corresponds to the given InsightConnect action name.

    Args:
        spec: parsed OpenAPI spec dict (OpenAPI 3.x format).
        action_name: the snake_case action name (e.g. "get_client", "create_hunt").

    Returns:
        An EndpointDetails if a match is found, None otherwise.
    """
    paths = spec.get("paths", {})
    if not paths:
        return None

    # Build a list of all operations with their scores
    candidates: List[Tuple[float, str, str, Dict[str, Any]]] = []

    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method in ("get", "post", "put", "patch", "delete"):
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            score = _match_score(action_name, method, path, operation)
            if score > 0:
                candidates.append((score, method, path, operation))

    if not candidates:
        return None

    # Pick the best match
    candidates.sort(key=lambda x: x[0], reverse=True)
    _, best_method, best_path, best_op = candidates[0]

    return _extract_details(spec, best_method, best_path, best_op)


def _match_score(action_name: str, method: str, path: str, operation: Dict[str, Any]) -> float:
    """Score how well an operation matches an action name (higher = better)."""
    score = 0.0
    action_words = set(action_name.lower().split("_"))

    # Check operationId (strongest signal)
    op_id = operation.get("operationId", "")
    if op_id:
        op_id_lower = op_id.lower()
        op_id_normalized = re.sub(r"[^a-z0-9]", "_", op_id_lower)
        if action_name.lower() == op_id_normalized:
            score += 100
        elif action_name.lower() in op_id_lower or op_id_lower in action_name.lower():
            score += 50
        else:
            op_words = set(re.split(r"[^a-z0-9]+", op_id_lower))
            overlap = action_words & op_words
            score += len(overlap) * 15

    # Check path segments
    path_segments = set(re.split(r"[/{}\-]+", path.lower()))
    path_segments.discard("")
    path_overlap = action_words & path_segments
    score += len(path_overlap) * 10

    # Check HTTP method alignment with action prefix
    method_map = {
        "get": {"get", "list", "search", "fetch", "retrieve"},
        "post": {"create", "add", "start", "collect", "run"},
        "put": {"update", "set", "replace"},
        "patch": {"update", "modify", "change"},
        "delete": {"delete", "remove", "cancel"},
    }
    expected_methods = method_map.get(method, set())
    if action_words & expected_methods:
        score += 5

    # Check summary/description
    summary = operation.get("summary", "").lower()
    description = operation.get("description", "").lower()
    for word in action_words:
        if word in summary:
            score += 3
        if word in description:
            score += 1

    return score


def _extract_details(spec: Dict[str, Any], method: str, path: str, operation: Dict[str, Any]) -> EndpointDetails:
    """Extract structured endpoint details from a matched operation."""
    details = EndpointDetails(
        http_method=method.upper(),
        path=path,
        summary=operation.get("summary", ""),
    )

    # Extract query parameters
    parameters = operation.get("parameters", [])
    for param in parameters:
        if not isinstance(param, dict):
            continue
        if param.get("in") == "query":
            name = param.get("name", "")
            if name:
                details.query_params.append(name)

    # Extract request body schema summary
    request_body = operation.get("requestBody", {})
    if isinstance(request_body, dict):
        content = request_body.get("content", {})
        json_content = content.get("application/json", {})
        schema = json_content.get("schema", {})
        if schema:
            details.request_body = _summarize_schema(spec, schema)

    # Extract response schema
    responses = operation.get("responses", {})
    # Find the success response (2xx)
    for status_code in ("200", "201", "204", "202"):
        if status_code in responses:
            details.success_status = int(status_code)
            response = responses[status_code]
            if isinstance(response, dict) and int(status_code) != 204:
                resp_content = response.get("content", {})
                json_resp = resp_content.get("application/json", {})
                resp_schema = json_resp.get("schema", {})
                if resp_schema:
                    details.response_shape = _summarize_schema(spec, resp_schema)
            break

    return details


def _summarize_schema(spec: Dict[str, Any], schema: Dict[str, Any], depth: int = 0) -> str:
    """Produce a compact text summary of a JSON schema for the LLM prompt.

    Resolves $ref pointers (one level deep) and summarizes object properties,
    arrays, and primitive types into a readable format.
    """
    if depth > 3:
        return "{...}"

    # Resolve $ref
    if "$ref" in schema:
        ref_path = schema["$ref"]
        resolved = _resolve_ref(spec, ref_path)
        if resolved:
            schema = resolved
        else:
            # Return the ref name as a hint
            return ref_path.split("/")[-1]

    schema_type = schema.get("type", "object")

    if schema_type == "object":
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        if not properties:
            return "{object}"
        parts = []
        for prop_name, prop_schema in list(properties.items())[:15]:
            prop_type = prop_schema.get("type", "string")
            if "$ref" in prop_schema:
                prop_type = prop_schema["$ref"].split("/")[-1]
            req_marker = " (required)" if prop_name in required else ""
            parts.append(f"  {prop_name}: {prop_type}{req_marker}")
        result = "{\n" + "\n".join(parts)
        if len(properties) > 15:
            result += f"\n  ... ({len(properties) - 15} more fields)"
        result += "\n}"
        return result

    elif schema_type == "array":
        items = schema.get("items", {})
        item_summary = _summarize_schema(spec, items, depth + 1)
        return f"[{item_summary}]"

    else:
        return schema_type


def _resolve_ref(spec: Dict[str, Any], ref: str) -> Optional[Dict[str, Any]]:
    """Resolve a JSON $ref pointer within the spec."""
    if not ref.startswith("#/"):
        return None
    parts = ref[2:].split("/")
    current: Any = spec
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current if isinstance(current, dict) else None
