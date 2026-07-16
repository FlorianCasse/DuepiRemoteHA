"""Metadata, translation, branding, and release regression tests."""

from __future__ import annotations

import json
from pathlib import Path
import struct


ROOT = Path(__file__).parents[1]
INTEGRATION = ROOT / "custom_components" / "duepi"


def _load_json(path: Path) -> dict[str, object]:
    """Load a repository JSON document."""
    return json.loads(path.read_text())


def _structure(value: object) -> object:
    """Return nested translation keys while ignoring translated values."""
    if isinstance(value, dict):
        return {key: _structure(child) for key, child in value.items()}
    return type(value)


def test_english_translation_exactly_matches_source_strings() -> None:
    """Custom integrations need an explicit runtime English translation."""
    strings = (INTEGRATION / "strings.json").read_text()
    english = (INTEGRATION / "translations" / "en.json").read_text()
    assert english == strings


def test_french_translation_has_the_same_key_structure() -> None:
    """French covers every form, error, option, and entity translation key."""
    source = _load_json(INTEGRATION / "strings.json")
    french = _load_json(INTEGRATION / "translations" / "fr.json")
    assert _structure(french) == _structure(source)


def test_release_metadata_is_consistent() -> None:
    """Manifest and HACS metadata agree on the v1.3 release contract."""
    manifest = _load_json(INTEGRATION / "manifest.json")
    hacs = _load_json(ROOT / "hacs.json")
    assert manifest["version"] == "1.3.0"
    assert hacs["zip_release"] is True
    assert hacs["filename"] == "duepi.zip"


def test_brand_icon_is_a_transparent_256_pixel_png() -> None:
    """The local brand asset satisfies HACS without a third-party logo."""
    data = (INTEGRATION / "brand" / "icon.png").read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    assert (width, height) == (256, 256)
    assert data[25] == 6  # PNG color type RGBA


def test_release_workflow_builds_a_rooted_hacs_archive() -> None:
    """Release automation uses the supported action and root archive layout."""
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert "working-directory: custom_components/duepi" in workflow
    assert "softprops/action-gh-release@v3" in workflow
    assert "grep -qx \"manifest.json\"" in workflow
    assert "grep -q '^custom_components/'" in workflow
