"""Tests for the delegated plugin-implementation seam.

The Kiro CLI is mocked at the process boundary (``asyncio.create_subprocess_exec``)
so no real subprocess or network is involved. What is asserted here is the set of
guarantees the seam is responsible for, each of which was a real defect in the
single-shot path it replaces:

* the child does not inherit this process's environment (it holds decrypted
  tenant credentials);
* the task travels on stdin, not argv;
* the run happens in the plugin's own directory;
* failures are surfaced with stderr rather than swallowed;
* what changed on disk is *observed*, not taken from the agent's narration.
"""

import asyncio

import pytest

from icplugin_builder.core.cost_controller import CostController
from icplugin_builder.integrations.llm_generator import CostLimitError
from icplugin_builder.integrations.plugin_agent import (
    AgentRunResult,
    PluginAgent,
    PluginAgentError,
    parse_credits,
    strip_transcript_noise,
)

_TRANSCRIPT = (
    "All tools are now trusted (!). Kiro will execute tools without asking.\n"
    "I'll create the following file: icon_acme/util/api.py (using tool: write)\n"
    "> Implemented the client and two actions. validate passes.\n"
    " \u25b8 Credits: 0.31 \u2022 Time: 44s\n"
)


class _FakeProcess:
    def __init__(self, returncode=0, stdout=b"", stderr=b"", *, writes=None, root=None):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.stdin_received = None
        self._writes = writes or {}
        self._root = root

    async def communicate(self, stdin=None):
        self.stdin_received = stdin
        # Simulate the agent editing files in its working directory.
        for relative, content in self._writes.items():
            target = self._root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return self._stdout, self._stderr

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


class _Spy:
    """Captures how the subprocess was launched."""

    def __init__(self, process_factory):
        self.factory = process_factory
        self.argv = None
        self.cwd = None
        self.env = None
        self.process = None

    def install(self, monkeypatch, root):
        async def fake_exec(*command, stdin=None, stdout=None, stderr=None, cwd=None, env=None, **kwargs):
            self.argv = [str(part) for part in command]
            self.cwd = cwd
            self.env = env
            self.process = self.factory(root)
            return self.process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        return self


def _ok(root):
    return _FakeProcess(returncode=0, stdout=_TRANSCRIPT.encode("utf-8"), root=root)


def _run(agent, root, instruction="Implement the plugin.", session="s1", user="u1"):
    return asyncio.run(agent.implement(root, instruction, session_id=session, user_id=user))


#: The usage footer exactly as the CLI writes it -- on **stderr**, wrapped in
#: colour and cursor-control escapes. Captured from a real invocation.
_REAL_STDERR_FOOTER = (
    "\x1b[38;5;252m\x1b[0m\x1b[?25l\x1b[0m\x1b[0m\n"
    "\x1b[38;5;8m\n \u25b8 Credits: 0.05 \u2022 Time: 2s\n"
    "\x1b[0m\x1b[1G\x1b[0m\x1b[0m\x1b[?25h"
)


class TestTranscriptHelpers:
    def test_strips_ansi_and_banner_chrome(self):
        cleaned = strip_transcript_noise("\x1b[31mred\x1b[0m\nAll tools are now trusted (!).\nkept\n")
        assert "\x1b[" not in cleaned
        assert "All tools are now trusted" not in cleaned
        assert "kept" in cleaned

    def test_strips_cursor_control_not_only_colour_sequences(self):
        # The footer arrives wrapped in \x1b[?25l / \x1b[1G, which a colour-only
        # pattern would leave in place.
        cleaned = strip_transcript_noise("\x1b[?25lhidden\x1b[1G\x1b[?25hshown")
        assert "\x1b[" not in cleaned

    def test_parses_the_credits_footer(self):
        assert parse_credits(_TRANSCRIPT) == 0.31

    def test_finds_the_footer_on_stderr_where_the_cli_actually_writes_it(self):
        # The regression this guards: reading only stdout finds the footer when
        # the streams are merged (an interactive terminal, or 2>&1) and silently
        # finds nothing when they are captured separately -- which is how this
        # module runs the CLI. That is why real runs reported no credits.
        assert parse_credits("> ok\n", _REAL_STDERR_FOOTER) == 0.05

    def test_finds_the_footer_regardless_of_stream_order(self):
        assert parse_credits(_REAL_STDERR_FOOTER, "> ok\n") == 0.05

    def test_missing_footer_is_none_rather_than_zero(self):
        # None means "not reported"; 0.0 would wrongly claim a free run.
        assert parse_credits("no footer here") is None
        assert parse_credits("", "") is None


class TestCommand:
    def test_trusts_named_tools_rather_than_everything(self):
        agent = PluginAgent(CostController(), executable="kiro-cli", tools=("read", "write"))
        command = agent.build_command()
        assert "--trust-tools=read,write" in command
        assert "--trust-all-tools" not in command
        assert "-a" not in command

    def test_selects_the_generated_agent_and_runs_non_interactively(self):
        command = PluginAgent(CostController(), executable="kiro-cli", agent_name="my-agent").build_command()
        assert "--no-interactive" in command
        assert command[command.index("--agent") + 1] == "my-agent"

    def test_model_is_only_passed_when_explicitly_set(self):
        assert "--model" not in PluginAgent(CostController(), executable="kiro-cli").build_command()
        pinned = PluginAgent(CostController(), executable="kiro-cli", model="some-model").build_command()
        assert pinned[pinned.index("--model") + 1] == "some-model"


class TestInvocation:
    def test_task_goes_on_stdin_not_argv(self, tmp_path, monkeypatch):
        spy = _Spy(_ok).install(monkeypatch, tmp_path)
        agent = PluginAgent(CostController(), executable="kiro-cli")
        _run(agent, tmp_path, instruction="Implement list_widgets.")
        assert spy.process.stdin_received == b"Implement list_widgets."
        assert not any("list_widgets" in part for part in spy.argv)

    def test_runs_in_the_plugin_directory(self, tmp_path, monkeypatch):
        spy = _Spy(_ok).install(monkeypatch, tmp_path)
        _run(PluginAgent(CostController(), executable="kiro-cli"), tmp_path)
        assert str(spy.cwd) == str(tmp_path)

    def test_child_environment_is_default_deny(self, tmp_path, monkeypatch):
        # The regression this guards: create_subprocess_exec without env= hands
        # the LLM CLI every secret in the operator's environment, and this
        # process decrypts tenant API keys and git credentials.
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
        monkeypatch.setenv("ICPLUGIN_TENANT_KEY", "tenant_secret")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        spy = _Spy(_ok).install(monkeypatch, tmp_path)
        _run(PluginAgent(CostController(), executable="kiro-cli"), tmp_path)

        assert spy.env is not None, "no env passed: the child would inherit the parent environment"
        assert "GITHUB_TOKEN" not in spy.env
        assert "ICPLUGIN_TENANT_KEY" not in spy.env
        assert "ghp_secret" not in spy.env.values()
        assert "tenant_secret" not in spy.env.values()
        # Kiro's own auth still reaches it, or the run could not authenticate.
        assert spy.env.get("AWS_REGION") == "us-east-1"

    def test_rejects_a_missing_project_directory(self, tmp_path):
        agent = PluginAgent(CostController(), executable="kiro-cli")
        with pytest.raises(ValueError):
            _run(agent, tmp_path / "nope")


class TestObservedChanges:
    def test_changed_files_come_from_the_filesystem_not_the_narration(self, tmp_path, monkeypatch):
        # The transcript claims util/api.py; the process actually writes a
        # different file. The result must reflect what really happened.
        def factory(root):
            return _FakeProcess(
                returncode=0,
                stdout=_TRANSCRIPT.encode("utf-8"),
                root=root,
                writes={"icon_acme/actions/list/action.py": "real content\n"},
            )

        _Spy(factory).install(monkeypatch, tmp_path)
        result = _run(PluginAgent(CostController(), executable="kiro-cli"), tmp_path)
        assert result.changed_files == ("icon_acme/actions/list/action.py",)

    def test_builder_metadata_writes_are_not_reported_as_plugin_work(self, tmp_path, monkeypatch):
        def factory(root):
            return _FakeProcess(
                returncode=0,
                stdout=_TRANSCRIPT.encode("utf-8"),
                root=root,
                writes={".builder/project.json": "{}\n", "help.md": "# help\n"},
            )

        _Spy(factory).install(monkeypatch, tmp_path)
        result = _run(PluginAgent(CostController(), executable="kiro-cli"), tmp_path)
        assert result.changed_files == ("help.md",)

    def test_unchanged_files_are_not_reported(self, tmp_path, monkeypatch):
        (tmp_path / "untouched.txt").write_text("same\n", encoding="utf-8")
        _Spy(_ok).install(monkeypatch, tmp_path)
        result = _run(PluginAgent(CostController(), executable="kiro-cli"), tmp_path)
        assert result.changed_files == ()

    def test_summary_is_the_agents_closing_report(self, tmp_path, monkeypatch):
        _Spy(_ok).install(monkeypatch, tmp_path)
        result = _run(PluginAgent(CostController(), executable="kiro-cli"), tmp_path)
        assert isinstance(result, AgentRunResult)
        assert result.summary == "Implemented the client and two actions. validate passes."
        assert result.credits == 0.31

    def test_credits_are_captured_from_a_realistic_stream_split(self, tmp_path, monkeypatch):
        # stdout carries only the answer; the usage footer is on stderr.
        def factory(root):
            return _FakeProcess(
                returncode=0,
                stdout=b"> Implemented it.\n",
                stderr=_REAL_STDERR_FOOTER.encode("utf-8"),
                root=root,
            )

        _Spy(factory).install(monkeypatch, tmp_path)
        result = _run(PluginAgent(CostController(), executable="kiro-cli"), tmp_path)
        assert result.credits == 0.05

    def test_a_run_reporting_no_credits_leaves_them_unknown(self, tmp_path, monkeypatch):
        def factory(root):
            return _FakeProcess(returncode=0, stdout=b"> done\n", stderr=b"", root=root)

        _Spy(factory).install(monkeypatch, tmp_path)
        result = _run(PluginAgent(CostController(), executable="kiro-cli"), tmp_path)
        assert result.credits is None


class TestFailureHandling:
    def test_non_zero_exit_raises_with_stderr_attached(self, tmp_path, monkeypatch):
        def factory(root):
            return _FakeProcess(returncode=3, stdout=b"partial", stderr=b"boom: model unavailable", root=root)

        _Spy(factory).install(monkeypatch, tmp_path)
        agent = PluginAgent(CostController(), executable="kiro-cli")
        with pytest.raises(PluginAgentError) as caught:
            _run(agent, tmp_path)
        assert caught.value.returncode == 3
        assert "boom: model unavailable" in caught.value.stderr

    def test_missing_executable_names_the_remedy(self, tmp_path, monkeypatch):
        async def missing(*command, **kwargs):
            raise FileNotFoundError("no kiro-cli")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", missing)
        with pytest.raises(PluginAgentError) as caught:
            _run(PluginAgent(CostController(), executable="kiro-cli"), tmp_path)
        assert "not found" in str(caught.value)
        assert "PATH" in str(caught.value)

    def test_timeout_is_reported_as_a_timeout(self, tmp_path, monkeypatch):
        class _Hang(_FakeProcess):
            async def communicate(self, stdin=None):
                await asyncio.sleep(10)

        async def fake_exec(*command, **kwargs):
            return _Hang(root=tmp_path)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        agent = PluginAgent(CostController(), executable="kiro-cli", timeout_seconds=0.05)
        with pytest.raises(PluginAgentError) as caught:
            _run(agent, tmp_path)
        assert "exceeded" in str(caught.value)

    def test_a_failed_run_is_excluded_from_the_session_total(self, tmp_path, monkeypatch):
        # Req 3.7: a failed invocation must not consume the session budget.
        def factory(root):
            return _FakeProcess(returncode=1, stderr=b"nope", root=root)

        _Spy(factory).install(monkeypatch, tmp_path)
        cost = CostController()
        agent = PluginAgent(cost, executable="kiro-cli")
        with pytest.raises(PluginAgentError):
            _run(agent, tmp_path)
        assert cost.session_total("s1") == 0


class TestCostGating:
    def test_a_blocked_run_is_never_dispatched(self, tmp_path, monkeypatch):
        # Req 4.2: once the budget is reached nothing reaches the CLI.
        spy = _Spy(_ok).install(monkeypatch, tmp_path)
        cost = CostController(token_budget=1)
        cost.record_usage("s1", 5, True)
        agent = PluginAgent(cost, executable="kiro-cli")
        with pytest.raises(CostLimitError):
            _run(agent, tmp_path)
        assert spy.argv is None

    def test_a_successful_run_is_recorded_on_the_session_total(self, tmp_path, monkeypatch):
        _Spy(_ok).install(monkeypatch, tmp_path)
        cost = CostController()
        result = _run(PluginAgent(cost, executable="kiro-cli"), tmp_path)
        assert result.tokens > 0
        assert cost.session_total("s1") == result.tokens
        assert result.session_total == result.tokens
