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

from ..core.cost_controller import CostController
from ..core.draft import ComponentKind
from ..core.generation import ArtifactKind, GenerationRequest
from ..core.spec_model import Component, FieldSchema, PluginSpec
from ..integrations.llm_generator import estimate_tokens
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
  "clarification": null | "string asking the user for more detail",
  "vendor_api": null | "vendor or product name",
  "proceed_without_reference": true | false
}}

3. If the request is ambiguous or you cannot determine what to do, set
   "operations" to [], "reasoning" to [], and "clarification" to a helpful
   question asking the user what they meant.

3a. Set "vendor_api" to the vendor or product name when this plugin calls an
   external company's HTTP API -- Okta, CrowdStrike, Jira, VirusTotal, and so on.
   Set it to null when the plugin does its work locally and calls nobody: string
   or date manipulation, encoding, hashing, regular expressions, arithmetic,
   parsing a value it was handed. The distinction matters because the tool cannot
   look up an API it has not been given documentation for, and would otherwise
   invent endpoints. A false null produces a plugin built on guesses; a false
   vendor name produces a pointless question. Judge from what the plugin would
   have to *do*, not from whether a company is mentioned in passing.

3b. Set "proceed_without_reference" to true only when the user has been asked for
   API documentation and is explicitly declining to supply it -- "go ahead
   anyway", "just make your best guess", "I don't have the docs, continue".
   Default false. Never infer it from silence.

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


#: How much of one attachment reaches the interpreter's prompt. The cap itself is
#: unchanged by clause 2.20 -- what changes is that exceeding it is disclosed.
MAX_ATTACHMENT_CHARS = 60_000


@dataclass(frozen=True)
class TruncationNotice:
    """One attachment the interpreter could not read in full.

    Attributes:
        name: the attachment as the user named it.
        full_chars: its whole size.
        included_chars: how much of it reached the prompt.

    The distinction this exists to draw: "the interpreter saw less of your spec" is
    not "your plugin was built from less of your spec". The delegated agent receives
    the whole file, because attachments are written verbatim into
    ``.builder/reference/`` -- so a truncated interpretation can still produce a
    plugin built against the complete document, and the operator should be told which
    of the two happened.
    """

    name: str
    full_chars: int
    included_chars: int
    detail: str

    @classmethod
    def for_attachment(cls, name: str, full_chars: int, included_chars: int) -> "TruncationNotice":
        """Build a notice, rendering the disclosure as part of the data.

        The rendered sentence is a *field* rather than a method, so it travels with
        the notice wherever the notice goes. A serializer that forgot to call a
        method would drop the half of the disclosure that makes it actionable.
        """
        dropped = full_chars - included_chars
        return cls(
            name=name,
            full_chars=full_chars,
            included_chars=included_chars,
            detail=(
                f"{name}: {dropped:,} of {full_chars:,} characters were not shown to the interpreter "
                f"({included_chars:,} included). The delegated agent receives the whole file, so this "
                "limits how the request was understood rather than what the plugin was built from."
            ),
        )

    @property
    def dropped_chars(self) -> int:
        """How much of the attachment the interpreter did not see."""
        return self.full_chars - self.included_chars

    def message(self) -> str:
        """The disclosure as the operator reads it."""
        return self.detail


@dataclass
class Interpreter:
    """Interprets user messages into TurnPlans via the Kiro CLI.

    Attributes:
        executable: the Kiro CLI command prefix (binary name or path).
        cost_controller: the controller a paid interpretation is recorded against.
            Optional so a caller that only wants a parsed plan need not supply one,
            but the API wires it: an interpretation runs a subprocess against the
            model and is paid for, and parent Property 9 already required every paid
            invocation be counted -- the interpreter was simply outside it, which is
            why a session total sat at zero and then jumped after the agent run.
    """

    executable: Union[str, Sequence[str]] = "kiro"
    cost_controller: Optional[CostController] = None

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
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> TurnPlan:
        """Convert a user's natural-language message into a TurnPlan.

        Args:
            text: the raw user message from the chat.
            spec: the current plugin spec (may be None for a brand-new draft).
            attachments: optional list of file attachments (each with "name" and
                "content" keys), e.g. API specs the user uploaded for the LLM
                to digest when building the plugin.
            session_id: the session this interpretation is charged to. Usage is
                recorded when it is supplied and a controller is configured;
                omitting it leaves the call uncounted, which is what a caller
                outside a session wants.
            user_id: carried for symmetry with the other paid invocations.

        Note:
            Gating the interpreter through :meth:`CostController.authorize` is
            deliberately **not** done here. A budget-exhausted session that cannot
            parse its own message is a different decision from one that cannot
            generate, and it is recorded as out of scope rather than taken silently.

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
        notices: List[TruncationNotice] = []
        if attachments:
            prompt += "\n\n## Attached reference files\n\n"
            for att in attachments:
                name = att.get("name", "unnamed")
                content = att.get("content", "")
                # Clause 2.20: the cap is unchanged; what changes is that exceeding it
                # is disclosed. A 206KB OpenAPI document had its `/systemusers` paths
                # at roughly byte 65,000 -- outside the interpreter's view -- and
                # nothing told the operator, so a request understood from two thirds
                # of a spec looked identical to one understood from all of it.
                if len(content) > MAX_ATTACHMENT_CHARS:
                    notices.append(
                        TruncationNotice.for_attachment(
                            name=name,
                            full_chars=len(content),
                            included_chars=MAX_ATTACHMENT_CHARS,
                        )
                    )
                    content = content[:MAX_ATTACHMENT_CHARS] + "\n\n... (truncated, file too large to include fully)"
                prompt += f"### {name}\n\n```\n{content}\n```\n\n"

        command = [*self._command_prefix, "chat", "--no-interactive"]

        try:
            returncode, stdout, stderr = await self._invoke(command, prompt)
        except FileNotFoundError as error:
            self._record(session_id, prompt, succeeded=False)
            raise InterpreterError(
                f"Kiro CLI not found at {self._command_prefix[0]!r}; " "ensure it is installed and on PATH.",
            ) from error

        if returncode != 0:
            # Clause 2.18 and parent Req 3.7: a failed invocation is excluded from
            # the total, exactly as LLMGenerator and PluginAgent exclude theirs.
            self._record(session_id, prompt, succeeded=False)
            raise InterpreterError(
                f"Kiro CLI interpretation failed (exit {returncode}): {stderr[:500]}",
                stdout=stdout,
                stderr=stderr,
            )

        self._record(session_id, prompt + stdout, succeeded=True)
        plan = self._parse_response(stdout)
        plan.truncation_notices = tuple(notices)
        return plan

    def _record(self, session_id: Optional[str], billed_text: str, *, succeeded: bool) -> None:
        """Record this interpretation's usage against ``session_id``.

        A lower bound rather than a measurement, like the agent's: it covers the
        prompt and the response, and cannot see what the model did in between. That
        is still the difference between a session total that accounts for every paid
        call and one that reports zero until the agent runs.
        """
        if self.cost_controller is None or session_id is None:
            return
        self.cost_controller.record_usage(session_id, estimate_tokens(billed_text), succeeded=succeeded)

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

        # Carried on every plan, including a clarifying one: a user who answers an
        # ambiguity question in the same breath as declining to supply docs should
        # not have to say it twice.
        vendor_api = _parse_vendor_api(data.get("vendor_api"))
        proceed_without_reference = data.get("proceed_without_reference") is True

        # If the LLM asked for clarification, surface that directly.
        clarification = data.get("clarification")
        if clarification:
            return TurnPlan(
                clarification=str(clarification),
                vendor_api=vendor_api,
                proceed_without_reference=proceed_without_reference,
            )

        operations = self._parse_operations(data.get("operations", []))
        reasoning = self._parse_reasoning(data.get("reasoning", []))

        # If we got nothing useful, ask for clarification.
        if not operations and not reasoning:
            return TurnPlan(
                clarification="I wasn't able to determine a specific change from that. "
                "Could you describe the action, trigger, or connection you'd like to add or modify?",
                vendor_api=vendor_api,
                proceed_without_reference=proceed_without_reference,
            )

        return TurnPlan(
            operations=operations,
            reasoning=reasoning,
            vendor_api=vendor_api,
            proceed_without_reference=proceed_without_reference,
        )

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


def _parse_vendor_api(raw: Any) -> Optional[str]:
    """Return the vendor name from an interpreted response, or ``None``.

    Defensive about the shapes a model actually returns in this field: the string
    ``"null"``, ``"none"``, or an empty string all mean "no vendor", and treating
    any of them as a vendor name would produce a request for documentation about a
    vendor called "null".
    """
    if not isinstance(raw, str):
        return None
    name = raw.strip()
    if not name or name.lower() in ("null", "none", "n/a", "na", "false"):
        return None
    return name
