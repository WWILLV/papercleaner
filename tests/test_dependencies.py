from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from papercleaner.comments import clean_comments, decode_source, encode_source
from papercleaner.dependencies import analyze_dependencies, build_dependency_graph
from papercleaner.models import CommentOptions, TexFileResult
from papercleaner.workspace import scan_directory


def write_tree(root: Path, files: dict[str, str | bytes]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8", newline="")


def workspace_and_results(root: Path):
    workspace = scan_directory(root)
    results: dict[PurePosixPath, TexFileResult] = {}
    for relative in workspace.files:
        if relative.suffix.lower() != ".tex":
            continue
        raw = (root / Path(*relative.parts)).read_bytes()
        decoded = decode_source(raw)
        output = clean_comments(decoded.text)
        analysis = clean_comments(
            decoded.text, CommentOptions(remove_all=True, keep_ws=True)
        )
        results[relative] = TexFileResult(
            rel=relative,
            original_text=decoded.text,
            output_text=output.text,
            output_bytes=encode_source(decoded, output.text),
            analysis_text=analysis.text,
            comments_removed=output.comments_removed,
            magic_comments_kept=output.magic_comments_kept,
            magic_comments=output.magic_comments,
            warnings=output.warnings,
        )
    return workspace, results


def graph_for(root: Path):
    workspace, results = workspace_and_results(root)
    return build_dependency_graph(workspace, results)


def test_nested_inputs_and_multiple_roots(tmp_path: Path) -> None:
    write_tree(
        tmp_path,
        {
            "main.tex": (
                "\\documentclass{article}\n"
                "\\input{chapters/intro}\n"
                "\\include{chapters/appendix}\n"
                "\\subfile{chapters/sub}\n"
                "\\includegraphics{figures/used}\n"
            ),
            "chapters/intro.tex": "\\input{shared}\n",
            "chapters/appendix.tex": "appendix\n",
            "chapters/sub.tex": "\\documentclass[main.tex]{subfiles}\n",
            "shared.tex": "shared\n",
            "other.tex": "\\documentclass{article}\n\\input{other-body}\n",
            "other-body.tex": "body\n",
            "figures/used.png": b"png",
        },
    )
    graph = graph_for(tmp_path)
    assert PurePosixPath("main.tex") in graph.roots
    assert PurePosixPath("other.tex") in graph.roots
    assert PurePosixPath("chapters/intro.tex") in graph.reachable
    assert PurePosixPath("chapters/appendix.tex") in graph.reachable
    assert PurePosixPath("chapters/sub.tex") in graph.reachable
    assert PurePosixPath("shared.tex") in graph.reachable
    assert PurePosixPath("other-body.tex") in graph.reachable
    assert PurePosixPath("figures/used.png") in graph.reachable
    assert graph.issues == ()


def test_comments_and_verbatim_references_are_ignored(tmp_path: Path) -> None:
    write_tree(
        tmp_path,
        {
            "main.tex": (
                "\\documentclass{article}\n"
                "% \\input{gone-comment}\n"
                "\\begin{verbatim}\n\\input{gone-verbatim}\n\\end{verbatim}\n"
                "\\verb|\\input{gone-inline}|\n"
                "\\input{used}\n"
            ),
            "used.tex": "used\n",
        },
    )
    graph = graph_for(tmp_path)
    assert PurePosixPath("used.tex") in graph.reachable
    assert PurePosixPath("gone-comment.tex") not in graph.reachable
    assert PurePosixPath("gone-verbatim.tex") not in graph.reachable
    assert PurePosixPath("gone-inline.tex") not in graph.reachable
    assert graph.issues == ()


def test_missing_required_input_is_unsafe(tmp_path: Path) -> None:
    write_tree(
        tmp_path,
        {"main.tex": "\\documentclass{article}\n\\input{missing}\n"},
    )
    graph = graph_for(tmp_path)
    assert graph.issues
    assert any("unresolved reference" in issue for issue in graph.issues)


def test_graphicspath_and_declared_extensions_resolve(tmp_path: Path) -> None:
    write_tree(
        tmp_path,
        {
            "main.tex": (
                "\\documentclass{article}\n"
                "\\graphicspath{{figs/}}\n"
                "\\DeclareGraphicsExtensions{.png}\n"
                "\\includegraphics{plot}\n"
            ),
            "figs/plot.png": b"png",
        },
    )
    graph = graph_for(tmp_path)
    assert PurePosixPath("figs/plot.png") in graph.reachable
    assert graph.issues == ()


def test_multiple_graphics_candidates_are_all_reachable(tmp_path: Path) -> None:
    write_tree(
        tmp_path,
        {
            "main.tex": (
                "\\documentclass{article}\n"
                "\\includegraphics{figures/plot}\n"
            ),
            "figures/plot.pdf": b"pdf",
            "figures/plot.png": b"png",
        },
    )
    graph = graph_for(tmp_path)
    assert PurePosixPath("figures/plot.pdf") in graph.reachable
    assert PurePosixPath("figures/plot.png") in graph.reachable


def test_bibliography_and_style_references_are_reachable(
    tmp_path: Path,
) -> None:
    write_tree(
        tmp_path,
        {
            "main.tex": (
                "\\documentclass{article}\n"
                "\\bibliography{refs,extra}\n"
                "\\addbibresource{online}\n"
                "\\bibliographystyle{plain}\n"
            ),
            "refs.bib": "% refs\n",
            "extra.bib": "% extra\n",
            "online.bib": "% online\n",
            "plain.bst": "% bst\n",
        },
    )
    graph = graph_for(tmp_path)
    assert PurePosixPath("refs.bib") in graph.reachable
    assert PurePosixPath("extra.bib") in graph.reachable
    assert PurePosixPath("online.bib") in graph.reachable
    assert PurePosixPath("plain.bst") in graph.reachable
    assert graph.issues == ()


def test_local_style_and_class_are_parsed_recursively(tmp_path: Path) -> None:
    write_tree(
        tmp_path,
        {
            "main.tex": (
                "\\documentclass{localclass}\n"
                "\\usepackage{localstyle}\n"
                "\\input{body}\n"
            ),
            "localclass.cls": "\\LoadClass{article}\n",
            "localstyle.sty": "\\RequirePackage{inner}\n\\input{sty-body}\n",
            "inner.sty": "\\ProvidesPackage{inner}\n",
            "sty-body.tex": "body\n",
            "body.tex": "body\n",
        },
    )
    graph = graph_for(tmp_path)
    assert PurePosixPath("localclass.cls") in graph.reachable
    assert PurePosixPath("localstyle.sty") in graph.reachable
    assert PurePosixPath("inner.sty") in graph.reachable
    assert PurePosixPath("sty-body.tex") in graph.reachable


def test_undecodable_reachable_style_is_unsafe(tmp_path: Path) -> None:
    write_tree(
        tmp_path,
        {
            "main.tex": "\\documentclass{article}\n\\usepackage{badstyle}\n",
            "badstyle.sty": b"\xff\xfe" + "style".encode("utf-16-le"),
        },
    )
    graph = graph_for(tmp_path)
    assert graph.issues
    assert any("badstyle.sty" in issue for issue in graph.issues)


def test_dynamic_reference_is_unsafe(tmp_path: Path) -> None:
    write_tree(
        tmp_path,
        {"main.tex": "\\documentclass{article}\n\\input{\\chapdir/intro}\n"},
    )
    graph = graph_for(tmp_path)
    assert graph.issues
    assert any("dynamic" in issue for issue in graph.issues)


def test_out_of_workspace_reference_is_warning_not_unsafe(tmp_path: Path) -> None:
    source = tmp_path / "paper"
    source.mkdir()
    write_tree(
        source,
        {
            "main.tex": (
                "\\documentclass{article}\n"
                "\\includegraphics{../outside.png}\n"
            )
        },
    )
    (tmp_path / "outside.png").write_bytes(b"outside")
    graph = graph_for(source)
    assert graph.issues == ()
    assert any("outside the workspace" in warning for warning in graph.warnings)


def test_magic_root_is_relative_to_containing_file(tmp_path: Path) -> None:
    write_tree(
        tmp_path,
        {
            "main.tex": "\\documentclass{article}\n\\input{body}\n",
            "body.tex": "body\n",
            # This fragment only names main.tex as its root and has no
            # \documentclass. The source of a magic directive is not itself a
            # root, so sub/child-body.tex must not be reachable: main.tex
            # never inputs it, and sub/child.tex is not traversed.
            "sub/child.tex": (
                "% !TEX root = ../main.tex\n"
                "\\input{child-body}\n"
            ),
            "sub/child-body.tex": "child\n",
        },
    )
    graph = graph_for(tmp_path)
    assert PurePosixPath("main.tex") in graph.roots
    assert PurePosixPath("sub/child.tex") not in graph.roots
    assert PurePosixPath("sub/child-body.tex") not in graph.reachable


def test_magic_root_resolves_relative_to_containing_file(
    tmp_path: Path,
) -> None:
    write_tree(
        tmp_path,
        {
            # No \documentclass anywhere, so main.tex can only become a root
            # through the magic directive, resolved against sub/.
            "main.tex": "\\input{body}\n",
            "body.tex": "body\n",
            # If the directive were resolved against the workspace root, this
            # decoy would be picked instead of main.tex.
            "sub/child.tex": "% !TEX root = ../main.tex\nbody\n",
            "main.decoy.tex": "decoy\n",
        },
    )
    graph = graph_for(tmp_path)
    assert graph.roots == (PurePosixPath("main.tex"),)
    assert PurePosixPath("body.tex") in graph.reachable
    assert PurePosixPath("main.decoy.tex") not in graph.reachable
    assert graph.issues == ()
    assert graph.warnings == ()


def test_magic_looking_line_inside_verbatim_is_not_a_root(tmp_path: Path) -> None:
    write_tree(
        tmp_path,
        {
            "main.tex": "\\documentclass{article}\n",
            "fake.tex": (
                "\\begin{verbatim}\n"
                "% !TEX root = main.tex\n"
                "\\end{verbatim}\n"
            ),
        },
    )
    graph = graph_for(tmp_path)
    assert graph.roots == (PurePosixPath("main.tex"),)


def test_missing_magic_root_is_a_warning(tmp_path: Path) -> None:
    write_tree(
        tmp_path,
        {
            "main.tex": (
                "\\documentclass{article}\n"
                "% !TEX root = missing.tex\n"
            )
        },
    )
    graph = graph_for(tmp_path)
    assert graph.roots == (PurePosixPath("main.tex"),)
    assert any("magic root" in warning for warning in graph.warnings)


def test_all_roots_run_even_when_one_is_unsafe(tmp_path: Path) -> None:
    write_tree(
        tmp_path,
        {
            "bad.tex": "\\documentclass{article}\n\\input{missing}\n",
            "good.tex": "\\documentclass{article}\n\\input{good-body}\n",
            "good-body.tex": "good\n",
        },
    )
    graph = graph_for(tmp_path)
    assert graph.issues
    assert PurePosixPath("good-body.tex") in graph.reachable


def test_no_roots_returns_empty_graph(tmp_path: Path) -> None:
    write_tree(
        tmp_path,
        {"fragment.tex": "\\input{body}\n", "body.tex": "body\n"},
    )
    graph = graph_for(tmp_path)
    assert graph.roots == ()
    assert graph.reachable == frozenset()
    assert graph.issues == ()


def test_root_with_unclosed_raw_construct_is_unsafe(tmp_path: Path) -> None:
    write_tree(
        tmp_path,
        {"main.tex": "\\documentclass{article}\n\\begin{verbatim}\n%"},
    )
    graph = graph_for(tmp_path)
    assert graph.issues
    assert any("unclosed" in issue for issue in graph.issues)


@pytest.mark.parametrize(
    "magic_line",
    ["  % !TEX root = main.tex", "\t%!TeX root=main.tex"],
)
def test_indented_magic_root_is_recognized(
    tmp_path: Path, magic_line: str
) -> None:
    write_tree(
        tmp_path,
        {
            "main.tex": "\\documentclass{article}\n",
            "child.tex": f"{magic_line}\nbody\n",
        },
    )
    workspace, results = workspace_and_results(tmp_path)
    assert results[PurePosixPath("child.tex")].magic_comments == (
        (1, magic_line),
    )
    graph = build_dependency_graph(workspace, results)
    assert graph.roots == (PurePosixPath("main.tex"),)


def test_escaped_documentclass_is_not_a_root(tmp_path: Path) -> None:
    two_backslashes = "\\" * 2
    four_backslashes = "\\" * 4
    write_tree(
        tmp_path,
        {
            "even.tex": f"{two_backslashes}documentclass{{article}}\n",
            "even-even.tex": f"{four_backslashes}documentclass{{article}}\n",
            "real.tex": "\\documentclass{article}\n"
            f"{two_backslashes}input{{body}}\n",
            "body.tex": "body\n",
        },
    )
    graph = graph_for(tmp_path)
    assert graph.roots == (PurePosixPath("real.tex"),)
    assert graph.reachable == frozenset({PurePosixPath("real.tex")})
    assert graph.issues == ()


def plan_for(root: Path):
    workspace, results = workspace_and_results(root)
    return analyze_dependencies(workspace, results)


def test_acceptance_dependency_cleanup(tmp_path: Path) -> None:
    write_tree(
        tmp_path,
        {
            "main.tex": (
                "\\documentclass{article}\n"
                "\\input{chapters/intro}\n"
                "\\includegraphics{figures/used}\n"
            ),
            "chapters/intro.tex": "intro\n",
            "figures/used.pdf": b"used",
            "figures/unused.png": b"unused",
            "main.pdf": b"root-pdf",
            "README.md": "readme\n",
            "unused.tex": "unused\n",
        },
    )
    plan = plan_for(tmp_path)
    assert plan.skip_reasons == ()
    assert plan.deletions == (
        PurePosixPath("figures/unused.png"),
        PurePosixPath("unused.tex"),
    )
    assert PurePosixPath("main.pdf") not in plan.deletions
    assert PurePosixPath("README.md") not in plan.deletions


def test_non_candidates_and_protected_metadata_are_kept(tmp_path: Path) -> None:
    write_tree(
        tmp_path,
        {
            "main.tex": "\\documentclass{article}\n",
            "unused.aux": b"aux",
            "style.bbx": b"bbx",
            "unused.bib": "bib\n",
            "unused.sty": "sty\n",
            "README.pdf": b"pdf",
            "LICENSE.pdf": b"pdf",
            "Makefile": "all:\n",
        },
    )
    plan = plan_for(tmp_path)
    assert plan.deletions == (
        PurePosixPath("unused.bib"),
        PurePosixPath("unused.sty"),
    )


def test_hidden_files_and_directories_are_kept(tmp_path: Path) -> None:
    write_tree(
        tmp_path,
        {
            "main.tex": "\\documentclass{article}\n",
            ".secret.png": b"png",
            ".cache/plot.png": b"png",
            "unused.png": b"png",
        },
    )
    plan = plan_for(tmp_path)
    assert plan.deletions == (PurePosixPath("unused.png"),)


def test_missing_reference_skips_all_deletion(tmp_path: Path) -> None:
    write_tree(
        tmp_path,
        {
            "main.tex": (
                "\\documentclass{article}\n"
                "\\includegraphics{missing}\n"
            ),
            "unused.png": b"png",
        },
    )
    plan = plan_for(tmp_path)
    assert plan.deletions == ()
    assert plan.skip_reasons
    assert any("unresolved reference" in reason for reason in plan.skip_reasons)


def test_no_root_skips_all_deletion(tmp_path: Path) -> None:
    write_tree(
        tmp_path,
        {"orphan.tex": "orphan\n", "unused.png": b"png"},
    )
    plan = plan_for(tmp_path)
    assert plan.deletions == ()
    assert plan.skip_reasons == ("no root documents found",)


def test_multiple_roots_union_reachable_images(tmp_path: Path) -> None:
    write_tree(
        tmp_path,
        {
            "a.tex": "\\documentclass{article}\n\\includegraphics{a}\n",
            "b.tex": "\\documentclass{article}\n\\includegraphics{b}\n",
            "a.pdf": b"a",
            "b.pdf": b"b",
            "unused.png": b"unused",
        },
    )
    plan = plan_for(tmp_path)
    assert PurePosixPath("a.pdf") in plan.reachable
    assert PurePosixPath("b.pdf") in plan.reachable
    assert plan.deletions == (PurePosixPath("unused.png"),)


def test_deletions_are_sorted(tmp_path: Path) -> None:
    write_tree(
        tmp_path,
        {
            "main.tex": "\\documentclass{article}\n",
            "z.png": b"z",
            "a.png": b"a",
            "m.bib": "m\n",
        },
    )
    plan = plan_for(tmp_path)
    assert plan.deletions == (
        PurePosixPath("a.png"),
        PurePosixPath("m.bib"),
        PurePosixPath("z.png"),
    )


def test_root_pdf_is_protected_even_when_nothing_else_exists(
    tmp_path: Path,
) -> None:
    # The root PDF is protected by its own rule, not because it happens to be
    # reachable: the root references nothing and there are no other assets.
    write_tree(
        tmp_path,
        {
            "thesis.tex": "\\documentclass{article}\n",
            "thesis.pdf": b"root-pdf",
        },
    )
    plan = plan_for(tmp_path)
    assert plan.skip_reasons == ()
    assert plan.deletions == ()
    assert PurePosixPath("thesis.pdf") not in plan.reachable


def test_required_space_path_with_real_file_is_protected(tmp_path: Path) -> None:
    # A recognized required path with a space resolves to the real file, so
    # it is reachable and cleanup proceeds without an unresolved reference.
    write_tree(
        tmp_path,
        {
            "main.tex": (
                "\\documentclass{article}\n"
                "\\includepdf[pages=-]{paper v2.pdf}\n"
            ),
            "paper v2.pdf": b"pdf",
            "unused.png": b"unused",
        },
    )
    plan = plan_for(tmp_path)
    assert PurePosixPath("paper v2.pdf") in plan.reachable
    assert plan.skip_reasons == ()
    assert plan.deletions == (PurePosixPath("unused.png"),)


def test_required_svg_with_real_file_is_protected(tmp_path: Path) -> None:
    write_tree(
        tmp_path,
        {
            "main.tex": (
                "\\documentclass{article}\n"
                "\\includegraphics[width=1cm]{logo.svg}\n"
            ),
            "logo.svg": b"<svg/>",
            "unused.png": b"unused",
        },
    )
    plan = plan_for(tmp_path)
    assert PurePosixPath("logo.svg") in plan.reachable
    assert plan.skip_reasons == ()
    assert plan.deletions == (PurePosixPath("unused.png"),)


def test_macro_argument_alias_skips_all_deletion(tmp_path: Path) -> None:
    # \fi_0-style arguments are macro-generated. Each occurrence is a
    # dynamic-path issue, so the per-page PDFs are never deleted.
    write_tree(
        tmp_path,
        {
            "main.tex": (
                "\\documentclass{article}\n"
                "\\newcommand{\\fi}[1]{_#1=_}\n"
                + "".join(
                    f"\\includegraphics[page={page}]{{\\fi_{page}}}\n"
                    for page in range(12)
                )
            ),
            **{f"p{page}.pdf": b"pdf" for page in range(12)},
        },
    )
    plan = plan_for(tmp_path)
    assert plan.deletions == ()
    assert plan.reachable == frozenset({PurePosixPath("main.tex")})
    assert len(plan.skip_reasons) == 12
    assert all("dynamic path" in reason for reason in plan.skip_reasons)


def test_unclosed_optional_argument_skips_all_deletion(tmp_path: Path) -> None:
    # An optional group that never closes records an unsafe issue, so the
    # deletion stage stops even though the earlier inputs resolved.
    write_tree(
        tmp_path,
        {
            "main.tex": (
                "\\documentclass{article}\n"
                "\\input{frag}\n"
                "\\includegraphics[width=1cm{oops\n"
            ),
            "frag.tex": "fragment\n",
            "unused.png": b"unused",
        },
    )
    plan = plan_for(tmp_path)
    assert PurePosixPath("frag.tex") in plan.reachable
    assert plan.deletions == ()
    assert any(
        "unclosed optional argument" in reason for reason in plan.skip_reasons
    )
