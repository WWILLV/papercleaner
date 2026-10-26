from __future__ import annotations

import pytest

from papercleaner.comments import (
    LineIndex,
    clean_comments,
    decode_source,
    encode_source,
    find_raw_construct,
    is_control_sequence_start,
    line_number,
)
from papercleaner.models import CommentOptions, InputError


def test_line_index_matches_public_line_number() -> None:
    # Mixed "\n", "\r\n" and lone "\r" breaks are counted the same way.
    # Offsets between the "\r" and "\n" of a "\r\n" pair are excluded: the
    # public helper counts them as a new line, and the parser never looks up
    # that offset because every command has consumed past the break.
    text = "a\r\nb\nc\rd\ref\n"
    for index in (0, 1, 3, 4, 5, 6, 7, 8, 9, 10, 11):
        assert LineIndex(text).line_number(index) == line_number(text, index)


def test_public_line_number_values_are_unchanged() -> None:
    text = "first\r\nsecond\nthird\rfourth"
    assert [line_number(text, index) for index in (0, 5, 7, 14, 16)] == [
        1,
        1,
        2,
        3,
        3,
    ]


def test_utf8_round_trip() -> None:
    raw = "hello\r\n世界\n".encode("utf-8")
    decoded = decode_source(raw)
    assert decoded.encoding == "utf-8"
    assert decoded.bom is False
    assert encode_source(decoded, decoded.text) == raw


def test_utf8_bom_round_trip_preserves_bom() -> None:
    raw = b"\xef\xbb\xbfhello\r\n"
    decoded = decode_source(raw)
    assert decoded.encoding == "utf-8"
    assert decoded.bom is True
    assert decoded.text == "hello\r\n"
    assert encode_source(decoded, decoded.text) == raw


def test_gb18030_round_trip() -> None:
    raw = "中文论文\r\n".encode("gb18030")
    decoded = decode_source(raw)
    assert decoded.encoding == "gb18030"
    assert decoded.bom is False
    assert encode_source(decoded, decoded.text) == raw


@pytest.mark.parametrize(
    "raw",
    [
        b"\xff\xfe" + "text".encode("utf-16-le"),
        b"\xfe\xff" + "text".encode("utf-16-be"),
        b"\xff\xfe\x00\x00" + "text".encode("utf-32-le"),
        b"\x00\x00\xfe\xff" + "text".encode("utf-32-be"),
    ],
)
def test_utf16_and_utf32_boms_are_rejected(raw: bytes) -> None:
    with pytest.raises(InputError):
        decode_source(raw)


def test_non_utf8_non_gb18030_text_is_rejected() -> None:
    with pytest.raises(InputError):
        decode_source("café".encode("latin-1"))


def test_bom_with_invalid_utf8_is_rejected() -> None:
    with pytest.raises(InputError):
        decode_source(b"\xef\xbb\xbf\xff")


def test_empty_file_is_valid_utf8() -> None:
    decoded = decode_source(b"")
    assert decoded.text == ""
    assert decoded.encoding == "utf-8"
    assert encode_source(decoded, "") == b""


@pytest.mark.parametrize(
    "source",
    [
        "\\begin{verbatim}\n% raw\n\\end{verbatim}",
        "\\begin{Verbatim*}[fontsize=\\small]\n% raw\n\\end{Verbatim*}",
        "\\begin{lstlisting}\n% raw\n\\end{lstlisting}",
        "\\begin{minted}{python}\n% raw\n\\end{minted}",
        "\\begin{filecontents*}{x.bib}\n% raw\n\\end{filecontents*}",
        "\\begin{alltt}\n% raw\n\\end{alltt}",
        "\\begin{comment}\n% raw\n\\end{comment}",
    ],
)
def test_environment_raw_regions_are_found(source: str) -> None:
    raw = find_raw_construct(source, 0)
    assert raw is not None
    assert raw.closed is True
    assert source[raw.start : raw.end].startswith("\\begin")
    assert source[raw.start : raw.end].endswith(
        (
            "\\end{verbatim}",
            "\\end{Verbatim*}",
            "\\end{lstlisting}",
            "\\end{minted}",
            "\\end{filecontents*}",
            "\\end{alltt}",
            "\\end{comment}",
        )
    )


@pytest.mark.parametrize(
    "source",
    [
        "\\verb|%|",
        "\\verb*+%+",
        "\\Verb[formatcom=\\ttfamily]{%}",
        "\\lstinline[language=TeX]|%|",
        "\\mintinline{python}|%|",
        "\\mintinline[breaklines]{python}{%}",
        "\\url{https://example.test/a%b}",
        "\\path{/tmp/a%b}",
        "\\nolinkurl{a%b}",
    ],
)
def test_inline_raw_constructs_are_found(source: str) -> None:
    raw = find_raw_construct(source, 0)
    assert raw is not None
    assert raw.closed is True
    assert raw.end == len(source)


def test_unclosed_environment_runs_to_eof() -> None:
    source = "\\begin{verbatim}\n% kept"
    raw = find_raw_construct(source, 0)
    assert raw is not None
    assert raw.closed is False
    assert raw.end == len(source)


def test_unclosed_inline_raw_stops_at_end_of_line() -> None:
    source = "\\verb|unterminated\n% later"
    raw = find_raw_construct(source, 0)
    assert raw is not None
    assert raw.closed is False
    assert raw.end == source.index("\n")


def test_double_backslash_does_not_start_a_raw_environment() -> None:
    source = "\\\\begin{verbatim}%"
    assert is_control_sequence_start(source, 0) is True
    assert is_control_sequence_start(source, 1) is False
    assert find_raw_construct(source, 1) is None


def test_verbatiminput_is_not_mistaken_for_verb() -> None:
    source = "\\verbatiminput{file}"
    assert find_raw_construct(source, 0) is None


def test_default_removes_whole_line_and_eol_comments() -> None:
    result = clean_comments("a\n  % whole\nb % eol\nc")
    assert result.text == "a\nb\nc"
    assert result.comments_removed == 2


def test_whole_line_comment_leaves_no_blank_line() -> None:
    assert clean_comments("a\n  % whole\nb\n").text == "a\nb\n"


def test_eol_comment_removes_exposed_trailing_whitespace() -> None:
    result = clean_comments("text   % note\r\nnext\r\n")
    assert result.text == "text\r\nnext\r\n"


def test_keep_ws_preserves_only_exposed_trailing_whitespace() -> None:
    result = clean_comments(
        "a   \nb   % note\n", CommentOptions(keep_ws=True)
    )
    assert result.text == "a   \nb   \n"


def test_keep_eol_preserves_eol_but_not_whole_line_comments() -> None:
    result = clean_comments(
        "a % kept\n% removed\nb\n", CommentOptions(keep_eol=True)
    )
    assert result.text == "a % kept\nb\n"
    assert result.comments_removed == 1


def test_keep_eol_and_keep_ws_leave_eol_text_untouched() -> None:
    source = "a   % kept\n"
    assert clean_comments(
        source, CommentOptions(keep_eol=True, keep_ws=False)
    ).text == source
    assert clean_comments(
        source, CommentOptions(keep_eol=True, keep_ws=True)
    ).text == source


def test_magic_comment_is_preserved_and_recorded() -> None:
    source = "% !TEX root = main.tex\n% ordinary\nx % eol\n"
    result = clean_comments(source)
    assert result.text == "% !TEX root = main.tex\nx\n"
    assert result.magic_comments_kept == 1
    assert result.comments_removed == 2
    assert result.magic_comments == ((1, "% !TEX root = main.tex"),)


def test_remove_all_deletes_magic_but_records_it() -> None:
    source = "% arara: pdflatex\nx\n"
    result = clean_comments(source, CommentOptions(remove_all=True))
    assert result.text == "x\n"
    assert result.magic_comments_kept == 0
    assert result.comments_removed == 1
    assert result.magic_comments == ((1, "% arara: pdflatex"),)


def test_magic_prefix_is_case_insensitive() -> None:
    result = clean_comments("% !bib program = biber\n")
    assert result.text == "% !bib program = biber\n"
    assert result.magic_comments_kept == 1


def test_crlf_lf_and_missing_final_newline() -> None:
    assert clean_comments("a\r\n% c\r\nb").text == "a\r\nb"
    assert clean_comments("a\n% c\nb").text == "a\nb"
    assert clean_comments("a\n% c").text == "a\n"
    assert clean_comments("a   % c").text == "a"


def test_escaped_percent_rules() -> None:
    assert clean_comments("a \\% b\n").text == "a \\% b\n"
    assert clean_comments("a \\\\% c\n").text == "a \\\\\n"
    assert clean_comments("a \\\\\\% b\n").text == "a \\\\\\% b\n"


def test_raw_constructs_protect_percent_signs() -> None:
    source = (
        "\\begin{verbatim}\n% raw\n\\end{verbatim}\n"
        "\\verb|%| % real\n"
        "\\url{https://example.test/a%b}\n"
    )
    result = clean_comments(source)
    assert "% raw" in result.text
    assert "\\verb|%|" in result.text
    assert "\\url{https://example.test/a%b}" in result.text
    assert "% real" not in result.text
    assert result.comments_removed == 1


def test_unclosed_raw_environment_is_preserved_with_warning() -> None:
    source = "a\n\\begin{verbatim}\n% raw\n% more"
    result = clean_comments(source)
    assert result.text == source
    assert any("unclosed raw environment" in warning for warning in result.warnings)


def test_unclosed_inline_raw_is_preserved_with_warning() -> None:
    source = "\\verb|unterminated % raw\nafter % removed\n"
    result = clean_comments(source)
    assert "unterminated % raw" in result.text
    assert "after\n" in result.text
    assert any("unclosed" in warning for warning in result.warnings)


def test_no_comment_file_is_byte_identical_in_utf8_and_bom() -> None:
    for raw in (
        "no comments\r\n世界\n".encode("utf-8"),
        b"\xef\xbb\xbfno comments\r\n",
    ):
        decoded = decode_source(raw)
        cleaned = clean_comments(decoded.text)
        assert cleaned.text == decoded.text
        assert encode_source(decoded, cleaned.text) == raw


def test_gb18030_comment_cleaning_is_byte_faithful() -> None:
    source = "中文 % 注释\r\n内容\r\n"
    raw = source.encode("gb18030")
    decoded = decode_source(raw)
    cleaned = clean_comments(decoded.text)
    assert cleaned.text == "中文\r\n内容\r\n"
    assert encode_source(decoded, cleaned.text) == cleaned.text.encode("gb18030")


def test_cleaning_is_idempotent() -> None:
    source = "a % eol\n% whole\nb\n"
    once = clean_comments(source).text
    twice = clean_comments(once).text
    assert twice == once
    assert clean_comments(once).comments_removed == 0
