"""Property test for export recording semantics (task 16.5).

Property 19 states that recording the outcome of a finished tenant-export
attempt is exact and symmetric (design "Property 19"; Req 10.2, 10.3):

* a **successful** upload adds *exactly one* export record to the
  ``Plugin_Registry`` carrying the target region base URL and the upload
  timestamp (UTC), and appends a success entry to the ``Audit_Log`` (Req 10.2,
  18.6); while
* a **failed or timed-out** upload leaves the ``Plugin_Registry`` unchanged (no
  export row is written) and appends a failed-attempt entry -- one carrying a
  failure reason -- to the ``Audit_Log`` (Req 10.3).

The function under test is
:func:`icplugin_builder.integrations.export_outcome.record_export_outcome`. This
test drives it across the outcome input space by drawing the classification
(success, HTTP failure, or timeout) together with a varied region base URL,
version, and UTC upload timestamp, then builds a real
:class:`~icplugin_builder.integrations.export_manager.ExportResult`. A real
in-memory :class:`PluginRegistry` (seeded with the plugin's creation record) and
a file-backed :class:`AuditLog` under a temp directory stand in for the durable
stores; no network is contacted.

**Validates: Requirements 10.2, 10.3**
"""

import tempfile
from datetime import datetime, timezone
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.integrations.export_manager import ExportResult
from icplugin_builder.persistence.audit_log import AuditEvent, AuditLog
from icplugin_builder.persistence.registry import PluginRegistry

PLUGIN = "okta"

# A varied but well-formed set of tenant region base URLs.
_REGIONS = [
    "https://us.api.insight.rapid7.com",
    "https://eu.api.insight.rapid7.com",
    "https://ca.api.insight.rapid7.com",
    "https://au.api.insight.rapid7.com",
    "https://ap.api.insight.rapid7.com",
]


def _version() -> st.SearchStrategy[str]:
    """Draw a MAJOR.MINOR.PATCH version string."""
    return st.builds(
        lambda a, b, c: f"{a}.{b}.{c}",
        st.integers(min_value=0, max_value=99),
        st.integers(min_value=0, max_value=99),
        st.integers(min_value=0, max_value=99),
    )


def _utc_timestamp() -> st.SearchStrategy[str]:
    """Draw an ISO-8601 UTC timestamp string with second precision."""
    return st.datetimes(
        min_value=datetime(2020, 1, 1),
        max_value=datetime(2035, 12, 31),
    ).map(lambda dt: dt.replace(microsecond=0, tzinfo=timezone.utc).isoformat())


@st.composite
def export_results(draw: st.DrawFn):
    """Draw an ``(ExportResult, is_success)`` pair spanning the outcome space.

    Covers a successful upload, an HTTP failure, and a timeout, each with a
    varied region base URL, version, and UTC upload timestamp.
    """
    region = draw(st.sampled_from(_REGIONS))
    version = draw(_version())
    uploaded_utc = draw(_utc_timestamp())
    kind = draw(st.sampled_from(("success", "http_failure", "timeout")))
    # A stable path is enough; the failure path retains an artifact only when an
    # artifact store is supplied, which this property deliberately omits.
    artifact_path = Path(f"/tmp/{PLUGIN}-{version}.plg")

    if kind == "success":
        result = ExportResult(
            success=True,
            region_base_url=region,
            artifact_path=artifact_path,
            uploaded_utc=uploaded_utc,
            status_code=201,
        )
        return result, True, version

    if kind == "http_failure":
        result = ExportResult(
            success=False,
            region_base_url=region,
            artifact_path=artifact_path,
            uploaded_utc=uploaded_utc,
            status_code=500,
            error="tenant upload failed with status 500",
        )
        return result, False, version

    result = ExportResult(
        success=False,
        region_base_url=region,
        artifact_path=artifact_path,
        uploaded_utc=uploaded_utc,
        status_code=None,
        timed_out=True,
        error="tenant upload timed out after 60s",
    )
    return result, False, version


# Feature: insightconnect-plugin-builder, Property 19: Successful upload records export; failure leaves registry unchanged  # noqa: E501
@settings(max_examples=200)
@given(case=export_results())
def test_export_recording_semantics(case):
    """Property 19: success records exactly one export; failure leaves registry unchanged.

    For any export outcome: a success adds exactly one registry export record
    carrying the region base URL and the UTC upload timestamp and appends a
    success audit record; a failure or timeout writes no registry export row and
    appends a failed-attempt (reason-carrying) audit record.

    **Validates: Requirements 10.2, 10.3**
    """
    # Import here so the module-level tag remains the single point of reference.
    from icplugin_builder.integrations.export_outcome import record_export_outcome

    result, is_success, version = case

    registry = PluginRegistry(":memory:")
    registry.record_creation(PLUGIN, "rapid7_custom", version)
    with tempfile.TemporaryDirectory() as tmp:
        audit_log = AuditLog(Path(tmp) / "audit.jsonl")
        try:
            outcome = record_export_outcome(
                result,
                plugin_name=PLUGIN,
                version=version,
                registry=registry,
                audit_log=audit_log,
            )

            exports = registry.exports(PLUGIN)
            records = audit_log.records()

            # Every attempt appends exactly one audit record for an EXPORT event.
            assert len(records) == 1
            assert records[0].event == AuditEvent.EXPORT
            assert records[0].plugin_name == PLUGIN
            assert records[0].version == version
            assert records[0].target == result.region_base_url

            if is_success:
                # Exactly one export record carrying region + UTC (Req 10.2).
                assert outcome.success is True
                assert outcome.registry_updated is True
                assert len(exports) == 1
                assert exports[0].target == result.region_base_url
                assert exports[0].export_utc == result.uploaded_utc
                assert exports[0].version == version
                assert exports[0].result == "success"
                # A successful export carries no failure reason.
                assert records[0].reason is None
            else:
                # Registry unchanged; failed-attempt audit record (Req 10.3).
                assert outcome.success is False
                assert outcome.registry_updated is False
                assert outcome.export_record is None
                assert exports == []
                # A failed attempt is distinguished by carrying a failure reason.
                assert records[0].reason is not None
                assert records[0].reason != ""
        finally:
            registry.close()
