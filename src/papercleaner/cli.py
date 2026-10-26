"""Command-line interface for papercleaner."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from papercleaner import __version__
from papercleaner.models import PaperCleanerError, RunOptions
from papercleaner.pipeline import run


class _PaperCleanerParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        if "the following arguments are required:" in message:
            self.print_usage(sys.stderr)
            self.exit(
                2,
                f"{self.prog}: error: missing input: "
                "<paper directory or archive>\n",
            )
        super().error(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _PaperCleanerParser(
        prog="papercleaner",
        description=(
            "Copy a LaTeX paper directory or archive, remove .tex comments, "
            "and optionally delete unreferenced assets."
        ),
        allow_abbrev=False,
    )
    parser.add_argument(
        "input",
        metavar="<paper directory or archive>",
        type=Path,
        help="paper directory or archive to process",
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="<output path>",
        type=Path,
        default=None,
        help="write there instead of the default sibling -cleaned path",
    )
    parser.add_argument(
        "-d",
        "--deps",
        action="store_true",
        help="delete assets unreferenced by any root document",
    )
    parser.add_argument(
        "-a",
        "--all",
        dest="all_comments",
        action="store_true",
        help="remove all comments, including magic comments",
    )
    parser.add_argument(
        "--keep-eol",
        action="store_true",
        help="keep end-of-line comments",
    )
    parser.add_argument(
        "--keep-ws",
        action="store_true",
        help="keep trailing whitespace exposed by removed end-of-line comments",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def _print_report(report) -> None:
    print(f"output: {report.output_path}")
    print(f"tex files: {report.tex_files}")
    print(f"comments removed: {report.comments_removed}")
    print(f"magic comments kept: {report.magic_comments_kept}")
    if report.deps_requested and not report.dependency_skipped_reasons:
        print(f"dependencies deleted: {len(report.deleted)}")
        for relative in report.deleted:
            print(f"  {relative.as_posix()}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.all_comments and args.keep_eol:
        parser.error("-a/--all cannot be combined with --keep-eol")

    options = RunOptions(
        input_path=args.input,
        output_path=args.output,
        deps=args.deps,
        all_comments=args.all_comments,
        keep_eol=args.keep_eol,
        keep_ws=args.keep_ws,
    )

    try:
        report = run(options)
    except PaperCleanerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    for warning in report.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    _print_report(report)

    if report.dependency_skipped_reasons:
        print(
            "warning: dependency cleanup skipped; no files were deleted",
            file=sys.stderr,
        )
        for reason in report.dependency_skipped_reasons:
            print(f"reason: {reason}", file=sys.stderr)
        return 3
    return 0
