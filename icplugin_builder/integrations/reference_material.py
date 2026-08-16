"""Obtains vendor reference material, and records where every piece of it came from.

A generated plugin is only as correct as its knowledge of the vendor's API, and
the Plugin_Agent has no way to look that up: its tool set is read/write/shell/
search plus MCP, with no fetch server enabled. Given nothing, it infers endpoint
paths -- and inferred endpoints are wrong. This module is where the knowledge comes
from instead (Req 28).

Three decisions shape it, and they are worth stating because each rules out an
easier design.

**This tool retrieves; the agent does not.** Granting the agent network access
would put fetched pages inside the reasoning of a process that can run shell
commands, and the project's conventions already forbid putting untrusted content
into such a prompt. Retrieval happens here and the result is written to a file, so
the agent's contract is unchanged: it reads files (Req 28.10).

**Nothing is discovered.** There is no search. A vendor name alone yields a
request for a URL or an existing plugin to reference, because a guessed
documentation source is a guessed endpoint one step earlier (Req 28.12). Every
retrieval is therefore of a location the user supplied, and supplying it is what
authorizes it.

**Traceable is not verified.** Every document carries its origin, retrieval time,
media type, size, and content hash, and the agent is asked to cite which document
an endpoint came from. That establishes *where* a path came from. It does not
establish that the path is right -- only calling the API would. The two are kept
verbally distinct so "sourced" is never read as "verified" (Req 28.9, 28.14).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Protocol, Sequence, Tuple, Union

__all__ = [
    "ORIGIN_ATTACHMENT",
    "ORIGIN_URL",
    "ORIGIN_PLUGIN",
    "REFERENCE_DIR",
    "PROVENANCE_NAME",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "PDF_MEDIA_TYPE",
    "ALLOWED_MEDIA_TYPES",
    "PLUGIN_REFERENCE_FILES",
    "ReferenceDocument",
    "ReferenceFailure",
    "ReferenceSet",
    "FetchedBytes",
    "Fetcher",
    "UrllibReferenceFetcher",
    "ReferenceAcquirer",
    "ReferenceState",
    "safe_reference_name",
    "extract_text",
    "store_reference_set",
    "record_no_reference",
    "read_reference_state",
]

logger = logging.getLogger(__name__)

#: Where a piece of reference material came from.
ORIGIN_ATTACHMENT = "attachment"
ORIGIN_URL = "url"
ORIGIN_PLUGIN = "plugin"

#: Reference material lives under the project's tool-owned ``.builder/`` subtree,
#: so it is excluded from the packaged artifact and cannot leak into a published
#: plugin (Req 28.3).
REFERENCE_DIR = "reference"

#: The provenance record written beside the stored documents (Req 28.9).
PROVENANCE_NAME = "provenance.json"

#: Retrieval ceilings. A vendor API document is a document; anything far larger is
#: not what was asked for, and reading it costs the agent context it needs for the
#: plugin (Req 28.15).
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 30.0

PDF_MEDIA_TYPE = "application/pdf"

#: Media types that can be stored as text the agent can read. Anything else is
#: refused rather than written as bytes the agent cannot use (Req 28.15).
ALLOWED_MEDIA_TYPES: Tuple[str, ...] = (
    "text/plain",
    "text/markdown",
    "text/html",
    "text/yaml",
    "text/x-yaml",
    "application/json",
    "application/yaml",
    "application/x-yaml",
    "application/xml",
    "text/xml",
    PDF_MEDIA_TYPE,
)

#: The files of an existing plugin that document a vendor's API: what the spec
#: declares, what the docs say, and how the client actually calls it.
PLUGIN_REFERENCE_FILES: Tuple[str, ...] = ("plugin.spec.yaml", "help.md")

#: Characters allowed in a stored filename. Everything else is replaced, which is
#: what keeps a supplied name from escaping the reference directory (Req 28.4).
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")

#: Cap on a stored filename, leaving room for the directory path on any platform.
_MAX_NAME_LENGTH = 120


@dataclass(frozen=True)
class ReferenceDocument:
    """One piece of reference material, with its provenance.

    Attributes:
        name: the filename it is stored under, already made safe.
        text: the readable form handed to the agent.
        origin: :data:`ORIGIN_ATTACHMENT`, :data:`ORIGIN_URL`, or
            :data:`ORIGIN_PLUGIN`.
        source: where it came from -- the URL, the attachment's given name, or the
            plugin file's path. Recorded so content that influenced an
            implementation is attributable (Req 28.17).
        media_type: the media type it arrived as.
        extracted: ``True`` when :attr:`text` is an extraction from a format the
            agent cannot read directly (a PDF), rather than the original bytes.
            Recorded because an extraction can lose structure the original had
            (Req 28.11).
        obtained_utc: when it was obtained, ISO-8601 UTC.
        sha256: hash of the bytes as obtained, before any extraction.
        byte_size: size of those bytes.
        detail: a note about how it was obtained or extracted.
    """

    name: str
    text: str
    origin: str
    source: str
    media_type: str = "text/plain"
    extracted: bool = False
    obtained_utc: str = ""
    sha256: str = ""
    byte_size: int = 0
    detail: str = ""

    def provenance(self) -> dict:
        """Return the provenance record for this document (Req 28.9)."""
        return {
            "name": self.name,
            "origin": self.origin,
            "source": self.source,
            "media_type": self.media_type,
            "extracted": self.extracted,
            "obtained_utc": self.obtained_utc,
            "sha256": self.sha256,
            "byte_size": self.byte_size,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ReferenceFailure:
    """A piece of reference material that could not be obtained.

    Reported rather than swallowed: the alternative to real documentation is the
    agent inventing endpoints, so a failure the operator never sees is how a
    plugin ends up built on guesses (Req 28.16).

    Attributes:
        source: what was attempted.
        reason: why it did not work, in terms the operator can act on.
    """

    source: str
    reason: str

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return f"{self.source}: {self.reason}"


@dataclass(frozen=True)
class ReferenceSet:
    """The reference material gathered for one implementation run.

    Attributes:
        documents: what was obtained.
        failures: what was attempted and could not be obtained.
    """

    documents: Tuple[ReferenceDocument, ...] = ()
    failures: Tuple[ReferenceFailure, ...] = ()

    @property
    def has_material(self) -> bool:
        """Return ``True`` iff at least one document was obtained."""
        return bool(self.documents)

    def names(self) -> Tuple[str, ...]:
        """The stored filenames, in order."""
        return tuple(document.name for document in self.documents)

    def summary(self) -> str:
        """Return a one-line summary naming what was obtained and what was not."""
        if not self.documents and not self.failures:
            return "No reference material was supplied."
        parts = []
        if self.documents:
            parts.append(f"{len(self.documents)} document(s): {', '.join(self.names())}")
        if self.failures:
            parts.append(f"{len(self.failures)} could not be obtained: {'; '.join(str(f) for f in self.failures)}")
        return "; ".join(parts)


def safe_reference_name(name: str, *, fallback: str = "reference.txt") -> str:
    """Derive a filename that cannot write outside the reference directory (Req 28.4).

    Takes the final path component, replaces anything outside a conservative
    character set, and rejects the names that would still escape or collide with
    the provenance record. A supplied ``../../etc/passwd`` becomes a flat,
    harmless filename rather than a traversal.

    Args:
        name: the supplied name, which is untrusted.
        fallback: used when nothing usable survives.

    Returns:
        A safe, non-empty filename.
    """
    candidate = PurePosixName(name)
    candidate = _UNSAFE_NAME.sub("_", candidate).strip("._-")
    if not candidate or set(candidate) <= {"_"}:
        return fallback
    if len(candidate) > _MAX_NAME_LENGTH:
        stem, _, suffix = candidate.rpartition(".")
        if stem and len(suffix) <= 8:
            candidate = stem[: _MAX_NAME_LENGTH - len(suffix) - 1] + "." + suffix
        else:
            candidate = candidate[:_MAX_NAME_LENGTH]
    if candidate == PROVENANCE_NAME:
        return f"supplied_{candidate}"
    return candidate


def PurePosixName(name: str) -> str:  # noqa: N802 - reads as a helper at call sites
    """Return the final component of ``name``, treating both separators as such.

    A supplied name may use either separator regardless of this platform, so both
    are collapsed before the final component is taken.
    """
    return str(name).replace("\\", "/").rsplit("/", 1)[-1]


def extract_text(data: bytes, *, media_type: str, name: str = "") -> Tuple[str, bool, str]:
    """Return ``(text, extracted, detail)`` for ``data``.

    A PDF is the case this exists for: written verbatim it is bytes the agent
    cannot read, so its text is extracted and the substitution is recorded
    (Req 28.11). Everything else is decoded as text unchanged.

    Args:
        data: the bytes as obtained.
        media_type: the media type they arrived as.
        name: the document name, for the failure message.

    Returns:
        The readable text, whether it is an extraction, and a note about how.

    Raises:
        ValueError: when the bytes cannot be turned into readable text at all.
    """
    if media_type == PDF_MEDIA_TYPE or data[:5] == b"%PDF-":
        return _extract_pdf_text(data, name=name)
    text = data.decode("utf-8", errors="replace")
    if not text.strip():
        raise ValueError(f"{name or 'the document'} contained no readable text")
    return text, False, ""


def _extract_pdf_text(data: bytes, *, name: str = "") -> Tuple[str, bool, str]:
    """Extract a PDF's text, or explain why it could not be extracted."""
    try:
        from pypdf import PdfReader  # imported lazily so a missing extra is a message, not a crash
    except ImportError as error:  # pragma: no cover - depends on the environment
        raise ValueError(
            f"cannot read {name or 'the PDF'}: PDF text extraction needs pypdf ({error}); "
            "install it or supply the documentation as text, Markdown, or an OpenAPI spec"
        ) from error

    import io

    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as error:  # noqa: BLE001 - pypdf raises a range of parse errors
        raise ValueError(f"cannot read {name or 'the PDF'}: {error}") from error

    text = "\n\n".join(page for page in pages if page.strip())
    if not text.strip():
        # A scanned PDF is images; there is no text to extract and OCR is not
        # something this tool does. Saying so is better than handing the agent an
        # empty file it will quietly work around by inventing endpoints.
        raise ValueError(
            f"{name or 'the PDF'} has no extractable text (it may be scanned images); "
            "supply a text, Markdown, or OpenAPI version instead"
        )
    return text, True, f"text extracted from {len(pages)} PDF page(s)"


@dataclass(frozen=True)
class FetchedBytes:
    """What a :class:`Fetcher` returns.

    Attributes:
        data: the response body.
        media_type: the media type from the response, lowercased and without
            parameters.
        url: the URL actually read, after any redirects.
    """

    data: bytes
    media_type: str
    url: str


class Fetcher(Protocol):
    """Retrieves the bytes at a URL. Injected so tests contact no network."""

    def fetch(self, url: str, *, timeout: float, max_bytes: int) -> FetchedBytes:  # pragma: no cover - protocol
        ...


class UrllibReferenceFetcher:
    """A stdlib-only :class:`Fetcher` for real retrieval (Req 28.15).

    Deliberately spare. It sends no credentials, carries no cookies or session
    state, refuses anything that is not HTTPS -- including a redirect that leaves
    HTTPS -- and stops reading at the size ceiling rather than trusting a declared
    ``Content-Length``.
    """

    def __init__(self, *, user_agent: str = "insightconnect-plugin-builder") -> None:
        """Configure the fetcher.

        Args:
            user_agent: sent so an operator reading their own vendor's access logs
                can tell what made the request.
        """
        self._user_agent = user_agent

    def fetch(self, url: str, *, timeout: float, max_bytes: int) -> FetchedBytes:
        """Retrieve ``url``, refusing anything that is not plain HTTPS.

        Raises:
            ValueError: when the URL or the response is refused by policy.
            OSError: when the request itself fails.
        """
        _require_https(url)
        # No credential, cookie, or auth header is ever attached: reference
        # material is public documentation, and a retrieval that needs a secret is
        # one this tool should not be making on the operator's behalf.
        request = urllib.request.Request(  # noqa: S310 - scheme is checked above
            url,
            method="GET",
            headers={"User-Agent": self._user_agent, "Accept": "*/*"},
        )
        opener = urllib.request.build_opener(_HttpsOnlyRedirectHandler())
        with opener.open(request, timeout=timeout) as response:
            final_url = response.geturl()
            _require_https(final_url)
            media_type = str(response.headers.get_content_type() or "").lower()
            # Read one byte past the ceiling so an oversized body is detected
            # rather than silently truncated into a document that looks complete.
            data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError(f"the response exceeded the {max_bytes} byte limit")
        return FetchedBytes(data=data, media_type=media_type, url=final_url)


class _HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follows redirects only while they stay on HTTPS."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102 - stdlib signature
        _require_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _require_https(url: str) -> None:
    """Raise :class:`ValueError` unless ``url`` is HTTPS."""
    if not str(url).lower().startswith("https://"):
        raise ValueError(f"refusing a non-HTTPS reference URL: {url!r}")


class ReferenceAcquirer:
    """Turns supplied documents, URLs, and existing plugins into stored reference material."""

    def __init__(
        self,
        *,
        fetcher: Optional[Fetcher] = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        allowed_media_types: Sequence[str] = ALLOWED_MEDIA_TYPES,
    ) -> None:
        """Configure acquisition.

        Args:
            fetcher: retrieves URLs; defaults to :class:`UrllibReferenceFetcher`.
                Inject a fake in tests so no network is contacted.
            max_bytes: response size ceiling (Req 28.15).
            timeout_seconds: per-retrieval timeout (Req 28.15).
            allowed_media_types: media types that can be stored as text.
        """
        self._fetcher = fetcher if fetcher is not None else UrllibReferenceFetcher()
        self._max_bytes = max_bytes
        self._timeout = timeout_seconds
        self._allowed = tuple(allowed_media_types)

    def from_attachment(self, name: str, data: bytes, *, media_type: str = "") -> ReferenceDocument:
        """Build a document from a supplied file.

        Args:
            name: the supplied filename, untrusted.
            data: its bytes.
            media_type: its media type when known; inferred from the name and
                content otherwise.

        Returns:
            The :class:`ReferenceDocument`.

        Raises:
            ValueError: when the bytes yield no readable text.
        """
        resolved = media_type.lower() or _media_type_for(name, data)
        text, extracted, detail = extract_text(data, media_type=resolved, name=name)
        return _document(
            name=safe_reference_name(name),
            text=text,
            origin=ORIGIN_ATTACHMENT,
            source=name,
            media_type=resolved,
            extracted=extracted,
            data=data,
            detail=detail,
        )

    async def from_url(self, url: str) -> ReferenceDocument:
        """Retrieve a document from ``url`` (Req 28.8, 28.15).

        The retrieval runs in a worker thread so the caller's event loop is not
        blocked, matching how the rest of this package treats blocking I/O.

        Raises:
            ValueError: when the URL or its response is refused by policy, or the
                body yields no readable text.
            OSError: when the request fails.
        """
        fetched = await asyncio.to_thread(self._fetcher.fetch, url, timeout=self._timeout, max_bytes=self._max_bytes)
        media_type = fetched.media_type or _media_type_for(url, fetched.data)
        if media_type not in self._allowed:
            raise ValueError(
                f"{media_type or 'an unknown media type'} cannot be stored as text the agent can read; "
                f"expected one of {', '.join(self._allowed)}"
            )
        text, extracted, detail = extract_text(fetched.data, media_type=media_type, name=url)
        notes = [note for note in (f"retrieved from {fetched.url}", detail) if note]
        return _document(
            name=safe_reference_name(_name_from_url(fetched.url), fallback="fetched-reference.txt"),
            text=text,
            origin=ORIGIN_URL,
            source=fetched.url,
            media_type=media_type,
            extracted=extracted,
            data=fetched.data,
            detail="; ".join(notes),
        )

    def from_plugin(self, plugin_dir: Union[str, Path]) -> List[ReferenceDocument]:
        """Build documents from an existing plugin's spec, docs, and API client.

        The third tier of reference material, and the only one that needs no
        network: a plugin already written for this vendor documents the vendor's
        API in the most directly usable form there is (Req 28.8).

        Args:
            plugin_dir: the existing plugin's directory.

        Returns:
            A document per readable reference file, empty when none are present.
        """
        root = Path(plugin_dir)
        found: List[ReferenceDocument] = []
        candidates = [root / name for name in PLUGIN_REFERENCE_FILES]
        candidates.extend(sorted(root.glob("icon_*/util/*.py")))
        candidates.extend(sorted(root.glob("komand_*/util/*.py")))

        for path in candidates:
            if not path.is_file():
                continue
            try:
                data = path.read_bytes()
                text, extracted, detail = extract_text(data, media_type="text/plain", name=path.name)
            except (OSError, ValueError) as error:
                logger.info("skipping unreadable plugin reference %s: %s", path, error)
                continue
            relative = path.relative_to(root).as_posix()
            found.append(
                _document(
                    name=safe_reference_name(f"{root.name}-{relative}"),
                    text=text,
                    origin=ORIGIN_PLUGIN,
                    source=str(path),
                    media_type="text/plain",
                    extracted=extracted,
                    data=data,
                    detail=f"from the existing {root.name} plugin",
                )
            )
        return found

    async def acquire(
        self,
        *,
        attachments: Iterable[dict] = (),
        urls: Iterable[str] = (),
        plugin_dirs: Iterable[Union[str, Path]] = (),
    ) -> ReferenceSet:
        """Gather everything supplied, reporting each piece that could not be obtained.

        One failing source never discards the others, and a failure is never
        replaced with inferred content (Req 28.16): it is reported so the operator
        can supply something better.

        Args:
            attachments: supplied files as ``{"name", "content"}`` mappings, where
                ``content`` is text unless ``encoding`` is ``"base64"``.
            urls: documentation URLs the user supplied.
            plugin_dirs: existing plugin directories to reference.

        Returns:
            The :class:`ReferenceSet`.
        """
        documents: List[ReferenceDocument] = []
        failures: List[ReferenceFailure] = []

        for attachment in attachments:
            name = str(attachment.get("name") or "reference")
            try:
                documents.append(
                    self.from_attachment(
                        name,
                        _attachment_bytes(attachment),
                        media_type=str(attachment.get("media_type") or ""),
                    )
                )
            except (ValueError, TypeError) as error:
                failures.append(ReferenceFailure(source=name, reason=str(error)))

        for url in urls:
            try:
                documents.append(await self.from_url(url))
            except (ValueError, OSError, urllib.error.URLError) as error:
                failures.append(ReferenceFailure(source=url, reason=str(error)))

        for plugin_dir in plugin_dirs:
            found = self.from_plugin(plugin_dir)
            if found:
                documents.extend(found)
            else:
                failures.append(
                    ReferenceFailure(
                        source=str(plugin_dir),
                        reason="no readable spec, help, or client files were found in that plugin",
                    )
                )

        return ReferenceSet(documents=_deduplicate(documents), failures=tuple(failures))


def _deduplicate(documents: Sequence[ReferenceDocument]) -> Tuple[ReferenceDocument, ...]:
    """Drop documents with duplicate content, and make the stored names unique."""
    seen_hashes = set()
    used_names = set()
    kept: List[ReferenceDocument] = []
    for document in documents:
        if document.sha256 and document.sha256 in seen_hashes:
            continue
        seen_hashes.add(document.sha256)
        name = document.name
        if name in used_names:
            stem, _, suffix = name.rpartition(".")
            index = 2
            while name in used_names:
                name = f"{stem}-{index}.{suffix}" if stem else f"{name}-{index}"
                index += 1
            document = ReferenceDocument(**{**document.provenance(), "name": name, "text": document.text})
        used_names.add(name)
        kept.append(document)
    return tuple(kept)


def _document(
    *,
    name: str,
    text: str,
    origin: str,
    source: str,
    media_type: str,
    extracted: bool,
    data: bytes,
    detail: str,
) -> ReferenceDocument:
    """Build a document, hashing the bytes as obtained."""
    return ReferenceDocument(
        name=name,
        text=text,
        origin=origin,
        source=source,
        media_type=media_type,
        extracted=extracted,
        obtained_utc=datetime.now(timezone.utc).isoformat(),
        sha256=hashlib.sha256(data).hexdigest(),
        byte_size=len(data),
        detail=detail,
    )


def _attachment_bytes(attachment: dict) -> bytes:
    """Return an attachment's bytes, decoding a base64 payload when marked as one."""
    content = attachment.get("content")
    if isinstance(content, bytes):
        return content
    text = str(content or "")
    if str(attachment.get("encoding") or "").lower() == "base64":
        import base64
        import binascii

        try:
            return base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError(f"the base64 payload could not be decoded: {error}") from error
    return text.encode("utf-8")


def _media_type_for(name: str, data: bytes) -> str:
    """Infer a media type from a name and the leading bytes."""
    if data[:5] == b"%PDF-":
        return PDF_MEDIA_TYPE
    lowered = str(name).lower()
    for suffix, media_type in (
        (".pdf", PDF_MEDIA_TYPE),
        (".json", "application/json"),
        (".yaml", "application/yaml"),
        (".yml", "application/yaml"),
        (".md", "text/markdown"),
        (".html", "text/html"),
        (".htm", "text/html"),
        (".xml", "application/xml"),
    ):
        if lowered.endswith(suffix):
            return media_type
    return "text/plain"


def _name_from_url(url: str) -> str:
    """Derive a filename from a URL's path, falling back to its host."""
    without_scheme = str(url).split("://", 1)[-1]
    path = without_scheme.split("?", 1)[0].split("#", 1)[0]
    host, _, remainder = path.partition("/")
    tail = remainder.rstrip("/").rsplit("/", 1)[-1] if remainder.strip("/") else ""
    if not tail:
        return f"{host}.txt" if host else "fetched-reference.txt"
    return tail if "." in tail else f"{tail}.txt"


@dataclass(frozen=True)
class ReferenceState:
    """What a plugin's tree records about the documentation it was built from.

    Read from the stored provenance rather than from session memory, so the answer
    survives the session and can be checked by anything holding the tree.

    Attributes:
        recorded: whether a provenance record exists at all. ``False`` means
            nothing is known -- which is not the same as "no documentation", since a
            plugin that calls no external API needs none.
        document_count: how many documents were stored.
        without_reference: whether implementation deliberately proceeded with no
            documentation (Req 28.13).
        detail: the reason recorded when proceeding without documentation.
    """

    recorded: bool = False
    document_count: int = 0
    without_reference: bool = False
    detail: str = ""

    @property
    def has_material(self) -> bool:
        """Return ``True`` iff documentation was stored for this plugin."""
        return self.document_count > 0


def read_reference_state(project_dir: Union[str, Path]) -> ReferenceState:
    """Read what ``project_dir`` records about its reference material.

    Args:
        project_dir: the plugin working tree.

    Returns:
        A :class:`ReferenceState`. ``recorded`` is ``False`` when there is no
        record, which is deliberately distinct from a record saying there was no
        documentation.
    """
    path = Path(project_dir) / ".builder" / REFERENCE_DIR / PROVENANCE_NAME
    if not path.is_file():
        return ReferenceState()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ReferenceState()
    if not isinstance(payload, dict):
        return ReferenceState()
    documents = payload.get("documents")
    return ReferenceState(
        recorded=True,
        document_count=len(documents) if isinstance(documents, list) else 0,
        without_reference=bool(payload.get("implemented_without_reference")),
        detail=str(payload.get("detail") or ""),
    )


def record_no_reference(project_dir: Union[str, Path], *, detail: str = "") -> bool:
    """Record that implementation proceeded with no reference material (Req 28.13).

    Written into the tree rather than held in the session so the gap outlives the
    conversation: a plugin built on guessed endpoints should still say so when it is
    reopened tomorrow.

    Args:
        project_dir: the plugin working tree.
        detail: why it proceeded without documentation.

    Returns:
        ``True`` when the record was written.
    """
    directory = Path(project_dir) / ".builder" / REFERENCE_DIR
    try:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / PROVENANCE_NAME).write_text(
            json.dumps(
                {
                    "documents": [],
                    "failures": [],
                    "implemented_without_reference": True,
                    "detail": detail,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError as error:
        logger.warning("could not record the absence of reference material: %s", error)
        return False
    return True


def store_reference_set(
    project_dir: Union[str, Path],
    reference_set: ReferenceSet,
) -> Tuple[str, ...]:
    """Write ``reference_set`` into the project's reference directory.

    Files land under ``.builder/reference/`` -- tool-owned metadata, excluded from
    the packaged artifact, so documentation cannot leak into a published plugin
    (Req 28.3). A provenance record is written beside them so every document's
    origin survives the run (Req 28.9).

    Args:
        project_dir: the plugin working tree.
        reference_set: the documents to store.

    Returns:
        The stored paths relative to the project root, for naming in the agent's
        instruction. Empty when there was nothing to store or the write failed --
        reference material is an aid, not a precondition (Req 28.7).
    """
    if not reference_set.documents:
        return ()

    root = Path(project_dir)
    directory = root / ".builder" / REFERENCE_DIR
    written: List[str] = []
    try:
        directory.mkdir(parents=True, exist_ok=True)
        for document in reference_set.documents:
            # Resolve and re-check: the name is already sanitised, and this
            # confirms the result actually lands inside the reference directory.
            target = (directory / document.name).resolve()
            if directory.resolve() not in target.parents:
                logger.warning("refusing to write reference material outside %s: %s", directory, target)
                continue
            target.write_text(document.text, encoding="utf-8")
            written.append(f".builder/{REFERENCE_DIR}/{document.name}")

        provenance = {
            "documents": [document.provenance() for document in reference_set.documents],
            "failures": [{"source": f.source, "reason": f.reason} for f in reference_set.failures],
        }
        (directory / PROVENANCE_NAME).write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except OSError as error:
        logger.warning("could not stage reference material for the agent: %s", error)
        return ()
    return tuple(written)
