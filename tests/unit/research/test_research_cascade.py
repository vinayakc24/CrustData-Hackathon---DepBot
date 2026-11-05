"""Research cascade: version range, R1 x R2 merge, additive leftovers-only LLM, provenance."""

import json
from pathlib import Path

import pytest

import depbot.research.breaking_change_analyzer as bca
from depbot.llm.base import FixtureLLMClient
from depbot.llm.runtime import use_llm_client
from depbot.models import ChangeSource, ChangeType, Dependency, ReleaseEvent
from depbot.research.github_client import GitHubRelease
from tests.unit.research.test_header_diff import HAVE_CLANG, OLD, _job_artifacts

needs_clang = pytest.mark.skipif(not HAVE_CLANG, reason="libclang not available")


def _run(tmp_path, notes_text, *, current="1.0.0", target="2.0.0", artifacts=None, client=None):
    notes = tmp_path / "notes.md"
    notes.write_text(notes_text, encoding="utf-8")
    event = ReleaseEvent(event_id="e", dependency_name="toydep", upstream_repo="local/toydep",
                         previous_version=current, target_version=target, changelog_path=str(notes))
    dep = Dependency(name="toydep", current_version=current, target_version=target,
                     package_manager="conan", manifest_path="conanfile.txt")
    with use_llm_client(client):
        return bca.research_release(event=event, dependency=dep, artifacts_dir=artifacts or tmp_path / "art")


def test_every_section_in_the_upgrade_range_is_read(tmp_path):
    notes = ("# Changelog\n\n## 3.0.0\n\nThis release removes `toydep::later`.\n\n"
             "## 2.0.0\n\nThis release removes `toydep::old_sum`. Use `toydep::sum`.\n\n"
             "## 1.1.0\n\nRenamed `toydep::mul` to `toydep::product`.\n\n"
             "## 1.0.0\n\nThis release removes `toydep::ancient`.\n")
    changes = _run(tmp_path, notes, current="1.0.0", target="2.0.0")
    assert sorted(s for c in changes for s in c.affected_symbols) == ["mul", "old_sum"]  # not 'later'/'ancient'
    assert all(c.source == ChangeSource.CHANGELOG_RULE for c in changes)


NEW = OLD.replace("int old_sum(int a, int b);", "int sum(int a, int b);")


def _r1_artifacts(tmp_path):
    for side, text in (("old", OLD), ("new", NEW)):
        (tmp_path / side / "toydep").mkdir(parents=True)
        (tmp_path / side / "toydep" / "toydep.hpp").write_text(text)
    shared = tmp_path / "zlib"
    shared.mkdir()
    return _job_artifacts(tmp_path, tmp_path / "old", tmp_path / "new", shared)


@needs_clang
def test_r1_is_authoritative_and_changelog_advice_is_attached_once(tmp_path):
    art = _r1_artifacts(tmp_path)
    changes = _run(tmp_path, "## 2.0.0\n\nThis release removes `toydep::old_sum`. Use `toydep::sum`.\n",
                   artifacts=art)
    (change,) = [c for c in changes if "old_sum" in c.affected_symbols]  # no R2 duplicate
    assert change.source == ChangeSource.API_DIFF and change.replacement_symbols == ["sum"]
    assert change.affected_usrs == ["c:@N@toydep@F@old_sum#I#I#"]
    assert any(u.endswith("notes.md") for u in change.evidence_urls)  # changelog evidence kept
    assert json.loads((art / "api_diff.json").read_text())["available"] is True


@needs_clang
def test_changelog_claim_contradicted_by_headers_needs_review(tmp_path):
    art = _r1_artifacts(tmp_path)
    changes = _run(tmp_path, "## 2.0.0\n\nThis release removes `toydep::keep`.\n", artifacts=art)
    keep = next(c for c in changes if c.affected_symbols == ["keep"])
    assert keep.source == ChangeSource.CHANGELOG_RULE and keep.confidence <= 0.5
    assert "Not confirmed by the header API diff" in keep.migration_advice


def test_without_compile_databases_r1_is_skipped_not_fatal(tmp_path):
    changes = _run(tmp_path, "## 2.0.0\n\nThis release removes `toydep::old_sum`. Use `toydep::sum`.\n")
    assert [c.source for c in changes] == [ChangeSource.CHANGELOG_RULE]
    assert "compile database(s) missing" in json.loads((tmp_path / "art" / "api_diff.json").read_text())["reason"]


def test_llm_sees_only_leftovers_and_adds_to_the_list(tmp_path):
    notes = ("## 2.0.0\n\nThis release removes `toydep::old_sum`. Use `toydep::sum`.\n\n"
             "Rounding of `toydep::avg` is now incompatible: it rounds half to even.\n")
    seen = {}

    class Capture(FixtureLLMClient):
        def complete_structured(self, *, task, system, user, schema):
            seen["user"] = user
            return super().complete_structured(task=task, system=system, user=user, schema=schema)

    client = Capture({"breaking_changes": {"changes": [{
        "title": "avg rounding changed", "change_type": "behavior", "affected_symbols": ["toydep::avg"],
        "migration_advice": "Check callers that depend on rounding.", "confidence": 0.7,
        "evidence_excerpt": "Rounding of `toydep::avg` is now incompatible: it rounds half to even."}]}})
    changes = _run(tmp_path, notes, client=client)
    assert "old_sum" not in seen["user"]  # explained by rules → not sent
    kinds = {(c.change_type, c.source) for c in changes}
    assert (ChangeType.REMOVED, ChangeSource.CHANGELOG_RULE) in kinds  # deterministic finding kept
    assert (ChangeType.BEHAVIOR, ChangeSource.LLM) in kinds  # LLM finding added
    assert not [c for c in changes if c.change_type == ChangeType.UNKNOWN]  # explained leftover dropped


def test_github_release_page_uses_api_markdown_body(tmp_path, monkeypatch):
    calls = []

    def fake_find(*, upstream_repo, target_version, **_):
        calls.append((upstream_repo, target_version))
        return GitHubRelease(tag_name="v2.0.0", name="2.0.0", published_at=None, prerelease=False,
                             html_url="https://github.com/acme/toydep/releases/tag/v2.0.0",
                             body="## 2.0.0\n\nThis release removes `toydep::old_sum`. Use `toydep::sum`.\n")
    monkeypatch.setattr(bca, "find_github_release", fake_find)
    monkeypatch.setattr(bca, "fetch_release_document", lambda **kw: pytest.fail("HTML page fetched"))
    event = ReleaseEvent(event_id="e", dependency_name="toydep", upstream_repo="acme/toydep",
                         previous_version="1.0.0", target_version="2.0.0",
                         source_url="https://github.com/acme/toydep/releases/tag/v2.0.0")
    dep = Dependency(name="toydep", current_version="1.0.0", target_version="2.0.0",
                     package_manager="conan", manifest_path="conanfile.txt")
    changes = bca.research_release(event=event, dependency=dep, artifacts_dir=tmp_path / "art")
    assert calls == [("acme/toydep", "2.0.0")] and changes[0].affected_symbols == ["old_sum"]
    assert changes[0].evidence_urls == ["https://github.com/acme/toydep/releases/tag/v2.0.0"]
