"""Natural-language message interpreter.

Converts free-form user text into a structured :class:`TurnPlan` by prompting
the Kiro CLI (the configured LLM provider) with the current plugin spec and
parsing its JSON response into draft operations.

This is the "planner" layer between the chat UI and the orchestrator: the user
describes what they want in plain English, this module figures out what
structured operations that maps to, and the orchestrator applies them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Union

from ..core.draft import ComponentKind
from ..core.generation import ArtifactKind, GenerationRequest
from ..core.spec_model import Component, FieldSchema, PluginSpec
from .operations import (
    AddComponent,
    DraftOperation,
    ModifyComponent,
    RemoveComponent,
    SetConnection,
    UpdateMetadata,
)
from .orchestrator import TurnPlan

__all__ = ["Interpreter", "InterpreterError"]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompt template
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are the interpretation layer for an InsightConnect plugin builder tool.
Given the user's natural-language request and the current plugin spec, produce
a JSON response describing the operations to perform on the plugin draft.

## Current plugin spec (YAML-like summary)

{spec_summary}

## Rules

1. Respond with ONLY valid JSON -- no markdown fences, no explanation outside the JSON.
2. The JSON must have this shape:

{{
  "operations": [
    {{
      "op": "add_component" | "modify_component" | "remove_component" | "set_connection" | "update_metadata",
      ... (fields depend on op, see below)
    }}
  ],
  "reasoning": [
    {{
      "kind": "action_logic" | "field_description" | "help_text",
      "parameters": {{ ... }}
    }}
  ],
  "clarification": null | "string asking the user for more detail"
}}

3. If the request is ambiguous or you cannot determine what to do, set
   "operations" to [], "reasoning" to [], and "clarification" to a helpful
   question asking the user what they meant.

4. Operation schemas:

   add_component:
     {{"op": "add_component", "kind": "action"|"trigger"|"task",
       "name": "snake_case_name",
       "component": {{"title": "...", "description": "...",
       "input": {{}}, "output": {{}}}}}}
   modify_component:
     {{"op": "modify_component", "kind": "action"|"trigger"|"task",
       "name": "existing_name",
       "component": {{"title": "...", "description": "...",
       "input": {{}}, "output": {{}}}}}}
   remove_component:
     {{"op": "remove_component", "kind": "action"|"trigger"|"task",
       "name": "existing_name"}}
   set_connection:
     {{"op": "set_connection", "fields": {{"field_name":
       {{"type": "string", "required": true,
       "title": "...", "description": "..."}}}}}}
   update_metadata:
     {{"op": "update_metadata", "name": "...", "title": "...",
       "description": "...", "vendor": "..."}}

5. Input/output fields use this schema:
   {{"type": "string"|"integer"|"boolean"|"object"|"array"|"float"|
     "date"|"bytes"|"password"|"credential_token"|
     "credential_username_password",
     "required": true|false, "title": "Human Title",
     "description": "What this field is."}}

6. IMPORTANT - two-phase workflow: By DEFAULT, only define plugin STRUCTURE
   (components, fields, connection, metadata). Leave "reasoning" as an EMPTY
   list [] so no code is generated yet. This lets the user review the structure
   before spending tokens on implementation.
   ONLY include "action_logic" reasoning entries when the user EXPLICITLY asks
   to generate/implement/write the code or logic (e.g. "generate the
   implementation", "write the action logic", "implement the actions"). When
   they do, add a reasoning entry with kind "action_logic" and parameters
   {{"action": "the_action_name"}} for each action needing logic.

7. Use snake_case for component and field names.
8. When adding an action, always include at least a title and description.
9. The "kind" for components is one of: action, trigger, task.

## User request

{user_message}
"""


def _spec_summary(spec: Optional[PluginSpec]) -> str:
    """Produce a compact text summary of the current spec for the LLM prompt."""
    if spec is None:
        return "(empty draft -- no plugin defined yet)"

    lines: List[str] = []
    lines.append(f"name: {spec.name}")
    lines.append(f"title: {spec.title}")
    lines.append(f"description: {spec.description}")
    lines.append(f"version: {spec.version}")
    lines.append(f"vendor: {spec.vendor}")

    if spec.connection:
        fields = ", ".join(
            f"{name}({fs.type}, {'required' if fs.required else 'optional'})" for name, fs in spec.connection.items()
        )
        lines.append(f"connection: {fields}")
    else:
        lines.append("connection: (none)")

    for kind_name in ("actions", "triggers", "tasks"):
        components: Dict[str, Component] = getattr(spec, kind_name, {}) or {}
        if components:
            names = ", ".join(components.keys())
            lines.append(f"{kind_name}: {names}")
        else:
            lines.append(f"{kind_name}: (none)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InterpreterError(Exception):
    """Raised when interpretation fails (LLM unreachable or unparseable output)."""

    def __init__(self, message: str, *, stdout: str = "", stderr: str = "") -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr


# ---------------------------------------------------------------------------
# Interpreter
# ---------------------------------------------------------------------------


@dataclass
class Interpreter:
    """Interprets user messages into TurnPlans via the Kiro CLI.

    Attributes:
        executable: the Kiro CLI command prefix (binary name or path).
    """

    executable: Union[str, Sequence[str]] = "kiro"

    @property
    def _command_prefix(self) -> List[str]:
        if isinstance(self.executable, str):
            return [self.executable]
        return [str(p) for p in self.executable]

    async def interpret(
        self,
        text: str,
        spec: Optional[PluginSpec],
        *,
        attachments: Optional[List[Dict[str, str]]] = None,
    ) -> TurnPlan:
        """Convert a user's natural-language message into a TurnPlan.

        Args:
            text: the raw user message from the chat.
            spec: the current plugin spec (may be None for a brand-new draft).
            attachments: optional list of file attachments (each with "name" and
                "content" keys), e.g. API specs the user uploaded for the LLM
                to digest when building the plugin.

        Returns:
            A :class:`TurnPlan` ready for the orchestrator's ``submit_message``.

        Raises:
            InterpreterError: if the Kiro CLI fails or its output cannot be parsed.
        """
        prompt = _SYSTEM_PROMPT.format(
            spec_summary=_spec_summary(spec),
            user_message=text,
        )

        # Append attached API specs / reference files to the prompt so the LLM
        # can use them to derive actions, fields, and types.
        # Large files are truncated to avoid exceeding context limits.
        if attachments:
            prompt += "\n\n## Attached reference files\n\n"
            for att in attachments:
                name = att.get("name", "unnamed")
                content = att.get("content", "")
                # Truncate large attachments to ~60KB to stay within context.
                max_attachment_chars = 60_000
                if len(content) > max_attachment_chars:
                    content = content[:max_attachment_chars] + "\n\n... (truncated, file too large to include fully)"
                prompt += f"### {name}\n\n```\n{content}\n```\n\n"

        command = [*self._command_prefix, "chat", "--no-interactive"]

        try:
            returncode, stdout, stderr = await self._invoke(command, prompt)
        except FileNotFoundError as error:
            raise InterpreterError(
                f"Kiro CLI not found at {self._command_prefix[0]!r}; " "ensure it is installed and on PATH.",
            ) from error

        if returncode != 0:
            raise InterpreterError(
                f"Kiro CLI interpretation failed (exit {returncode}): {stderr[:500]}",
                stdout=stdout,
                stderr=stderr,
            )

        return self._parse_response(stdout)

    def _parse_response(self, stdout: str) -> TurnPlan:
        """Parse the LLM's JSON response into a TurnPlan."""
        # Strip ANSI escape sequences (color codes) the Kiro CLI may embed.
        text = re.sub(r"\x1b\[[0-9;]*m", "", stdout).strip()

        # The Kiro CLI appends a credits/timing line like " ▸ Credits: 0.05 • Time: 2s"
        # Strip any lines starting with " ▸" or "▸" at the end.
        lines = text.split("\n")
        while lines and (lines[-1].strip().startswith("\u25b8") or lines[-1].strip() == ""):
            lines.pop()

        # Strip leading "> " prefix the CLI adds to output lines.
        cleaned = []
        for line in lines:
            if line.startswith("> "):
                cleaned.append(line[2:])
            else:
                cleaned.append(line)
        text = "\n".join(cleaned).strip()

        # The first line is often a language hint like "json" — skip it.
        if text and not text[0] in ("{", "["):
            first_brace = text.find("{")
            first_bracket = text.find("[")
            start = -1
            if first_brace >= 0 and first_bracket >= 0:
                start = min(first_brace, first_bracket)
            elif first_brace >= 0:
                start = first_brace
            elif first_bracket >= 0:
                start = first_bracket
            if start >= 0:
                text = text[start:]

        # Strip markdown code fences if the LLM wrapped the response.
        if text.startswith("```"):
            fence_lines = text.split("\n")
            if fence_lines[0].startswith("```"):
                fence_lines = fence_lines[1:]
            if fence_lines and fence_lines[-1].strip() == "```":
                fence_lines = fence_lines[:-1]
            text = "\n".join(fence_lines)

        try:
            data = json.loads(text)
        except json.JSONDecodeError as error:
            # If the LLM returned something unparseable, treat it as a
            # clarification response rather than crashing.
            logger.warning("Interpreter: unparseable LLM response: %s", error)
            # Write the raw response to a debug file for troubleshooting.
            _dump_debug(stdout, text, error)
            logger.warning("Interpreter: unparseable LLM response: %s", error)
            return TurnPlan(clarification="I had trouble understanding that. Could you rephrase your request?")

        if not isinstance(data, dict):
            return TurnPlan(clarification="I had trouble understanding that. Could you rephrase your request?")

        # If the LLM asked for clarification, surface that directly.
        clarification = data.get("clarification")
        if clarification:
            return TurnPlan(clarification=str(clarification))

        operations = self._parse_operations(data.get("operations", []))
        reasoning = self._parse_reasoning(data.get("reasoning", []))

        # If we got nothing useful, ask for clarification.
        if not operations and not reasoning:
            return TurnPlan(
                clarification="I wasn't able to determine a specific change from that. "
                "Could you describe the action, trigger, or connection you'd like to add or modify?"
            )

        return TurnPlan(operations=operations, reasoning=reasoning)

    def _parse_operations(self, raw_ops: Any) -> List[DraftOperation]:
        """Parse the operations array from the LLM response."""
        if not isinstance(raw_ops, list):
            return []

        operations: List[DraftOperation] = []
        for item in raw_ops:
            if not isinstance(item, dict):
                continue
            op = item.get("op", "")
            try:
                parsed = self._parse_single_operation(op, item)
                if parsed is not None:
                    operations.append(parsed)
            except (KeyError, ValueError, TypeError) as exc:
                logger.warning("Interpreter: skipping malformed operation %r: %s", item, exc)
                continue
        return operations

    def _parse_single_operation(self, op: str, item: Dict[str, Any]) -> Optional[DraftOperation]:
        """Parse one operation dict into a DraftOperation."""
        if op == "add_component":
            return AddComponent(
                kind=_parse_kind(item["kind"]),
                name=str(item["name"]),
                component=_parse_component(item.get("component", {})),
            )
        elif op == "modify_component":
            return ModifyComponent(
                kind=_parse_kind(item["kind"]),
                name=str(item["name"]),
                component=_parse_component(item.get("component", {})),
            )
        elif op == "remove_component":
            return RemoveComponent(
                kind=_parse_kind(item["kind"]),
                name=str(item["name"]),
            )
        elif op == "set_connection":
            fields = _parse_field_map(item.get("fields", {}))
            return SetConnection(connection=fields)
        elif op == "update_metadata":
            from ..core.spec_model import SemVer

            kwargs: Dict[str, Any] = {}
            for key in ("name", "title", "description", "vendor"):
                if key in item and item[key] is not None:
                    kwargs[key] = str(item[key])
            if "version" in item and item["version"] is not None:
                v = item["version"]
                if isinstance(v, str):
                    parts = v.split(".")
                    if len(parts) == 3:
                        kwargs["version"] = SemVer(int(parts[0]), int(parts[1]), int(parts[2]))
            return UpdateMetadata(**kwargs) if kwargs else None
        return None

    def _parse_reasoning(self, raw: Any) -> List[GenerationRequest]:
        """Parse the reasoning array from the LLM response.

        Code requests are passed through as the LLM named them. Ordering the
        generation of ``util/api.py`` before the action bodies that call it used
        to be arranged here, by prepending synthetic ``api_client`` and
        ``connection_logic`` requests. That is no longer this layer's concern:
        code implementation is delegated to the Kiro agent as a single task, and
        the agent decides the order in which it writes the interdependent files.
        """
        if not isinstance(raw, list):
            return []

        requests: List[GenerationRequest] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            kind_str = item.get("kind", "")
            try:
                kind = ArtifactKind(kind_str)
            except ValueError:
                continue
            params = item.get("parameters", {})
            if not isinstance(params, dict):
                params = {}
            requests.append(GenerationRequest(kind=kind, parameters=params))

        return requests

    async def _invoke(self, command: Sequence[str], prompt: str) -> tuple:
        """Run the Kiro CLI, feeding the prompt on stdin."""
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await process.communicate(prompt.encode("utf-8"))
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        returncode = process.returncode if process.returncode is not None else -1
        return returncode, stdout, stderr


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_kind(raw: Any) -> ComponentKind:
    """Convert a string kind to ComponentKind."""
    mapping = {
        "action": ComponentKind.ACTION,
        "trigger": ComponentKind.TRIGGER,
        "task": ComponentKind.TASK,
    }
    result = mapping.get(str(raw).lower())
    if result is None:
        raise ValueError(f"Unknown component kind: {raw!r}")
    return result


def _parse_component(raw: Any) -> Component:
    """Parse a component dict into a Component dataclass."""
    if not isinstance(raw, dict):
        return Component()
    return Component(
        title=raw.get("title"),
        description=raw.get("description"),
        input=_parse_field_map(raw.get("input", {})),
        output=_parse_field_map(raw.get("output", {})),
    )


def _parse_field_map(raw: Any) -> Dict[str, FieldSchema]:
    """Parse a field map dict into {name: FieldSchema}."""
    if not isinstance(raw, dict):
        return {}
    fields: Dict[str, FieldSchema] = {}
    for name, value in raw.items():
        if not isinstance(value, dict):
            continue
        fields[str(name)] = FieldSchema(
            type=value.get("type", "string"),
            required=bool(value.get("required", False)),
            title=value.get("title"),
            description=value.get("description"),
        )
    return fields


def _dump_debug(raw_stdout: str, cleaned_text: str, error: Exception) -> None:
    """Write a debug dump when LLM response parsing fails.

    Creates ~/.icplugin-builder/interpreter_debug.log with the raw CLI output
    and the cleaned text that was attempted to be parsed, so users can inspect
    what went wrong.
    """
    import os
    from pathlib import Path

    debug_dir = Path(os.path.expanduser("~/.icplugin-builder"))
    debug_dir.mkdir(parents=True, exist_ok=True)
    debug_path = debug_dir / "interpreter_debug.log"

    with open(debug_path, "w", encoding="utf-8") as f:
        f.write("=== INTERPRETER DEBUG LOG ===\n\n")
        f.write(f"Parse error: {error}\n\n")
        f.write("--- RAW STDOUT FROM KIRO CLI ---\n")
        f.write(raw_stdout)
        f.write("\n\n--- CLEANED TEXT (attempted to parse as JSON) ---\n")
        f.write(cleaned_text)
        f.write("\n\n--- FIRST 200 CHARS OF CLEANED TEXT ---\n")
        f.write(repr(cleaned_text[:200]))
        f.write("\n")

    logger.warning("Interpreter: debug dump written to %s", debug_path)
