"""Local and tenant export of a built plugin (the Export_Manager, task 16.1).

The ``Export_Manager`` takes a plugin that has already been packaged into a
``.plg`` by the :class:`~icplugin_builder.integrations.build_engine.BuildEngine`
and either makes it available locally or uploads it to an InsightConnect tenant
(design "Export_Manager"; Req 9.3, 10).

This slice (task 16.1) implements the two entry points and their pre-network
guards:

* :meth:`ExportManager.export_local` writes the ``.plg`` to a user-accessible
  output location and reports the resulting path (Req 9.3). It reuses the
  Build_Engine as the single packager so the local export is byte-for-byte the
  same artifact the tenant export would upload.
* :meth:`ExportManager.export_tenant` uploads a built artifact to a tenant, but
  only after two checks that happen **before any network call**:

  1. the tenant region base URL **and** API key must both be non-empty; if
     either is missing the export is rejected and the error names the missing
     credential (Req 10.4); and
  2. a built ``.plg`` artifact must actually exist; if none does the export is
     rejected with an error telling the user to build first (Req 10.5).

  Only once both guards pass is the artifact uploaded through an injected
  uploader with a 60-second overall timeout (Req 10.1, 10.3).

Everything costly or non-deterministic is injected: the ``build_engine`` that
packages the plugin and the ``uploader`` that performs the HTTP upload are both
collaborators, so the manager is fully deterministic under test -- no real
network is contacted (the tenant API is mocked). A minimal stdlib-based default
uploader (:class:`UrllibTenantUploader`) is provided for real use.

Recording a successful export in the ``Plugin_Registry`` and a failed attempt in
the ``Audit_Log`` (Req 10.2, 10.3) is deliberately **not** implemented here; that
outcome-recording step (task 16.4) lives alongside this module in
:mod:`icplugin_builder.integrations.export_outcome`. It builds on the
:class:`ExportResult` returned by :meth:`export_tenant`, which already carries
the region, timestamp, and success/failure classification those records need.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Protocol, Union, runtime_checkable

from .build_engine import BuildEngine, PlgArtifact

__all__ = [
    "ExportManagerError",
    "MissingCredentialError",
    "ArtifactNotBuiltError",
    "TenantCredentials",
    "UploadResponse",
    "TenantUploader",
    "UrllibTenantUploader",
    "ExportResult",
    "ExportManager",
    "DEFAULT_UPLOAD_TIMEOUT_SECONDS",
    "DEFAULT_PLUGIN_UPLOAD_PATH",
    "NO_ARTIFACT_MESSAGE",
]

#: The overall upload timeout: an upload that does not complete within this many
#: seconds is treated as a failure (Req 10.3).
DEFAULT_UPLOAD_TIMEOUT_SECONDS = 60.0

#: The tenant plugin-management endpoint path appended to the region base URL by
#: :class:`UrllibTenantUploader`. Kept as a single named constant so the real
#: endpoint can be adjusted in one place without touching the guard logic; the
#: injected uploader used in tests never relies on it.
DEFAULT_PLUGIN_UPLOAD_PATH = "/api/v1/plugins"

#: Surfaced when export is requested but no built ``.plg`` artifact exists (Req 10.5).
NO_ARTIFACT_MESSAGE = "no built plugin artifact exists; the plugin must be built before export"

#: A path, a produced artifact, or nothing (= not built).
ArtifactInput = Union["PlgArtifact", str, Path, None]

#: A plugin working tree, or an object exposing a ``root`` attribute.
PathInput = Union[str, Path]


class ExportManagerError(Exception):
    """Base class for Export_Manager failures."""


class MissingCredentialError(ExportManagerError):
    """Raised when a required tenant credential is empty (Req 10.4).

    The message names exactly which credential (region base URL or API key) was
    missing so the user can supply it. Raised **before** any network call, so an
    uploader is never invoked when this is raised.
    """


class ArtifactNotBuiltError(ExportManagerError):
    """Raised when a tenant export is requested with no built artifact (Req 10.5).

    Raised **before** any network call, so an uploader is never invoked when this
    is raised.
    """


@dataclass(frozen=True)
class TenantCredentials:
    """Addressing + authentication for an InsightConnect tenant (Req 10.1, 10.4).

    Attributes:
        region_base_url: The tenant region base URL (e.g.
            ``https://us.api.insight.rapid7.com``). Must be non-empty.
        api_key: The user-supplied InsightConnect API key. Must be non-empty.
    """

    region_base_url: str
    api_key: str

    @property
    def region(self) -> str:
        """The region base URL with surrounding whitespace stripped."""
        return (self.region_base_url or "").strip()

    @property
    def key(self) -> str:
        """The API key with surrounding whitespace stripped."""
        return (self.api_key or "").strip()

    def validate(self) -> None:
        """Reject the export if either credential is empty (Req 10.4).

        Raises:
            MissingCredentialError: If the region base URL or API key is empty or
                whitespace-only. The message names the missing credential.
        """
        if not self.region:
            raise MissingCredentialError("tenant region base URL is required but was empty")
        if not self.key:
            raise MissingCredentialError("tenant API key is required but was empty")


@dataclass(frozen=True)
class UploadResponse:
    """The outcome of a single upload attempt reported by a :class:`TenantUploader`.

    Attributes:
        status_code: The HTTP status code returned by the tenant API.
        body: The response body text (may be empty).
    """

    status_code: int
    body: str = ""

    @property
    def success(self) -> bool:
        """Return ``True`` iff the tenant API reported a 2xx success response."""
        return 200 <= self.status_code < 300


@runtime_checkable
class TenantUploader(Protocol):
    """Minimal interface for uploading a built ``.plg`` to a tenant.

    Injecting the uploader keeps :meth:`ExportManager.export_tenant` deterministic
    under test: a fake uploader stands in for the real InsightConnect tenant API
    so no network is contacted.
    """

    def upload(
        self,
        *,
        region_base_url: str,
        api_key: str,
        artifact_path: Path,
        timeout: float,
    ) -> UploadResponse:  # pragma: no cover - protocol definition
        """Upload ``artifact_path`` to the tenant, honoring ``timeout`` seconds."""
        ...


@dataclass(frozen=True)
class ExportResult:
    """The outcome of an :meth:`ExportManager.export_tenant` call.

    A rejection that happens before any network call is signalled by an
    exception (:class:`MissingCredentialError`, :class:`ArtifactNotBuiltError`),
    not by this result; this result models an upload that was actually attempted
    and either succeeded or failed/timed out. It carries the region, timestamp,
    and failure classification that the outcome-recording step (task 16.4;
    Req 10.2, 10.3) needs to write registry and audit records.

    Attributes:
        success: ``True`` iff the tenant API returned a success response.
        region_base_url: The tenant region the artifact was uploaded to.
        artifact_path: The built ``.plg`` that was uploaded.
        uploaded_utc: ISO-8601 UTC instant the upload attempt completed.
        status_code: The HTTP status code, when a response was received.
        timed_out: ``True`` iff the attempt failed by exceeding the timeout.
        error: A human-readable failure description, empty on success.
    """

    success: bool
    region_base_url: str
    artifact_path: Path
    uploaded_utc: str
    status_code: Optional[int] = None
    timed_out: bool = False
    error: str = ""

    @property
    def failed(self) -> bool:
        """Return ``True`` iff the upload did not succeed (the inverse of :attr:`success`)."""
        return not self.success


class ExportManager:
    """Exports a built plugin locally or to an InsightConnect tenant (Req 9.3, 10).

    The manager never mutates the source tree: :meth:`export_local` delegates
    packaging to the read-only :class:`BuildEngine`, and :meth:`export_tenant`
    only reads the built artifact.
    """

    def __init__(
        self,
        *,
        build_engine: Optional[BuildEngine] = None,
        uploader: Optional[TenantUploader] = None,
    ) -> None:
        """Create an Export_Manager.

        Args:
            build_engine: The packager used by :meth:`export_local`; defaults to a
                fresh :class:`BuildEngine`.
            uploader: The tenant uploader used by :meth:`export_tenant`; defaults
                to :class:`UrllibTenantUploader`. Inject a fake in tests so no
                network is contacted.
        """
        self._build_engine = build_engine if build_engine is not None else BuildEngine()
        self._uploader = uploader if uploader is not None else UrllibTenantUploader()

    def export_local(
        self,
        project: PathInput,
        *,
        output_dir: Optional[PathInput] = None,
        validation_passed: bool = True,
        artifact_name: Optional[str] = None,
    ) -> Path:
        """Write the plugin's ``.plg`` to a user-accessible location; report it (Req 9.3).

        Packages ``project`` via the Build_Engine into ``output_dir`` and returns
        the absolute path of the produced ``.plg`` so the caller can display it to
        the user. Reusing the Build_Engine guarantees the local artifact is the
        same gzipped tarball that a tenant export would upload.

        Args:
            project: The plugin working tree -- a path, or an object with a
                ``root`` attribute.
            output_dir: The user-accessible directory to write the ``.plg`` into;
                defaults to the current working directory so the artifact is
                readily accessible rather than buried in tool-only metadata.
            validation_passed: Forwarded to the Build_Engine; packaging occurs
                only when validation has passed (Req 9.1, 9.4).
            artifact_name: Optional artifact file name; defaults to the plugin
                directory name with a ``.plg`` suffix.

        Returns:
            The absolute path of the written ``.plg``.

        Raises:
            ValidationNotPassedError: If ``validation_passed`` is ``False``.
            PackagingError: If packaging fails (no partial artifact; sources
                unchanged).
        """
        destination = Path(output_dir) if output_dir is not None else Path.cwd()
        artifact = self._build_engine.package(
            project,
            validation_passed=validation_passed,
            output_dir=destination,
            artifact_name=artifact_name,
        )
        return artifact.path

    def export_tenant(
        self,
        artifact: ArtifactInput,
        credentials: TenantCredentials,
        *,
        timeout: float = DEFAULT_UPLOAD_TIMEOUT_SECONDS,
    ) -> ExportResult:
        """Upload a built ``.plg`` to an InsightConnect tenant (Req 10.1, 10.3, 10.4, 10.5).

        Performs two guards **before any network call**: the credentials must be
        non-empty (Req 10.4) and a built artifact must exist (Req 10.5). Only then
        is the artifact uploaded through the injected uploader with an overall
        ``timeout``-second budget (Req 10.1, 10.3).

        Args:
            artifact: The built ``.plg`` to upload -- a
                :class:`~icplugin_builder.integrations.build_engine.PlgArtifact`,
                a path to a ``.plg``, or ``None``/a missing path when no artifact
                has been built.
            credentials: The tenant region base URL and API key.
            timeout: The overall upload timeout in seconds; defaults to 60
                (Req 10.3).

        Returns:
            An :class:`ExportResult` describing the upload attempt (success or
            failure/timeout). Recording the outcome in the registry/audit log is
            task 16.4.

        Raises:
            MissingCredentialError: If the region base URL or API key is empty,
                naming the missing credential (Req 10.4). Raised before any
                network call.
            ArtifactNotBuiltError: If no built artifact exists (Req 10.5). Raised
                before any network call.
        """
        # Guard 1: credentials must be present before contacting the API (Req 10.4).
        credentials.validate()

        # Guard 2: a built artifact must exist before contacting the API (Req 10.5).
        artifact_path = self._resolve_artifact_path(artifact)

        # Both guards passed: attempt the upload with the overall timeout (Req 10.1, 10.3).
        return self._upload(artifact_path, credentials, timeout)

    def _upload(
        self,
        artifact_path: Path,
        credentials: TenantCredentials,
        timeout: float,
    ) -> ExportResult:
        """Perform the upload and classify the outcome as success/failure/timeout."""
        region = credentials.region
        try:
            response = self._uploader.upload(
                region_base_url=region,
                api_key=credentials.key,
                artifact_path=artifact_path,
                timeout=timeout,
            )
        except TimeoutError as error:
            return self._result(
                success=False,
                region=region,
                artifact_path=artifact_path,
                timed_out=True,
                error=f"tenant upload timed out after {timeout:g}s: {error}",
            )
        except Exception as error:  # noqa: BLE001 -- any upload failure is reported, not raised.
            return self._result(
                success=False,
                region=region,
                artifact_path=artifact_path,
                error=f"tenant upload failed: {error}",
            )

        if response.success:
            return self._result(
                success=True,
                region=region,
                artifact_path=artifact_path,
                status_code=response.status_code,
            )
        return self._result(
            success=False,
            region=region,
            artifact_path=artifact_path,
            status_code=response.status_code,
            error=f"tenant upload failed with status {response.status_code}",
        )

    @staticmethod
    def _result(
        *,
        success: bool,
        region: str,
        artifact_path: Path,
        status_code: Optional[int] = None,
        timed_out: bool = False,
        error: str = "",
    ) -> ExportResult:
        """Build an :class:`ExportResult` stamped with the current UTC instant."""
        return ExportResult(
            success=success,
            region_base_url=region,
            artifact_path=artifact_path,
            uploaded_utc=datetime.now(timezone.utc).isoformat(),
            status_code=status_code,
            timed_out=timed_out,
            error=error,
        )

    @staticmethod
    def _resolve_artifact_path(artifact: ArtifactInput) -> Path:
        """Resolve ``artifact`` to an existing ``.plg`` path or reject (Req 10.5).

        Accepts a :class:`PlgArtifact`, a path, or ``None``. Rejects when the
        artifact is absent or its file does not exist on disk, since a tenant
        export requires a real built artifact to upload.

        Raises:
            ArtifactNotBuiltError: If no built artifact exists.
        """
        if artifact is None:
            raise ArtifactNotBuiltError(NO_ARTIFACT_MESSAGE)

        if isinstance(artifact, PlgArtifact):
            path = Path(artifact.path)
        else:
            path = Path(artifact)

        if not path.is_file():
            raise ArtifactNotBuiltError(f"{NO_ARTIFACT_MESSAGE} (expected artifact at {path})")
        return path


class UrllibTenantUploader:
    """A stdlib-only :class:`TenantUploader` for real tenant uploads (Req 10.1, 10.3).

    Uploads the ``.plg`` bytes to ``<region_base_url><upload_path>`` via an HTTP
    ``POST`` authenticated with the InsightConnect API key, enforcing the overall
    timeout by passing it to :func:`urllib.request.urlopen`. Kept dependency-free
    on purpose; tests inject a fake uploader instead of exercising this path.
    """

    def __init__(self, *, upload_path: str = DEFAULT_PLUGIN_UPLOAD_PATH) -> None:
        """Create the uploader.

        Args:
            upload_path: The plugin-management endpoint path appended to the
                region base URL.
        """
        self._upload_path = upload_path

    def upload(
        self,
        *,
        region_base_url: str,
        api_key: str,
        artifact_path: Path,
        timeout: float,
    ) -> UploadResponse:
        """Upload the artifact and return the tenant API's response.

        Raises:
            TimeoutError: If the upload exceeds ``timeout`` seconds. The
                Export_Manager classifies this as a timeout failure (Req 10.3).
        """
        url = region_base_url.rstrip("/") + self._upload_path
        payload = Path(artifact_path).read_bytes()
        request = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={
                "X-Api-Key": api_key,
                "Content-Type": "application/octet-stream",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
                return UploadResponse(status_code=response.status, body=body)
        except TimeoutError:
            raise
        except urllib.error.HTTPError as error:
            body = _read_error_body(error)
            return UploadResponse(status_code=error.code, body=body)


def _read_error_body(error: urllib.error.HTTPError) -> str:
    """Best-effort read of an :class:`~urllib.error.HTTPError` response body."""
    try:
        raw = error.read()
    except Exception:  # noqa: BLE001 - the status code is what matters here.
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)
