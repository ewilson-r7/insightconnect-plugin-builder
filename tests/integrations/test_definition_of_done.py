"""Unit tests for the structural definition-of-done conditions.

The fixtures here are deliberately copies of shapes taken from plugins this tool
actually produced, not invented examples. Two in particular:

* ``connection.test()`` written as a ``# TODO`` comment above a bare ``pass`` --
  the scaffolder's own stub, shipped unchanged in every early plugin;
* an action that calls ``requests.get(url)`` after assembling an f-string URL,
  *without importing* ``requests`` at all. That last detail is why these checks
  read usage rather than imports: an import-based check passes this file, and the
  plugin then dies with a ``NameError`` on first run.

Each condition is checked for all three outcomes it can produce -- met, unmet,
and unverified -- because the third is the one that keeps a missing tool from
looking like a clean result (Req 27.5).
"""

from pathlib import Path

import json

from icplugin_builder.integrations.definition_of_done import (
    CONDITION_ACTIONS_USE_CLIENT,
    CONDITION_API_CLIENT,
    CONDITION_CONNECTION,
    CONDITION_DEPENDENCY_MANIFEST,
    CONDITION_ORDER,
    CONDITION_REFERENCE_MATERIAL,
    ConditionStatus,
    evaluate_done,
)

PACKAGE = "icon_example"

#: The scaffolder's stub, verbatim in shape: a marker and a bare ``pass``.
STUB_CONNECTION = """import insightconnect_plugin_runtime
from .schema import ConnectionSchema, Input


class Connection(insightconnect_plugin_runtime.Connection):
    def connect(self, params):
        self.api_key = params.get(Input.API_KEY)

    def test(self):
        # TODO: Implement connection test
        pass
"""

REAL_CONNECTION = """import insightconnect_plugin_runtime
from .schema import ConnectionSchema, Input
from ..util.api import ExampleApi


class Connection(insightconnect_plugin_runtime.Connection):
    def connect(self, params):
        self.api_key = params.get(Input.API_KEY)
        self.client = ExampleApi(self.api_key, self.logger)

    def test(self):
        self.client.get_status()
        return {"success": True}
"""

#: An action doing its own HTTP. Note the absence of ``import requests``.
SELF_SERVICE_ACTION = """import insightconnect_plugin_runtime
from .schema import CheckIpInput, CheckIpOutput, Input, Output


class CheckIp(insightconnect_plugin_runtime.Action):
    def run(self, params={}):
        url = f"https://api.example.com/v1/check/{params.get(Input.IP)}"
        response = requests.get(url, headers={"X-Api-Key": self.connection.api_key})
        return {Output.RESULT: response.json()}
"""

DELEGATING_ACTION = """import insightconnect_plugin_runtime
from .schema import CheckIpInput, CheckIpOutput, Input, Output


class CheckIp(insightconnect_plugin_runtime.Action):
    def run(self, params={}):
        return {Output.RESULT: self.connection.client.check_ip(params.get(Input.IP))}
"""

REAL_API_CLIENT = '''import requests
from insightconnect_plugin_runtime.exceptions import PluginException

HTTP_ERROR_MAP = {
    401: "The API key is invalid.",
    404: "The requested resource was not found.",
}


class ExampleApi:
    def __init__(self, api_key, logger):
        self.api_key = api_key
        self.logger = logger

    def check_ip(self, ip_address):
        """One domain method per action."""
        return self._make_request("GET", "check", params={"ipAddress": ip_address})

    def get_status(self):
        return self._make_request("GET", "status")

    def _make_request(self, method, path, **kwargs):
        response = requests.request(method, f"https://api.example.com/v1/{path}", **kwargs)
        if response.status_code in HTTP_ERROR_MAP:
            raise PluginException(cause=HTTP_ERROR_MAP[response.status_code])
        return response.json()
'''

PINNED_MANIFEST = """# List third-party dependencies here, separated by newlines.
requests==2.31.0
"""


def _plugin(
    root: Path,
    *,
    connection: str = REAL_CONNECTION,
    action: str = DELEGATING_ACTION,
    api_client: str = REAL_API_CLIENT,
    manifest: str = PINNED_MANIFEST,
) -> Path:
    """Build a plugin tree with the standard layout, defaulting to a sound one."""
    package = root / PACKAGE
    (package / "connection").mkdir(parents=True)
    (package / "actions" / "check_ip").mkdir(parents=True)
    (package / "util").mkdir(parents=True)

    (package / "connection" / "connection.py").write_text(connection, encoding="utf-8")
    (package / "actions" / "check_ip" / "action.py").write_text(action, encoding="utf-8")
    if api_client is not None:
        (package / "util" / "api.py").write_text(api_client, encoding="utf-8")
    if manifest is not None:
        (root / "requirements.txt").write_text(manifest, encoding="utf-8")
    return root


def _connection_with_test(*body_lines: str) -> str:
    """Build a connection whose ``connect()`` is real and whose ``test()`` is given."""
    body = "".join(f"        {line}\n" for line in body_lines)
    return (
        "class Connection:\n"
        "    def connect(self, params):\n"
        "        self.key = 1\n\n"
        "    def test(self):\n" + body
    )


def _status(root: Path, name: str) -> ConditionStatus:
    """Evaluate ``root`` and return the status of the ``name`` condition."""
    condition = evaluate_done(root).condition(name)
    assert condition is not None, f"{name} was not evaluated"
    return condition.status


def _detail(root: Path, name: str) -> str:
    """Evaluate ``root`` and return the detail of the ``name`` condition."""
    condition = evaluate_done(root).condition(name)
    assert condition is not None
    return condition.detail


class TestEveryConditionIsAlwaysReported:
    def test_a_report_names_every_condition_even_for_an_empty_tree(self, tmp_path):
        # The gate is meant to be the whole answer. A tree with nothing in it must
        # still produce a verdict on each condition rather than a short report
        # that happens to contain no failures.
        report = evaluate_done(tmp_path)
        assert report.missing_conditions == ()
        assert len(report.conditions) == len(CONDITION_ORDER)
        assert not report.complete

    def test_a_sound_plugin_meets_every_structural_condition(self, tmp_path):
        root = _plugin(tmp_path)
        report = evaluate_done(root)
        structural = (
            CONDITION_API_CLIENT,
            CONDITION_ACTIONS_USE_CLIENT,
            CONDITION_CONNECTION,
            CONDITION_DEPENDENCY_MANIFEST,
        )
        for name in structural:
            condition = report.condition(name)
            assert condition is not None
            assert condition.status is ConditionStatus.MET, f"{name}: {condition.detail}"
        # The tool-run conditions are unverified here -- no reports were supplied --
        # so the plugin is still not done.
        assert not report.complete


class TestApiClient:
    def test_a_missing_api_module_is_unmet(self, tmp_path):
        root = _plugin(tmp_path, api_client=None)
        assert _status(root, CONDITION_API_CLIENT) is ConditionStatus.UNMET
        assert "util/api.py" in _detail(root, CONDITION_API_CLIENT)

    def test_a_client_without_a_central_request_helper_is_unmet(self, tmp_path):
        client = "HTTP_ERROR_MAP = {}\n\n\nclass Api:\n    def check_ip(self, ip):\n        return ip\n"
        root = _plugin(tmp_path, api_client=client)
        assert _status(root, CONDITION_API_CLIENT) is ConditionStatus.UNMET
        assert "_make_request" in _detail(root, CONDITION_API_CLIENT)

    def test_a_client_without_an_error_map_is_unmet(self, tmp_path):
        client = (
            "class Api:\n"
            "    def check_ip(self, ip):\n"
            "        return self._make_request(ip)\n\n"
            "    def _make_request(self, ip):\n"
            "        return ip\n"
        )
        root = _plugin(tmp_path, api_client=client)
        assert "HTTP_ERROR_MAP" in _detail(root, CONDITION_API_CLIENT)

    def test_a_client_with_no_domain_method_is_unmet(self, tmp_path):
        # A client that can only make a generic request gives the actions nothing
        # to call, so they end up building their own requests again.
        client = (
            "HTTP_ERROR_MAP = {}\n\n\nclass Api:\n    def _make_request(self, method, path):\n        return path\n"
        )
        root = _plugin(tmp_path, api_client=client)
        assert _status(root, CONDITION_API_CLIENT) is ConditionStatus.UNMET
        assert "domain method" in _detail(root, CONDITION_API_CLIENT)

    def test_an_unparseable_client_is_unverified_not_unmet(self, tmp_path):
        # The parse failure is its own condition. Reporting it here as well would
        # count one defect twice and point the fixer at the wrong thing.
        root = _plugin(tmp_path, api_client="def broken(\n")
        assert _status(root, CONDITION_API_CLIENT) is ConditionStatus.UNVERIFIED


class TestComponentsUseTheClient:
    def test_an_action_making_its_own_request_is_unmet(self, tmp_path):
        root = _plugin(tmp_path, action=SELF_SERVICE_ACTION)
        detail = _detail(root, CONDITION_ACTIONS_USE_CLIENT)
        assert _status(root, CONDITION_ACTIONS_USE_CLIENT) is ConditionStatus.UNMET
        assert "makes its own HTTP request" in detail
        assert "builds a URL" in detail

    def test_usage_is_caught_even_with_no_import(self, tmp_path):
        # The shape that shipped: requests.get(...) with requests never imported.
        action = "class A:\n    def run(self, params={}):\n        return requests.get('/x')\n"
        root = _plugin(tmp_path, action=action)
        assert _status(root, CONDITION_ACTIONS_USE_CLIENT) is ConditionStatus.UNMET

    def test_an_import_of_an_http_library_is_caught_too(self, tmp_path):
        action = (
            "import httpx\n\n\n"
            "class A:\n"
            "    def run(self, params={}):\n"
            "        return self.connection.client.check_ip(1)\n"
        )
        root = _plugin(tmp_path, action=action)
        assert _status(root, CONDITION_ACTIONS_USE_CLIENT) is ConditionStatus.UNMET

    def test_a_url_in_a_docstring_is_not_a_violation(self, tmp_path):
        # Documentation may cite the vendor's API; only assembled URLs count.
        action = (
            "class A:\n"
            '    """Checks an IP against https://api.example.com/v1/check."""\n\n'
            "    def run(self, params={}):\n"
            "        return self.connection.client.check_ip(1)\n"
        )
        root = _plugin(tmp_path, action=action)
        assert _status(root, CONDITION_ACTIONS_USE_CLIENT) is ConditionStatus.MET

    def test_an_unparseable_component_is_unverified(self, tmp_path):
        root = _plugin(tmp_path, action="def run(\n")
        assert _status(root, CONDITION_ACTIONS_USE_CLIENT) is ConditionStatus.UNVERIFIED

    def test_generated_component_files_are_not_checked(self, tmp_path):
        # schema.py is generated from the spec and must not be hand-edited, so a
        # URL or import in it is not something a fixer may act on.
        root = _plugin(tmp_path)
        schema = root / PACKAGE / "actions" / "check_ip" / "schema.py"
        schema.write_text("import requests\n\nURL = 'https://api.example.com'\n", encoding="utf-8")
        assert _status(root, CONDITION_ACTIONS_USE_CLIENT) is ConditionStatus.MET


class TestConnection:
    def test_the_scaffolders_todo_stub_is_unmet(self, tmp_path):
        root = _plugin(tmp_path, connection=STUB_CONNECTION)
        assert _status(root, CONDITION_CONNECTION) is ConditionStatus.UNMET
        assert "test() is a stub" in _detail(root, CONDITION_CONNECTION)

    def test_a_bare_pass_is_a_stub(self, tmp_path):
        root = _plugin(tmp_path, connection=_connection_with_test("pass"))
        assert _status(root, CONDITION_CONNECTION) is ConditionStatus.UNMET

    def test_a_docstring_only_body_is_a_stub(self, tmp_path):
        root = _plugin(tmp_path, connection=_connection_with_test('"""Tests the connection."""'))
        assert _status(root, CONDITION_CONNECTION) is ConditionStatus.UNMET

    def test_not_implemented_is_a_stub(self, tmp_path):
        root = _plugin(tmp_path, connection=_connection_with_test("raise NotImplementedError"))
        assert _status(root, CONDITION_CONNECTION) is ConditionStatus.UNMET

    def test_an_implemented_body_carrying_a_marker_is_still_unmet(self, tmp_path):
        connection = _connection_with_test(
            "# TODO: check the response shape",
            "return self.client.get_status()",
        )
        root = _plugin(tmp_path, connection=connection)
        assert _status(root, CONDITION_CONNECTION) is ConditionStatus.UNMET
        assert "unfinished marker" in _detail(root, CONDITION_CONNECTION)

    def test_a_missing_method_is_named(self, tmp_path):
        connection = "class Connection:\n    def connect(self, params):\n        self.key = 1\n"
        root = _plugin(tmp_path, connection=connection)
        assert "test() is not defined" in _detail(root, CONDITION_CONNECTION)

    def test_a_real_connection_is_met(self, tmp_path):
        root = _plugin(tmp_path)
        assert _status(root, CONDITION_CONNECTION) is ConditionStatus.MET

    def test_an_unparseable_connection_is_unverified(self, tmp_path):
        root = _plugin(tmp_path, connection="def connect(\n")
        assert _status(root, CONDITION_CONNECTION) is ConditionStatus.UNVERIFIED


class TestDependencyManifest:
    def test_an_absent_manifest_is_unmet(self, tmp_path):
        root = _plugin(tmp_path, manifest=None)
        assert _status(root, CONDITION_DEPENDENCY_MANIFEST) is ConditionStatus.UNMET

    def test_a_comment_only_manifest_is_met(self, tmp_path):
        # This is the scaffolded default. A plugin with no third-party
        # dependencies has nothing to pin, so requiring a pin here would report a
        # defect that does not exist.
        manifest = (
            "# List third-party dependencies here, separated by newlines.\n"
            "# All dependencies must be version-pinned, eg. requests==1.2.0\n"
        )
        root = _plugin(tmp_path, manifest=manifest)
        assert _status(root, CONDITION_DEPENDENCY_MANIFEST) is ConditionStatus.MET

    def test_a_floating_version_is_unmet(self, tmp_path):
        root = _plugin(tmp_path, manifest="requests>=2.0.0\n")
        assert _status(root, CONDITION_DEPENDENCY_MANIFEST) is ConditionStatus.UNMET
        assert "requests>=2.0.0" in _detail(root, CONDITION_DEPENDENCY_MANIFEST)

    def test_an_unpinned_name_is_unmet(self, tmp_path):
        root = _plugin(tmp_path, manifest="requests\n")
        assert _status(root, CONDITION_DEPENDENCY_MANIFEST) is ConditionStatus.UNMET

    def test_pip_options_are_not_dependencies(self, tmp_path):
        root = _plugin(tmp_path, manifest="-r base.txt\nrequests==2.31.0\n")
        assert _status(root, CONDITION_DEPENDENCY_MANIFEST) is ConditionStatus.MET


class TestUnscaffoldedTree:
    def test_a_tree_with_no_package_leaves_the_code_conditions_unverified(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("requests==2.31.0\n", encoding="utf-8")
        report = evaluate_done(tmp_path)
        for name in (CONDITION_API_CLIENT, CONDITION_ACTIONS_USE_CLIENT, CONDITION_CONNECTION):
            condition = report.condition(name)
            assert condition is not None
            assert condition.status is ConditionStatus.UNVERIFIED
            assert "no plugin package directory" in condition.detail
        # The manifest is checked at the tree root, so it is knowable either way.
        assert _status(tmp_path, CONDITION_DEPENDENCY_MANIFEST) is ConditionStatus.MET

    def test_a_legacy_komand_package_is_recognised(self, tmp_path):
        package = tmp_path / "komand_legacy"
        (package / "connection").mkdir(parents=True)
        (package / "connection" / "connection.py").write_text(REAL_CONNECTION, encoding="utf-8")
        report = evaluate_done(tmp_path)
        condition = report.condition(CONDITION_CONNECTION)
        assert condition is not None
        assert condition.status is ConditionStatus.MET


class TestReferenceMaterialCondition:
    """Three outcomes, because absent documentation is not always a defect.

    A plugin that calls somebody's API and has no documentation was built on
    guesses. A plugin that encodes base64 needs no documentation at all, and
    reporting it as unmet would be a false alarm -- the kind that teaches an
    operator to stop reading this condition.
    """

    def _record(self, root: Path, payload: dict) -> None:
        directory = root / ".builder" / "reference"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "provenance.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_stored_documentation_meets_the_condition(self, tmp_path):
        root = _plugin(tmp_path)
        self._record(root, {"documents": [{"name": "api.md", "origin": "url"}], "failures": []})
        assert _status(root, CONDITION_REFERENCE_MATERIAL) is ConditionStatus.MET

    def test_proceeding_without_documentation_is_unmet_and_says_why(self, tmp_path):
        root = _plugin(tmp_path)
        self._record(
            root,
            {
                "documents": [],
                "failures": [],
                "implemented_without_reference": True,
                "detail": "proceeded at the user's direction with no vendor documentation",
            },
        )
        assert _status(root, CONDITION_REFERENCE_MATERIAL) is ConditionStatus.UNMET
        assert "user's direction" in _detail(root, CONDITION_REFERENCE_MATERIAL)

    def test_no_record_at_all_is_unverified_not_unmet(self, tmp_path):
        # Nothing is known, and a plugin with no external API needs nothing. The
        # honest answer is unverified, which still keeps the plugin from reading as
        # done while never inventing a defect.
        root = _plugin(tmp_path)
        assert _status(root, CONDITION_REFERENCE_MATERIAL) is ConditionStatus.UNVERIFIED

    def test_an_unreadable_record_is_unverified(self, tmp_path):
        root = _plugin(tmp_path)
        directory = root / ".builder" / "reference"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "provenance.json").write_text("{not json", encoding="utf-8")
        assert _status(root, CONDITION_REFERENCE_MATERIAL) is ConditionStatus.UNVERIFIED
