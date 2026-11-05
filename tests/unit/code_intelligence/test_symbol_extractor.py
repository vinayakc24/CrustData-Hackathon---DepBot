from __future__ import annotations

from depbot.code_intelligence.ripgrep_indexer import search_symbols
from depbot.code_intelligence.symbol_extractor import SymbolCandidate, extract_symbols
from depbot.models import BreakingChange, ChangeType


def _change(change_id: str, affected: list[str], replacements: list[str] | None = None) -> BreakingChange:
    return BreakingChange(
        id=change_id,
        title="API change",
        change_type=ChangeType.UNKNOWN,
        affected_symbols=affected,
        replacement_symbols=replacements or [],
        migration_advice="unknown",
        evidence_urls=[],
        confidence=0.5,
    )


def test_extracts_multiple_symbols_and_excludes_replacements() -> None:
    result = extract_symbols([_change("bc1", ["old_sum", "Legacy::Type"], ["sum"])])
    assert result == [
        SymbolCandidate("old_sum", ("bc1",)),
        SymbolCandidate("Legacy::Type", ("bc1",)),
    ]


def test_deduplicates_symbols_and_maps_all_unique_change_ids_in_order() -> None:
    result = extract_symbols([
        _change("bc1", ["old_sum", "old_sum", "other"]),
        _change("bc2", ["other", "old_sum"]),
        _change("bc1", ["old_sum"]),
    ])
    assert result == [
        SymbolCandidate("old_sum", ("bc1", "bc2")),
        SymbolCandidate("other", ("bc1", "bc2")),
    ]


def test_preserves_namespaces_case_and_exact_spelling() -> None:
    names = ["Net::HTTPClient", "net::HTTPClient", "Net::httpClient"]
    assert [item.symbol for item in extract_symbols([_change("bc", names)])] == names


def test_ignores_empty_and_whitespace_only_symbols() -> None:
    assert extract_symbols([_change("bc", ["", "  ", "\t\n"])]) == []


def test_empty_input_and_changes_without_affected_symbols_return_empty() -> None:
    assert extract_symbols([]) == []
    assert extract_symbols([_change("bc", [], ["replacement"])]) == []


def test_candidates_pass_to_existing_indexer_api_without_conversion(tmp_path) -> None:
    (tmp_path / "use.cpp").write_text("old_sum();\n", encoding="utf-8")
    candidates = extract_symbols([_change("bc", ["old_sum"])])
    results = search_symbols(tmp_path, candidates)
    assert [(match.symbol, match.line_number) for match in results] == [("old_sum", 1)]
