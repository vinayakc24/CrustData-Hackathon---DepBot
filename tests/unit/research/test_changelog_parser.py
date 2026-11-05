from __future__ import annotations

import pytest

from depbot.research.changelog_parser import (
    ChangelogParseError,
    ChangelogSection,
    find_version_section,
)


@pytest.mark.parametrize("heading", ["## 2.0.0", "## v2.0.0", "## [2.0.0]", "## [2.0.0] - 2026-11-06"])
def test_supported_heading_forms(heading: str) -> None:
    result = find_version_section(f"{heading}\nChanged API\n", "2.0.0")
    assert result is not None
    assert result.heading == heading
    assert result.content == "Changed API\n"
    assert result.version in ("2.0.0", "v2.0.0")


def test_extracts_requested_section_between_versions_with_exact_lines() -> None:
    text = "# Changelog\n\n## 3.0.0\nNew release\n\n## [2.0.0] - 2026-11-06\nRemoved old_sum\nSee migration guide.\n\n## 1.0.0\nInitial\n"
    section = find_version_section(text, "v2.0.0")
    assert section == ChangelogSection(
        version="2.0.0",
        heading="## [2.0.0] - 2026-11-06",
        content="Removed old_sum\nSee migration guide.\n\n",
        line_start=6,
        line_end=9,
    )


@pytest.mark.parametrize("requested", ["2.0.0", "v2.0.0", "[2.0.0]", "[v2.0.0]"])
def test_normalizes_optional_prefix_and_brackets(requested: str) -> None:
    assert find_version_section("## v2.0.0\ntext", requested) is not None


def test_exact_match_does_not_match_prefix_or_prerelease() -> None:
    text = "## 12.0.0\nmajor\n## 2.0.0-rc1\npreview\n"
    assert find_version_section(text, "2.0.0") is None
    assert find_version_section(text, "2.0.0-rc1").content == "preview\n"


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_preserves_line_endings_and_content(newline: str) -> None:
    text = newline.join(["## 1.0.0", "older", "## v2.0.0", "  exact text  ", "## 3.0.0", "newer", ""])
    section = find_version_section(text, "2.0.0")
    assert section is not None
    assert section.content == f"  exact text  {newline}"
    assert section.line_start == 3
    assert section.line_end == 4


def test_missing_empty_and_malformed_inputs() -> None:
    assert find_version_section("## 1.0.0\ntext", "2.0.0") is None
    assert find_version_section("", "2.0.0") is None
    assert find_version_section("## [2.0.0\nmalformed\n## 1.0.0\n", "2.0.0") is None
    with pytest.raises(ValueError, match="target_version"):
        find_version_section("## 1.0.0", " ")


def test_duplicate_matching_sections_are_rejected() -> None:
    with pytest.raises(ChangelogParseError, match="duplicate"):
        find_version_section("## 2.0.0\na\n## v2.0.0\nb\n", "2.0.0")
