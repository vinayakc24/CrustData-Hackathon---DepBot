from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from depbot.code_intelligence import clang_analyzer as analyzer
from depbot.code_intelligence import impact_analyzer as impact
from depbot.code_intelligence.ripgrep_indexer import UsageMatch
from depbot.llm.runtime import use_llm_client
from depbot.models import (
    AnalysisMethod,
    BreakingChange,
    ChangeType,
    CodeUsage,
    ImpactLevel,
    ResolutionLayer,
)


class _Kind:
    def __init__(self, name: str, declaration: bool = False):
        self.name = name
        self._declaration = declaration

    def is_declaration(self) -> bool:
        return self._declaration


class _Location:
    def __init__(self, filename: str | Path, line: int, column: int = 1):
        self.file = SimpleNamespace(name=str(filename))
        self.line = line
        self.column = column


class _Cursor:
    def __init__(
        self,
        kind,
        spelling: str,
        location: _Location,
        *,
        usr: str = "",
        referenced=None,
        children=(),
    ):
        self.kind = kind
        self.spelling = spelling
        self.location = location
        self._usr = usr
        self.referenced = referenced
        self._children = list(children)
        self.extent = None

    def get_usr(self):
        return self._usr

    def get_children(self):
        return iter(self._children)

    def walk_preorder(self):
        yield self
        for child in self._children:
            yield from child.walk_preorder()


class _Token:
    def __init__(self, kind, spelling: str, location: _Location):
        self.kind = kind
        self.spelling = spelling
        self.location = location


def _usage(root: Path, line: int, snippet: str = "old_sum();") -> CodeUsage:
    return CodeUsage(
        symbol="old_sum",
        file_path="src/main.cpp",
        line_start=line,
        line_end=line,
        snippet=snippet,
        analysis_method=AnalysisMethod.RIPGREP,
        impact=ImpactLevel.POSSIBLE,
        related_change_ids=["toydep-2.0.0-API1"],
        confidence=0.5,
        resolved_by=ResolutionLayer.TEXT_PREFILTER,
    )


def _compile_db(root: Path) -> Path:
    source = root / "src" / "main.cpp"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("old_sum();\nold_sum();\n// old_sum\nint old_sum();\nold_sum();\n", encoding="utf-8")
    path = root.parent / "compile_commands.baseline.json"
    path.write_text(json.dumps([{
        "directory": str(root),
        "file": str(source),
        "arguments": ["clang++", "-I", "include", "-c", str(source), "-o", "main.o"],
    }]), encoding="utf-8")
    return path


def _fake_clang(source: Path):
    cursor_kind = SimpleNamespace(
        DECL_REF_EXPR=_Kind("DECL_REF_EXPR"),
        MEMBER_REF_EXPR=_Kind("MEMBER_REF_EXPR"),
        OVERLOADED_DECL_REF=_Kind("OVERLOADED_DECL_REF"),
        TYPE_REF=_Kind("TYPE_REF"),
        TEMPLATE_REF=_Kind("TEMPLATE_REF"),
        USING_DECLARATION=_Kind("USING_DECLARATION"),
        MACRO_DEFINITION=_Kind("MACRO_DEFINITION"),
        MACRO_INSTANTIATION=_Kind("MACRO_INSTANTIATION"),
        FUNCTION_DECL=_Kind("FUNCTION_DECL", declaration=True),
    )
    token_kind = SimpleNamespace(
        COMMENT=_Kind("COMMENT"),
        LITERAL=_Kind("LITERAL"),
        IDENTIFIER=_Kind("IDENTIFIER"),
    )
    declaration = SimpleNamespace(
        get_usr=lambda: "c:@N@toydep@F@old_sum#I#I#",
        location=_Location("C:/deps/toydep.hpp", 2),
    )
    nodes = [
        _Cursor(cursor_kind.DECL_REF_EXPR, "old_sum", _Location(source, 1), referenced=declaration),
        _Cursor(cursor_kind.DECL_REF_EXPR, "old_sum", _Location(source, 2), referenced=SimpleNamespace(
            get_usr=lambda: "c:@N@application@F@old_sum#I#I#",
            location=_Location(source, 4),
        )),
        _Cursor(cursor_kind.FUNCTION_DECL, "old_sum", _Location(source, 4), usr="c:@N@application@F@old_sum#I#I#"),
    ]
    root_cursor = _Cursor(_Kind("TRANSLATION_UNIT"), "", _Location(source, 1), children=nodes)
    tokens = [
        _Token(token_kind.IDENTIFIER, "old_sum", _Location(source, 1)),
        _Token(token_kind.IDENTIFIER, "old_sum", _Location(source, 2)),
        _Token(token_kind.COMMENT, "// old_sum", _Location(source, 3)),
        _Token(token_kind.IDENTIFIER, "old_sum", _Location(source, 4)),
        _Token(token_kind.IDENTIFIER, "old_sum", _Location(source, 5)),
    ]
    tu = SimpleNamespace(cursor=root_cursor, get_tokens=lambda extent: tokens)

    class Index:
        @classmethod
        def create(cls):
            return cls()

        def parse(self, filename, *, args, options):
            assert "-I" in args and str((source.parent.parent / "include").resolve()) in args
            return tu

    return SimpleNamespace(
        CursorKind=cursor_kind,
        TokenKind=token_kind,
        TranslationUnit=SimpleNamespace(PARSE_DETAILED_PROCESSING_RECORD=1),
        Index=Index,
    )


def test_ast_classifies_exact_usrs_and_rules_out_comments_and_declarations(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repo"
    compile_db = _compile_db(root)
    monkeypatch.setattr(analyzer, "load_cindex", lambda: _fake_clang(root / "src" / "main.cpp"))
    change = BreakingChange(
        id="toydep-2.0.0-API1",
        title="old_sum removed",
        change_type=ChangeType.REMOVED,
        affected_symbols=["old_sum"],
        affected_usrs=["c:@N@toydep@F@old_sum#I#I#"],
        migration_advice="Removed",
        evidence_urls=["file:///toydep.hpp"],
        confidence=0.99,
    )

    result = analyzer.resolve_usages(
        worktree=root,
        usages=[_usage(root, line) for line in range(1, 6)],
        changes=[change],
        compile_commands_path=compile_db,
    )

    assert [item.impact for item in result] == [
        ImpactLevel.DEFINITE,
        ImpactLevel.UNAFFECTED,
        ImpactLevel.UNAFFECTED,
        ImpactLevel.UNAFFECTED,
        ImpactLevel.POSSIBLE,
    ]
    assert result[0].analysis_method == AnalysisMethod.CLANG_AST
    assert result[0].resolved_by == ResolutionLayer.CLANG_AST
    assert result[0].resolved_usr == "c:@N@toydep@F@old_sum#I#I#"
    assert result[0].declaration_file == str(Path("C:/deps/toydep.hpp").resolve())
    assert "comment" in result[2].resolution_proof


def test_missing_compile_database_keeps_text_fallback(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    usage = _usage(root, 1)
    assert analyzer.resolve_usages(
        worktree=root,
        usages=[usage],
        changes=[],
        compile_commands_path=tmp_path / "missing.json",
    ) == [usage]


def test_preprocessor_context_is_attached_to_possible_ast_residual(tmp_path: Path) -> None:
    source = tmp_path / "main.cpp"
    source.write_text("#ifdef _WIN32\nold_sum();\n#endif\n", encoding="utf-8")
    assert analyzer._preprocessor_context(source, 2) == "#ifdef _WIN32"


def test_impact_stage_uses_ast_before_optional_llm(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repo"
    compile_db = _compile_db(root)
    source = root / "src" / "main.cpp"
    monkeypatch.setattr(analyzer, "load_cindex", lambda: _fake_clang(source))
    monkeypatch.setattr(impact, "search_symbols", lambda worktree, candidates: [
        UsageMatch("old_sum", "src/main.cpp", 1, "old_sum();")
    ])
    change = BreakingChange(
        id="toydep-2.0.0-API1",
        title="old_sum removed",
        change_type=ChangeType.REMOVED,
        affected_symbols=["old_sum"],
        affected_usrs=["c:@N@toydep@F@old_sum#I#I#"],
        migration_advice="Removed",
        evidence_urls=["file:///toydep.hpp"],
        confidence=0.99,
    )

    with use_llm_client(None):
        result = impact.find_impacted_usages(
            worktree=root, changes=[change], artifacts_dir=compile_db.parent
        )

    assert len(result) == 1
    assert result[0].impact == ImpactLevel.DEFINITE
    assert result[0].resolved_by == ResolutionLayer.CLANG_AST
