from pathlib import Path

import pytest

from papercleaner.models import (
    CommentResult,
    InputError,
    OutputError,
    PaperCleanerError,
    ProcessingError,
    RunOptions,
    UsageError,
)


@pytest.mark.parametrize(
    ("error_type", "exit_code"),
    [
        (ProcessingError, 1),
        (UsageError, 2),
        (InputError, 2),
        (OutputError, 2),
    ],
)
def test_expected_exception_exit_codes(
    error_type: type[PaperCleanerError], exit_code: int
) -> None:
    error = error_type("message")
    assert error.exit_code == exit_code
    assert isinstance(error, PaperCleanerError)
    assert str(error) == "message"


def test_comment_result_defaults() -> None:
    result = CommentResult(text="body", comments_removed=1, magic_comments_kept=0)
    assert result.magic_comments == ()
    assert result.warnings == ()


def test_run_options_require_explicit_switches(tmp_path: Path) -> None:
    input_path = tmp_path / "paper"
    output_path = tmp_path / "paper-cleaned"
    options = RunOptions(
        input_path=input_path,
        output_path=output_path,
        deps=True,
        all_comments=False,
        keep_eol=True,
        keep_ws=True,
    )
    assert options.input_path == input_path
    assert options.output_path == output_path
    assert options.deps is True
    assert options.all_comments is False
    assert options.keep_eol is True
    assert options.keep_ws is True
