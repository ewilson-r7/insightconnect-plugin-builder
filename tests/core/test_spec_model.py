"""Unit tests for the typed PluginSpec data model (task 1.2).

These cover specific examples and edge cases for SemVer, FieldSchema,
Component, and PluginSpec construction and serialization. The universal
round-trip property is covered separately by the property test (task 1.3).
"""

import pytest

from icplugin_builder.core.spec_model import (
    Component,
    FieldSchema,
    PluginSpec,
    SemVer,
)


class TestSemVer:
    def test_parse_valid(self):
        assert SemVer.parse("1.2.3") == SemVer(1, 2, 3)

    def test_parse_accepts_existing_semver(self):
        v = SemVer(2, 0, 1)
        assert SemVer.parse(v) is v

    def test_str_round_trips(self):
        assert str(SemVer.parse("10.20.30")) == "10.20.30"

    @pytest.mark.parametrize(
        "bad",
        ["1.2", "1.2.3.4", "v1.2.3", "1.2.x", "01.2.3", "", "1.02.3", "1.2.3-rc1"],
    )
    def test_parse_rejects_invalid(self, bad):
        assert SemVer.is_valid(bad) is False
        with pytest.raises(ValueError):
            SemVer.parse(bad)

    def test_total_ordering(self):
        assert SemVer(1, 0, 0) < SemVer(1, 0, 1) < SemVer(1, 1, 0) < SemVer(2, 0, 0)
        assert sorted([SemVer(2, 0, 0), SemVer(1, 5, 9), SemVer(1, 10, 0)]) == [
            SemVer(1, 5, 9),
            SemVer(1, 10, 0),
            SemVer(2, 0, 0),
        ]

    def test_bump_major_resets_minor_patch(self):
        assert SemVer(1, 4, 7).bump_major() == SemVer(2, 0, 0)

    def test_bump_patch(self):
        assert SemVer(1, 4, 7).bump_patch() == SemVer(1, 4, 8)

    def test_parse_invalid_error_names_version_and_expected_format(self):
        # Req 7.5: the error must identify the version field and state the
        # expected MAJOR.MINOR.PATCH format.
        with pytest.raises(ValueError) as excinfo:
            SemVer.parse("1.2")
        message = str(excinfo.value)
        assert "version" in message
        assert "MAJOR.MINOR.PATCH" in message

    def test_parse_non_string_error_names_version_and_expected_format(self):
        with pytest.raises(ValueError) as excinfo:
            SemVer.parse(123)
        message = str(excinfo.value)
        assert "version" in message
        assert "MAJOR.MINOR.PATCH" in message


class TestFieldSchema:
    def test_from_mapping_promotes_known_keys(self):
        fs = FieldSchema.from_mapping(
            {
                "title": "URL",
                "description": "Base URL",
                "type": "string",
                "required": True,
                "default": "https://example.com",
                "enum": ["a", "b"],
            }
        )
        assert fs.type == "string"
        assert fs.required is True
        assert fs.title == "URL"
        assert fs.default == "https://example.com"
        assert fs.enum == ["a", "b"]
        assert fs.extra == {}

    def test_unknown_keys_land_in_extra(self):
        fs = FieldSchema.from_mapping({"type": "string", "some_vendor_ext": 5})
        assert fs.extra == {"some_vendor_ext": 5}

    def test_to_mapping_is_inverse(self):
        raw = {
            "title": "Count",
            "description": "How many",
            "type": "integer",
            "required": False,
            "default": 3,
            "order": 2,
        }
        assert FieldSchema.from_mapping(raw).to_mapping() == raw

    def test_defaults(self):
        fs = FieldSchema.from_mapping({})
        assert fs.type == "string"
        assert fs.required is False


class TestComponent:
    def test_from_mapping_parses_io(self):
        comp = Component.from_mapping(
            {
                "title": "Create Incident",
                "description": "Create one",
                "input": {"name": {"type": "string", "required": True}},
                "output": {"id": {"type": "integer", "required": False}},
            }
        )
        assert comp.title == "Create Incident"
        assert comp.input["name"].required is True
        assert comp.output["id"].type == "integer"

    def test_to_mapping_omits_empty_io(self):
        comp = Component(title="Do", description="thing")
        assert comp.to_mapping() == {"title": "Do", "description": "thing"}


class TestPluginSpec:
    def _raw(self):
        return {
            "plugin_spec_version": "v2",
            "name": "example",
            "title": "Example",
            "description": "An example plugin",
            "version": "1.2.3",
            "vendor": "rapid7",
            "connection": {
                "url": {"title": "URL", "type": "string", "required": True},
            },
            "actions": {
                "run": {
                    "title": "Run",
                    "input": {"x": {"type": "integer", "required": True}},
                    "output": {"y": {"type": "string", "required": False}},
                }
            },
        }

    def test_from_mapping_builds_typed_tree(self):
        spec = PluginSpec.from_mapping(self._raw())
        assert spec.name == "example"
        assert spec.version == SemVer(1, 2, 3)
        assert spec.vendor == "rapid7"
        assert spec.connection["url"].required is True
        assert spec.actions["run"].input["x"].type == "integer"

    def test_typed_round_trip_equivalence(self):
        raw = self._raw()
        once = PluginSpec.from_mapping(raw)
        twice = PluginSpec.from_mapping(once.to_mapping())
        assert once == twice

    def test_unmodeled_top_level_keys_preserved(self):
        raw = self._raw()
        raw["support"] = "community"
        raw["sdk"] = {"type": "slim", "version": "5.4.7"}
        spec = PluginSpec.from_mapping(raw)
        assert spec.extra["support"] == "community"
        assert spec.extra["sdk"] == {"type": "slim", "version": "5.4.7"}
        assert PluginSpec.from_mapping(spec.to_mapping()) == spec

    def test_invalid_version_rejected(self):
        raw = self._raw()
        raw["version"] = "not-a-version"
        with pytest.raises(ValueError):
            PluginSpec.from_mapping(raw)
