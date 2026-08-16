"""Tests for obtaining vendor reference material and recording where it came from.

The thing being defended against is not a failed download. It is the agent
inventing endpoints when documentation is absent, which is why every test here
about a *failure* asserts that the failure is reported rather than quietly
tolerated: a source that silently yields nothing is indistinguishable, from the
agent's side, from having been given no documentation at all.

No test contacts the network. The fetcher is a protocol with an injected fake,
matching how the tenant uploader is tested.
"""

import asyncio
import base64
import json

import pytest

from icplugin_builder.integrations.reference_material import (
    ALLOWED_MEDIA_TYPES,
    ORIGIN_ATTACHMENT,
    ORIGIN_PLUGIN,
    ORIGIN_URL,
    PDF_MEDIA_TYPE,
    PROVENANCE_NAME,
    FetchedBytes,
    ReferenceAcquirer,
    ReferenceDocument,
    ReferenceSet,
    UrllibReferenceFetcher,
    extract_text,
    safe_reference_name,
    store_reference_set,
)


class FakeFetcher:
    """Returns scripted bytes per URL, recording what it was asked for."""

    def __init__(self, responses=None, error=None):
        self.responses = responses or {}
        self.error = error
        self.calls = []

    def fetch(self, url, *, timeout, max_bytes):
        self.calls.append({"url": url, "timeout": timeout, "max_bytes": max_bytes})
        if self.error is not None:
            raise self.error
        if url not in self.responses:
            raise OSError(f"no scripted response for {url}")
        data, media_type = self.responses[url]
        return FetchedBytes(data=data, media_type=media_type, url=url)


def _acquirer(**kwargs):
    kwargs.setdefault("fetcher", FakeFetcher())
    return ReferenceAcquirer(**kwargs)


class TestSafeName:
    """A supplied name is untrusted and must not be able to escape (Req 28.4)."""

    @pytest.mark.parametrize(
        "supplied",
        [
            "../../etc/passwd",
            "..\\..\\windows\\system32\\config",
            "/absolute/path/doc.md",
            "....//....//doc.md",
        ],
    )
    def test_traversal_is_flattened(self, supplied):
        name = safe_reference_name(supplied)
        assert "/" not in name
        assert "\\" not in name
        assert not name.startswith(".")

    def test_spaces_and_punctuation_are_replaced(self):
        assert safe_reference_name("okta api docs.pdf") == "okta_api_docs.pdf"

    def test_an_unusable_name_falls_back(self):
        assert safe_reference_name("///") == "reference.txt"
        assert safe_reference_name("") == "reference.txt"

    def test_the_provenance_record_cannot_be_overwritten(self):
        # A document named provenance.json would otherwise replace the record of
        # where every document came from.
        assert safe_reference_name(PROVENANCE_NAME) != PROVENANCE_NAME

    def test_a_long_name_is_capped_but_keeps_its_extension(self):
        name = safe_reference_name("a" * 400 + ".yaml")
        assert len(name) <= 120
        assert name.endswith(".yaml")


class TestExtraction:
    def test_text_passes_through_unchanged(self):
        text, extracted, _ = extract_text(b"GET /v1/users\n", media_type="text/plain")
        assert text == "GET /v1/users\n"
        assert extracted is False

    def test_empty_content_is_an_error_not_an_empty_document(self):
        # An empty reference file is worse than none: the agent reads it, learns
        # nothing, and proceeds as though it had been given documentation.
        with pytest.raises(ValueError, match="no readable text"):
            extract_text(b"   \n\t ", media_type="text/plain", name="empty.md")

    def test_a_real_pdf_is_extracted_and_marked_as_extracted(self):
        pdf = _one_page_pdf("GET /api/v1/users returns a user list")
        text, extracted, detail = extract_text(pdf, media_type=PDF_MEDIA_TYPE, name="api.pdf")
        assert "GET /api/v1/users" in text
        # Recorded as an extraction: it is not the original bytes, and a reader
        # comparing it against the vendor's PDF should know that.
        assert extracted is True
        assert "PDF page" in detail

    def test_a_pdf_is_detected_by_content_not_just_by_name(self):
        pdf = _one_page_pdf("POST /api/v1/tokens")
        text, extracted, _ = extract_text(pdf, media_type="text/plain", name="mislabelled.txt")
        assert "POST /api/v1/tokens" in text
        assert extracted is True

    def test_a_pdf_with_no_extractable_text_says_so(self):
        # A scanned PDF is images. This tool does not do OCR, and handing the agent
        # an empty file would let it fall back to inventing endpoints.
        empty_pdf = _one_page_pdf(None)
        with pytest.raises(ValueError, match="no extractable text"):
            extract_text(empty_pdf, media_type=PDF_MEDIA_TYPE, name="scan.pdf")


class TestAttachments:
    def test_a_text_attachment_records_its_provenance(self):
        document = _acquirer().from_attachment("openapi.yaml", b"openapi: 3.0.0\n")
        assert document.origin == ORIGIN_ATTACHMENT
        assert document.source == "openapi.yaml"
        assert document.byte_size == len(b"openapi: 3.0.0\n")
        assert len(document.sha256) == 64
        assert document.obtained_utc.endswith("+00:00")

    def test_a_base64_attachment_is_decoded(self):
        pdf = _one_page_pdf("DELETE /api/v1/users/{id}")
        payload = {
            "name": "api.pdf",
            "content": base64.b64encode(pdf).decode("ascii"),
            "encoding": "base64",
        }
        result = asyncio.run(_acquirer().acquire(attachments=[payload]))
        assert result.has_material
        assert "DELETE /api/v1/users" in result.documents[0].text
        assert result.documents[0].extracted is True

    def test_a_corrupt_base64_payload_is_reported_not_silently_dropped(self):
        payload = {"name": "api.pdf", "content": "not base64!!", "encoding": "base64"}
        result = asyncio.run(_acquirer().acquire(attachments=[payload]))
        assert not result.has_material
        assert len(result.failures) == 1
        assert "base64" in result.failures[0].reason

    def test_the_hash_covers_the_bytes_as_supplied(self):
        # Hashed before extraction, so the record identifies what arrived rather
        # than what this tool made of it.
        import hashlib

        data = b"openapi: 3.0.0\n"
        document = _acquirer().from_attachment("openapi.yaml", data)
        assert document.sha256 == hashlib.sha256(data).hexdigest()


class TestUrlRetrieval:
    def test_a_document_is_retrieved_and_attributed(self):
        fetcher = FakeFetcher({"https://api.example.com/docs": (b"GET /v1/things\n", "text/markdown")})
        result = asyncio.run(ReferenceAcquirer(fetcher=fetcher).acquire(urls=["https://api.example.com/docs"]))

        assert result.has_material
        document = result.documents[0]
        assert document.origin == ORIGIN_URL
        assert document.source == "https://api.example.com/docs"
        assert "retrieved from" in document.detail

    def test_the_size_and_timeout_ceilings_are_passed_to_the_fetcher(self):
        fetcher = FakeFetcher({"https://x.example.com/d": (b"docs", "text/plain")})
        acquirer = ReferenceAcquirer(fetcher=fetcher, max_bytes=1234, timeout_seconds=5.5)
        asyncio.run(acquirer.acquire(urls=["https://x.example.com/d"]))
        assert fetcher.calls[0]["max_bytes"] == 1234
        assert fetcher.calls[0]["timeout"] == 5.5

    def test_an_unusable_media_type_is_refused_with_a_reason(self):
        fetcher = FakeFetcher({"https://x.example.com/z": (b"PK\x03\x04binary", "application/zip")})
        result = asyncio.run(ReferenceAcquirer(fetcher=fetcher).acquire(urls=["https://x.example.com/z"]))

        assert not result.has_material
        assert "application/zip" in result.failures[0].reason
        # Req 28.16: nothing is substituted for what could not be obtained.
        assert result.documents == ()

    def test_a_failed_retrieval_is_reported(self):
        fetcher = FakeFetcher(error=OSError("connection refused"))
        result = asyncio.run(ReferenceAcquirer(fetcher=fetcher).acquire(urls=["https://x.example.com/d"]))
        assert "connection refused" in result.failures[0].reason

    def test_one_failure_does_not_discard_the_others(self):
        fetcher = FakeFetcher({"https://good.example.com/d": (b"GET /v1/ok\n", "text/plain")})
        result = asyncio.run(
            ReferenceAcquirer(fetcher=fetcher).acquire(urls=["https://bad.example.com/d", "https://good.example.com/d"])
        )
        assert len(result.documents) == 1
        assert len(result.failures) == 1

    def test_every_allowed_media_type_can_actually_be_stored(self):
        for media_type in ALLOWED_MEDIA_TYPES:
            data = _one_page_pdf("GET /v1/x") if media_type == PDF_MEDIA_TYPE else b"GET /v1/x\n"
            fetcher = FakeFetcher({"https://x.example.com/d": (data, media_type)})
            result = asyncio.run(ReferenceAcquirer(fetcher=fetcher).acquire(urls=["https://x.example.com/d"]))
            assert result.has_material, f"{media_type} is allowed but could not be stored"


class TestTransportPolicy:
    """HTTPS only, no credentials, no leaving HTTPS on a redirect (Req 28.15)."""

    def test_a_non_https_url_is_refused_before_any_request(self):
        fetcher = UrllibReferenceFetcher()
        for url in ("http://example.com/docs", "ftp://example.com/docs", "file:///etc/passwd"):
            with pytest.raises(ValueError, match="non-HTTPS"):
                fetcher.fetch(url, timeout=1.0, max_bytes=1024)

    def test_a_redirect_off_https_is_refused(self):
        from icplugin_builder.integrations.reference_material import _HttpsOnlyRedirectHandler

        handler = _HttpsOnlyRedirectHandler()
        with pytest.raises(ValueError, match="non-HTTPS"):
            handler.redirect_request(None, None, 302, "Found", {}, "http://example.com/elsewhere")

    def test_no_authorization_header_is_sent(self):
        # Reference material is public documentation. A retrieval that needs a
        # secret is not one this tool should make on the operator's behalf.
        import inspect

        source = inspect.getsource(UrllibReferenceFetcher.fetch)
        assert "Authorization" not in source
        assert "Cookie" not in source


class TestExistingPluginAsReference:
    """The tier that needs no network: a plugin already written for this vendor."""

    def _plugin(self, tmp_path):
        root = tmp_path / "okta"
        (root / "icon_okta" / "util").mkdir(parents=True)
        (root / "plugin.spec.yaml").write_text("name: okta\nvendor: rapid7\n", encoding="utf-8")
        (root / "help.md").write_text("# Okta\n\nGET /api/v1/users\n", encoding="utf-8")
        (root / "icon_okta" / "util" / "api.py").write_text(
            "class OktaApi:\n    def get_users(self):\n        return self._make_request('GET', 'api/v1/users')\n",
            encoding="utf-8",
        )
        return root

    def test_it_collects_the_api_documenting_files(self, tmp_path):
        documents = _acquirer().from_plugin(self._plugin(tmp_path))
        sources = " ".join(d.source for d in documents)
        assert "plugin.spec.yaml" in sources
        assert "help.md" in sources
        assert "api.py" in sources
        assert all(d.origin == ORIGIN_PLUGIN for d in documents)

    def test_a_directory_with_nothing_usable_is_reported(self, tmp_path):
        empty = tmp_path / "nothing"
        empty.mkdir()
        result = asyncio.run(_acquirer().acquire(plugin_dirs=[empty]))
        assert not result.has_material
        assert "no readable spec" in result.failures[0].reason

    def test_legacy_komand_clients_are_found_too(self, tmp_path):
        root = tmp_path / "legacy"
        (root / "komand_legacy" / "util").mkdir(parents=True)
        (root / "komand_legacy" / "util" / "api.py").write_text("X = 1\n", encoding="utf-8")
        documents = _acquirer().from_plugin(root)
        assert any("komand_legacy" in d.source for d in documents)


class TestStorage:
    def _set(self):
        return ReferenceSet(
            documents=(
                ReferenceDocument(
                    name="openapi.yaml",
                    text="openapi: 3.0.0\n",
                    origin=ORIGIN_ATTACHMENT,
                    source="openapi.yaml",
                    sha256="abc",
                    byte_size=15,
                ),
            )
        )

    def test_documents_land_in_the_tool_owned_subtree(self, tmp_path):
        paths = store_reference_set(tmp_path, self._set())
        # Inside .builder/, so reference material is excluded from the .plg and
        # cannot leak into a published plugin (Req 28.3).
        assert paths == (".builder/reference/openapi.yaml",)
        assert (tmp_path / ".builder" / "reference" / "openapi.yaml").read_text() == "openapi: 3.0.0\n"

    def test_provenance_is_written_beside_the_documents(self, tmp_path):
        store_reference_set(tmp_path, self._set())
        record = json.loads((tmp_path / ".builder" / "reference" / PROVENANCE_NAME).read_text())
        assert record["documents"][0]["origin"] == ORIGIN_ATTACHMENT
        assert record["documents"][0]["sha256"] == "abc"

    def test_failures_are_recorded_in_the_provenance(self, tmp_path):
        from icplugin_builder.integrations.reference_material import ReferenceFailure

        reference_set = ReferenceSet(
            documents=self._set().documents,
            failures=(ReferenceFailure(source="https://x/y", reason="404"),),
        )
        store_reference_set(tmp_path, reference_set)
        record = json.loads((tmp_path / ".builder" / "reference" / PROVENANCE_NAME).read_text())
        assert record["failures"] == [{"source": "https://x/y", "reason": "404"}]

    def test_nothing_is_written_when_there_is_nothing_to_store(self, tmp_path):
        assert store_reference_set(tmp_path, ReferenceSet()) == ()
        assert not (tmp_path / ".builder").exists()

    def test_duplicate_content_is_stored_once(self, tmp_path):
        # The same document supplied twice (attached and fetched) should not become
        # two files the agent has to reconcile.
        data = b"openapi: 3.0.0\n"
        acquirer = _acquirer()
        first = acquirer.from_attachment("openapi.yaml", data)
        second = acquirer.from_attachment("openapi.yaml", data)
        result = ReferenceSet(documents=(first, second))
        from icplugin_builder.integrations.reference_material import _deduplicate

        assert len(_deduplicate(result.documents)) == 1

    def test_distinct_documents_with_the_same_name_both_survive(self, tmp_path):
        acquirer = _acquirer()
        first = acquirer.from_attachment("api.md", b"GET /v1/a\n")
        second = acquirer.from_attachment("api.md", b"GET /v1/b\n")
        from icplugin_builder.integrations.reference_material import _deduplicate

        kept = _deduplicate((first, second))
        assert len(kept) == 2
        assert len({d.name for d in kept}) == 2


class TestSummary:
    def test_it_names_what_was_obtained_and_what_was_not(self):
        from icplugin_builder.integrations.reference_material import ReferenceFailure

        reference_set = ReferenceSet(
            documents=(ReferenceDocument(name="a.md", text="x", origin=ORIGIN_URL, source="https://a"),),
            failures=(ReferenceFailure(source="https://b", reason="timed out"),),
        )
        summary = reference_set.summary()
        assert "a.md" in summary
        assert "https://b" in summary
        assert "timed out" in summary

    def test_no_material_says_so_plainly(self):
        assert "No reference material" in ReferenceSet().summary()


def _one_page_pdf(text):
    """Build a real single-page PDF, with ``text`` on it, or no text when ``None``.

    A genuine PDF rather than a stub, so extraction is exercised for real. The
    ``None`` case produces a page with no text layer at all, which is what a
    scanned document looks like to a text extractor.
    """
    import io

    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    if text is not None:
        # The font resource is required: without it the content stream draws
        # nothing an extractor can recover, which is exactly the empty-text case.
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1"))
        page[NameObject("/Contents")] = writer._add_object(stream)

    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()
