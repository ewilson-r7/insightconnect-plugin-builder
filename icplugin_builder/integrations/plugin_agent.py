"""Delegated plugin implementation: the Kiro CLI run as an agent, not a prompt.

This is the seam that does the plugin development work. It runs the Kiro CLI
**agentically** -- with tools and a working directory -- so the CLI reads the
spec, writes the API client, the connection, the action bodies and the unit
tests, runs ``insight-plugin`` and the linters, reads the failures, and fixes
them. That is the same loop the operator drives by hand in the IDE, and it is
what produces a plugin that runs.

It replaces a single-shot approach that could not work: prompt the CLI for a
snippet of Python, then splice its stdout into ``action.py`` with a regex and a
fixed indent. Chat stdout is a *transcript* -- narration, tool calls, a credits
footer -- not a payload, so the splice produced syntactically invalid files and,
on occasion, wrote the model's own deliberation into the plugin. Nothing here
parses code out of stdout. The agent edits the files; this module runs it,
observes what changed on disk, and accounts for the cost.

Guarantees this module holds:

* **The prompt goes on stdin, never argv.** Nothing sensitive lands in the
  process list and there is no ``E2BIG`` ceiling on context.
* **The child gets a default-deny environment** (:mod:`.env_guard`). This process
  holds decrypted tenant API keys and git credentials; the LLM CLI has no
  business seeing them.
* **Tool trust is explicit and narrow.** ``--trust-tools`` names exactly the
  tools the agent config grants, rather than blanket-trusting everything.
* **Failure is surfaced, never swallowed.** A non-zero exit or a missing binary
  raises with the captured stderr attached.
* **What changed is observed, not claimed.** The set of modified files comes from
  comparing a snapshot of the tree before and after the run, so it is accurate
  even if the agent's narration is not.
* **Every run is cost-gated** through the :class:`CostController`, exactly as the
  prior single-shot path was.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence, Tuple, Union

from icplugin_builder.core.cost_controller import CostController
from icplugin_builder.integrations.agent_config import AGENT_NAME, DEFAULT_TOOLS
from icplugin_builder.integrations.env_guard import KIRO_ALLOW_PREFIXES, guarded_env
from icplugin_builder.integrations.insight_plugin_cli import snapshot_tree
from icplugin_builder.integrations.llm_generator import (
    CostLimitError,
    LLMGeneratorError,
    estimate_tokens,
)

__all__ = [
    "DEFAULT_EXECUTABLE",
    "DEFAULT_TIMEOUT_SECONDS",
    "PluginAgentError",
    "AgentRunResult",
    "PluginAgent",
    "parse_credits",
    "strip_transcript_noise",
]

#: The Kiro CLI executable name, resolved on ``PATH``. Installed as a wrapper
#: rather than a symlink -- the launcher locates its sibling binaries relative to
#: its own path, so a symlink outside the app bundle breaks it.
DEFAULT_EXECUTABLE = "kiro-cli"

#: How long a delegated implementation run may take. Generous on purpose: this is
#: a full implement-verify-fix loop over several files, not a single completion.
#: Matches the Code_Validator's per-stage ceiling (Req 8.8) for consistency, and
#: is unrelated to the 30s draft-generation bound of Req 1.2, which governs
#: interpreting a message rather than implementing a plugin.
DEFAULT_TIMEOUT_SECONDS = 600.0

#: The Kiro CLI's trailing usage footer, e.g. " ▸ Credits: 0.14 • Time: 8s".
_CREDITS_RE = re.compile(r"Credits:\s*([0-9]+(?:\.[0-9]+)?)")

#: Any CSI escape sequence, not only the colour (``m``-terminated) ones. The
#: usage footer arrives on stderr surrounded by cursor-control sequences
#: (``\x1b[?25l``, ``\x1b[1G``), which a colour-only pattern leaves behind.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

#: Lines the CLI emits as chrome rather than content.
_NOISE_PREFIXES = (
    "All tools are now trusted",
    "Agents can sometimes do unexpected things",
    "Learn more at https://kiro.dev",
)


class PluginAgentError(LLMGeneratorError):
    """Raised when a delegated agent run cannot be dispatched or fails.

    Subclasses :class:`LLMGeneratorError` so existing orchestrator error handling
    -- which already halts the turn without committing on that type (Req 1.7,
    3.7) -- covers delegated runs without change.
    """


@dataclass(frozen=True)
class AgentRunResult:
    """The observed outcome of one delegated implementation run.

    Attributes:
        succeeded: whether the CLI exited ``0``.
        summary: the agent's closing report (its ``>``-prefixed lines), which is
            where it states what it did and what is outstanding.
        transcript: the full cleaned stdout, retained for display and debugging.
        changed_files: POSIX-relative paths that were added or modified in the
            project tree during the run, observed by snapshot comparison.
        credits: the CLI's self-reported credit spend -- the only usage figure it
            actually measures. ``None`` means it reported none, which is distinct
            from a spend of zero.
        tokens: the figure recorded against the session token budget. A lower
            bound rather than a measurement: it covers the instruction and the
            transcript, and cannot see the file contents the agent read.
        session_total: the cumulative session token total after recording.
        returncode: the CLI's exit status.
        stderr: captured standard error.
    """

    succeeded: bool
    summary: str
    transcript: str
    changed_files: Tuple[str, ...] = ()
    credits: Optional[float] = None
    tokens: int = 0
    session_total: int = 0
    returncode: int = 0
    stderr: str = ""


def strip_transcript_noise(stdout: str) -> str:
    """Strip ANSI codes and CLI chrome from a transcript.

    Deliberately minimal. The old code path had to aggressively reconstruct
    *code* from this stream -- discarding markdown fences, guessing at inner
    monologue by prefix, re-deriving indentation -- because it was trying to
    recover a payload from a transcript. The agent writes files directly now, so
    the transcript only has to be readable by a human. Nothing downstream parses
    it, and no file content is derived from it.

    Args:
        stdout: raw captured standard output.

    Returns:
        The transcript with escape sequences and banner lines removed.
    """
    text = _ANSI_RE.sub("", stdout)
    kept = [line for line in text.split("\n") if not line.strip().startswith(_NOISE_PREFIXES)]
    return "\n".join(kept).strip()


def parse_credits(*streams: str) -> Optional[float]:
    """Extract the CLI's self-reported credit spend from its usage footer.

    The Kiro CLI reports cost in *credits* rather than tokens, so this is the only
    figure it actually measures; everything else this module records about usage
    is an estimate.

    **The footer is written to stderr, not stdout.** Both streams are accepted
    because that is easy to get wrong: reading only stdout finds the footer when
    the two streams have been merged (as they are at an interactive terminal, or
    under ``2>&1``) and silently finds nothing when they are captured separately,
    which is how this module runs the CLI.

    Args:
        *streams: any captured output streams, in any order.

    Returns:
        The reported credits, or ``None`` when no footer was present. ``None``
        means "not reported" and is deliberately distinct from ``0.0``, which
        would claim a free run.
    """
    for stream in reversed(streams):
        matches = _CREDITS_RE.findall(_ANSI_RE.sub("", stream or ""))
        if matches:
            try:
                return float(matches[-1])
            except ValueError:  # pragma: no cover - regex guarantees a numeric match
                return None
    return None


def _summary_lines(transcript: str) -> str:
    """Extract the agent's closing report (the ``> ``-prefixed lines)."""
    lines = [line[2:] if line.startswith("> ") else line for line in transcript.split("\n") if line.startswith("> ")]
    return "\n".join(lines).strip()


class PluginAgent:
    """Runs the Kiro CLI as an agent to implement a plugin in place."""

    def __init__(
        self,
        cost_controller: CostController,
        *,
        executable: Union[str, Sequence[str]] = DEFAULT_EXECUTABLE,
        agent_name: str = AGENT_NAME,
        tools: Sequence[str] = DEFAULT_TOOLS,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        model: Optional[str] = None,
    ) -> None:
        """Configure the delegation seam.

        Args:
            cost_controller: the controller every run is authorized and recorded
                against.
            executable: the Kiro CLI command -- a binary name/path or a full
                argument-vector prefix.
            agent_name: the agent config to select with ``--agent``.
            tools: the tools to trust, which must match what the agent config
                grants; a tool that is available but untrusted would stall a
                non-interactive run waiting for a confirmation nobody can give.
            timeout_seconds: ceiling for one run.
            model: an explicit model id, or ``None`` to use the CLI's configured
                default.
        """
        self._cost_controller = cost_controller
        if isinstance(executable, str):
            self._command_prefix = [executable]
        else:
            self._command_prefix = [str(part) for part in executable]
        self._agent_name = agent_name
        self._tools = tuple(str(tool) for tool in tools)
        self._timeout_seconds = timeout_seconds
        self._model = model

    @property
    def cost_controller(self) -> CostController:
        """The :class:`CostController` this agent gates through."""
        return self._cost_controller

    def build_command(self) -> Tuple[str, ...]:
        """Build the argument vector for one run.

        ``--trust-tools`` names the granted tools explicitly rather than using
        ``--trust-all-tools``, so the trusted set is visible here and stays in
        step with the agent config.
        """
        command = [
            *self._command_prefix,
            "chat",
            "--no-interactive",
            "--agent",
            self._agent_name,
            f"--trust-tools={','.join(self._tools)}",
        ]
        if self._model:
            command.extend(["--model", self._model])
        return tuple(command)

    async def implement(
        self,
        project_dir: Union[str, Path],
        instruction: str,
        *,
        session_id: str,
        user_id: str,
    ) -> AgentRunResult:
        """Run the agent against ``project_dir`` until it reports the task done.

        Args:
            project_dir: the plugin working tree; becomes the child's working
                directory, so the agent's relative file operations land in the
                right plugin and cannot reach a sibling project.
            instruction: the task for this run. Task only -- the standing rules
                and the definition of done live in the agent config, and the
                plugin conventions live in the skills it references.
            session_id: the session whose token budget governs and records the run.
            user_id: the user whose per-minute request rate governs the run.

        Returns:
            An :class:`AgentRunResult` describing what the run did and changed.

        Raises:
            ValueError: if ``project_dir`` is not an existing directory.
            CostLimitError: if the Cost_Controller blocks the run; nothing is
                dispatched (Req 4.2, 4.5).
            PluginAgentError: if the CLI is missing, times out, or exits
                non-zero. The captured stderr is attached (Req 19.1).
        """
        root = Path(project_dir)
        if not root.is_dir():
            raise ValueError(f"project directory does not exist: {root}")

        decision = self._cost_controller.authorize(session_id, user_id)
        if not decision.authorized:
            raise CostLimitError(decision)

        command = list(self.build_command())
        before = snapshot_tree(root)

        try:
            returncode, stdout, stderr = await self._invoke(command, instruction, cwd=root)
        except FileNotFoundError as error:
            self._cost_controller.record_usage(session_id, estimate_tokens(instruction), succeeded=False)
            raise PluginAgentError(
                f"Kiro CLI executable not found: {self._command_prefix[0]!r}; "
                "install the Kiro CLI and ensure it is on PATH",
                command=command,
            ) from error
        except asyncio.TimeoutError as error:
            self._cost_controller.record_usage(session_id, estimate_tokens(instruction), succeeded=False)
            raise PluginAgentError(
                f"the delegated agent run exceeded {self._timeout_seconds:.0f}s and was aborted",
                command=command,
            ) from error

        transcript = strip_transcript_noise(stdout)
        credits = parse_credits(stdout, stderr)
        after = snapshot_tree(root)
        changed = _changed_paths(before.files, after.files)

        if returncode != 0:
            # Exclude the failed run from the session total (Req 3.7) but still
            # report what it managed to change, so a partial run is visible.
            self._cost_controller.record_usage(session_id, estimate_tokens(instruction), succeeded=False)
            raise PluginAgentError(
                f"the delegated agent run failed with exit code {returncode}",
                command=command,
                returncode=returncode,
                stdout=transcript,
                stderr=stderr,
            )

        # The session budget is denominated in tokens (Req 4.1) but the CLI
        # reports credits, and an agentic run's real consumption includes every
        # file it chose to read -- which never appears in either the instruction
        # or the transcript. This figure is therefore a *floor*, not a
        # measurement: it keeps the budget monotonic and bounded, and `credits`
        # carries the only number the CLI actually measured.
        tokens = estimate_tokens(instruction) + estimate_tokens(transcript)
        session_total = self._cost_controller.record_usage(session_id, tokens, succeeded=True)

        return AgentRunResult(
            succeeded=True,
            summary=_summary_lines(transcript),
            transcript=transcript,
            changed_files=changed,
            credits=credits,
            tokens=tokens,
            session_total=session_total,
            returncode=returncode,
            stderr=stderr,
        )

    async def _invoke(
        self,
        command: Sequence[str],
        prompt: str,
        *,
        cwd: Path,
    ) -> Tuple[int, str, str]:
        """Run the CLI in ``cwd``, feeding ``prompt`` on stdin.

        The child receives a default-deny environment, so it cannot read this
        process's decrypted credentials or the operator's unrelated provider
        secrets out of the inherited environment.
        """
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=guarded_env(KIRO_ALLOW_PREFIXES),
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(prompt.encode("utf-8")),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError:
            await _terminate(process)
            raise

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        returncode = process.returncode if process.returncode is not None else -1
        return returncode, stdout, stderr


def _changed_paths(
    before: Mapping[str, Union[str, bytes]],
    after: Mapping[str, Union[str, bytes]],
) -> Tuple[str, ...]:
    """Return paths added or modified between two tree snapshots, sorted.

    Removals are intentionally not reported: the caller uses this to know which
    files the agent authored, and the tool-only ``.builder/`` metadata subtree is
    excluded so bookkeeping writes are not mistaken for plugin work.
    """
    changed = []
    for path, content in after.items():
        if path.startswith(".builder/"):
            continue
        if path not in before or before[path] != content:
            changed.append(path)
    return tuple(sorted(changed))


async def _terminate(process: "asyncio.subprocess.Process") -> None:
    """Kill ``process`` and reap it after a timeout abort."""
    try:
        process.kill()
    except ProcessLookupError:  # pragma: no cover - already exited
        return
    try:
        await process.wait()
    except Exception:  # pragma: no cover - best-effort reap
        pass
