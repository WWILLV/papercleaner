from __future__ import annotations

import sys

import pytest

from papercleaner.commands import parse_source
from papercleaner.models import RefKind


def references(source: str):
    return parse_source(source).references


@pytest.mark.parametrize(
    ("source", "command", "kind", "argument", "required"),
    [
        (r"\input{chapters/intro}", "input", RefKind.TEX, "chapters/intro", True),
        (r"\include{chapters/intro}", "include", RefKind.TEX, "chapters/intro", True),
        (r"\subfile{chapters/intro}", "subfile", RefKind.TEX, "chapters/intro", True),
        (
            r"\includegraphics*[width=\textwidth]{figures/plot}",
            "includegraphics",
            RefKind.GRAPHIC,
            "figures/plot",
            True,
        ),
        (r"\includesvg[inkscapelatex=false]{figures/plot}", "includesvg",
         RefKind.SVG, "figures/plot", True),
        (r"\includepdf[pages=-]{appendix.pdf}", "includepdf",
         RefKind.PDF, "appendix.pdf", True),
        (r"\bibliography{refs}", "bibliography", RefKind.BIB, "refs", True),
        (r"\addbibresource{refs.bib}", "addbibresource", RefKind.BIB, "refs.bib", True),
        (r"\bibliographystyle{plain}", "bibliographystyle",
         RefKind.BIBSTYLE, "plain", False),
        (r"\usepackage[utf8]{local}", "usepackage", RefKind.STY, "local", False),
        (r"\RequirePackage{local}", "RequirePackage", RefKind.STY, "local", False),
        (r"\documentclass[11pt]{article}", "documentclass",
         RefKind.CLS, "article", False),
        (r"\lstinputlisting[language=Python]{code.py}", "lstinputlisting",
         RefKind.EXACT, "code.py", True),
        (r"\verbatiminput{raw.txt}", "verbatiminput",
         RefKind.EXACT, "raw.txt", True),
        (r"\VerbatimInput{raw.txt}", "VerbatimInput",
         RefKind.EXACT, "raw.txt", True),
        (r"\BVerbatimInput{raw.txt}", "BVerbatimInput",
         RefKind.EXACT, "raw.txt", True),
        (r"\LVerbatimInput{raw.txt}", "LVerbatimInput",
         RefKind.EXACT, "raw.txt", True),
    ],
)
def test_supported_single_file_commands(
    source, command, kind, argument, required
) -> None:
    parsed = parse_source(source)
    assert parsed.issues == ()
    assert len(parsed.references) == 1
    reference = parsed.references[0]
    assert reference.command == command
    assert reference.kind == kind
    assert reference.argument == argument
    assert reference.required is required


@pytest.mark.parametrize(
    "command",
    ["import", "subimport", "includefrom", "subincludefrom", "inputfrom", "subinputfrom"],
)
def test_import_family_combines_two_path_arguments(command: str) -> None:
    parsed = parse_source(f"\\{command}{{chapters/}}{{intro}}")
    assert parsed.issues == ()
    assert parsed.references[0].kind == RefKind.TEX
    assert parsed.references[0].argument == "chapters/intro"


def test_inputminted_uses_second_path_argument() -> None:
    parsed = parse_source(r"\inputminted[linenos]{python}{snippets/demo.py}")
    assert parsed.issues == ()
    assert parsed.references[0].command == "inputminted"
    assert parsed.references[0].kind == RefKind.EXACT
    assert parsed.references[0].argument == "snippets/demo.py"


def test_comma_separated_references_are_split() -> None:
    parsed = parse_source(r"\bibliography{refs,extra}\usepackage{alpha, beta}")
    assert [(ref.kind, ref.argument) for ref in parsed.references] == [
        (RefKind.BIB, "refs"),
        (RefKind.BIB, "extra"),
        (RefKind.STY, "alpha"),
        (RefKind.STY, "beta"),
    ]


def test_documentclass_marks_a_root() -> None:
    assert parse_source(r"\documentclass{subfiles}").has_documentclass is True
    assert parse_source(r"\input{not-a-root}").has_documentclass is False


def test_graphics_configuration_is_collected() -> None:
    parsed = parse_source(
        r"\graphicspath{{figures/}{images/}}"
        r"\DeclareGraphicsExtensions{.pdf,.png}"
    )
    assert parsed.graphics_dirs == ("figures/", "images/")
    assert parsed.graphics_extensions == (".pdf", ".png")
    assert parsed.issues == ()


def test_subfix_static_wrapper_is_unwrapped() -> None:
    parsed = parse_source(r"\includegraphics{\subfix{figures/plot.pdf}}")
    assert parsed.references[0].argument == "figures/plot.pdf"
    assert parsed.issues == ()


def test_dynamic_path_is_an_issue() -> None:
    parsed = parse_source(r"\input{\chapterdir/intro}")
    assert parsed.issues
    assert "dynamic" in parsed.issues[0].message
    assert parsed.references == ()


def test_parameter_placeholder_is_an_issue() -> None:
    parsed = parse_source(r"\includegraphics{fig-#1}")
    assert parsed.issues
    assert "dynamic" in parsed.issues[0].message


def test_missing_required_argument_is_an_issue() -> None:
    parsed = parse_source(r"\input")
    assert parsed.issues
    assert "missing" in parsed.issues[0].message


def test_optional_formatting_macro_is_not_a_dynamic_path_issue() -> None:
    parsed = parse_source(r"\includegraphics[width=\textwidth]{fig}")
    assert parsed.issues == ()
    assert parsed.references[0].argument == "fig"


def test_raw_constructs_are_ignored() -> None:
    source = (
        "\\begin{verbatim}\n\\input{hidden-one}\n\\end{verbatim}\n"
        "\\verb|\\input{hidden-two}|\n"
        "\\url{https://example.test/a%b}\n"
        "\\input{visible}\n"
    )
    parsed = parse_source(source)
    assert [ref.argument for ref in parsed.references] == ["visible"]
    assert parsed.issues == ()


def test_unclosed_raw_construct_is_an_issue() -> None:
    parsed = parse_source("\\begin{verbatim}\n\\input{hidden}")
    assert parsed.references == ()
    assert parsed.issues
    assert "unclosed" in parsed.issues[0].message


def test_remote_addbibresource_is_ignored() -> None:
    parsed = parse_source(r"\addbibresource{https://example.test/refs.bib}")
    assert parsed.references == ()
    assert parsed.issues == ()


def test_empty_graphics_configuration_is_an_issue() -> None:
    assert parse_source(r"\graphicspath{}").issues
    assert parse_source(r"\DeclareGraphicsExtensions{}").issues


def test_deeply_nested_subfix_is_a_dynamic_path_issue() -> None:
    depth = 1000
    source = (
        r"\includegraphics{"
        + r"\subfix{" * depth
        + "figures/plot.pdf"
        + "}" * depth
        + "}"
    )
    parsed = parse_source(source)
    assert parsed.references == ()
    assert parsed.issues
    assert "dynamic" in parsed.issues[0].message


def _count_balanced_group_scans(source: str) -> int:
    """Parse ``source`` and count calls to ``comments.find_matching_group``.

    Counting through a tracing hook makes the assertion independent of
    wall-clock speed.  The hook returns ``None`` for every other call so it
    is not active inside unrelated code, and it restores the previous hook.
    """
    scans = 0

    def tracer(frame, event, arg):
        nonlocal scans
        if (
            frame.f_code.co_qualname == "find_matching_group"
            and frame.f_code.co_filename.endswith("comments.py")
        ):
            scans += 1
        return None

    previous = sys.gettrace()
    sys.settrace(tracer)
    try:
        parse_source(source)
    finally:
        sys.settrace(previous)
    return scans


def test_repeated_unclosed_required_groups_are_not_rescanned() -> None:
    small = r"\input{" * 1000
    large = r"\input{" * 2000

    # The first failed group scan reaches end of input; the parser must stop
    # instead of rescanning the remainder for every repeated construct.
    small_parsed = parse_source(small)
    assert small_parsed.references == ()
    assert len(small_parsed.issues) == 1
    assert "unclosed required argument" in small_parsed.issues[0].message
    large_parsed = parse_source(large)
    assert large_parsed.references == ()
    assert len(large_parsed.issues) == 1

    # Exactly one group is scanned, whatever the number of constructs.
    assert _count_balanced_group_scans(small) == 1
    assert _count_balanced_group_scans(large) == 1


def _count_all_python_lines(source: str) -> int:
    r"""Parse ``source`` and count every Python line event executed meanwhile.

    All Python line events are counted, not just ``parse_source``'s own, so
    work done by callees such as ``comments.line_number`` is included.  The
    ``\includegraphics`` command is not a raw construct, so every input line
    reaches exactly one handled command and the parser's own loop work grows
    linearly with the number of commands.  Before the I5 fix each command
    rescanned the whole text from offset zero, so the count grew
    quadratically.  A ratio, rather than a wall-clock threshold, keeps the
    test deterministic on slow machines.
    """
    executed = 0

    def tracer(frame, event, arg):
        nonlocal executed
        if event == "line":
            executed += 1
        return tracer

    previous = sys.gettrace()
    sys.settrace(tracer)
    try:
        parse_source(source)
    finally:
        sys.settrace(previous)
    return executed


def test_well_formed_commands_scale_linearly_with_input() -> None:
    def source(count: int) -> str:
        return "".join(
            f"\\includegraphics[width={{0.5\\textwidth}}]{{fig{index}}}\n"
            for index in range(count)
        )

    small = _count_all_python_lines(source(200))
    large = _count_all_python_lines(source(500))
    # Fixed per-command cost makes the ratio approach 2.5 as inputs grow;
    # the old quadratic rescan measured about 3.5 here (and would keep
    # climbing with input size).  The 3.0 bound leaves margin for constant
    # setup cost while still failing on a quadratic line lookup.
    assert large < small * 3


def test_macro_argument_alias_is_fail_closed() -> None:
    # \fi_0-style arguments are macro-generated, not static paths: each one
    # must be reported as a dynamic path so dependency cleanup is skipped.
    source = (
        "\\newcommand{\\fi}[1]{_#1=_}\n"
        + "".join(
            f"\\includegraphics[page={page}]{{{{\\fi_{page}}}}}\n"
            for page in range(12)
        )
    )
    parsed = parse_source(source)
    assert parsed.references == ()
    assert len(parsed.issues) == 12
    assert all("dynamic path" in issue.message for issue in parsed.issues)
    assert [issue.line for issue in parsed.issues] == list(range(2, 14))


def test_unclosed_optional_stops_scan_and_records_an_issue() -> None:
    # The optional group belongs to a handled command and never closes. The
    # scan stops after the failure (pinned I4 behavior), the references found
    # so far are kept, and the source is marked unsafe so dependency cleanup
    # is skipped instead of deleting files it could not analyze.
    source = (
        "".join(f"\\input{{note{index}}}\n" for index in range(12))
        + "\\includegraphics[width=1cm{oops\n"
        + "\\includegraphics{after-the-failure}\n"
    )
    parsed = parse_source(source)
    assert [reference.argument for reference in parsed.references] == [
        f"note{index}" for index in range(12)
    ]
    assert len(parsed.issues) == 1
    assert parsed.issues[0].line == 13
    assert "unclosed optional argument" in parsed.issues[0].message


def test_stray_bracket_in_text_is_not_an_issue() -> None:
    # A "[" that follows no command is ordinary text, not an argument, so it
    # is neither unsafe nor a reason to stop scanning later commands.
    source = (
        "".join(f"\\input{{note{index}}}\n" for index in range(12))
        + "[unclosed optional]\n"
        + "\\input{after-the-bracket}\n"
    )
    parsed = parse_source(source)
    assert parsed.issues == ()
    assert [reference.argument for reference in parsed.references] == [
        *(f"note{index}" for index in range(12)),
        "after-the-bracket",
    ]
