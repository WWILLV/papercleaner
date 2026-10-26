"""End-to-end orchestration for papercleaner."""

from __future__ import annotations

from papercleaner.comments import clean_comments, decode_source, encode_source
from papercleaner.dependencies import analyze_dependencies
from papercleaner.models import (
    CommentOptions,
    InputError,
    ProcessingError,
    RunOptions,
    RunReport,
    TexFileResult,
)
from papercleaner.workspace import (
    detect_input,
    open_workspace,
    resolve_output,
    write_output,
)


def run(options: RunOptions) -> RunReport:
    spec = detect_input(options.input_path)
    output_path = resolve_output(spec, options.output_path)
    warnings: list[str] = []

    with open_workspace(spec) as workspace:
        tex_files = tuple(
            relative
            for relative in workspace.files
            if relative.suffix.lower() == ".tex"
        )
        if not tex_files:
            raise InputError("input contains no .tex files")

        comment_options = CommentOptions(
            keep_eol=options.keep_eol,
            keep_ws=options.keep_ws,
            remove_all=options.all_comments,
        )
        results: dict = {}
        for relative in tex_files:
            source_path = workspace.root.joinpath(*relative.parts)
            try:
                raw = source_path.read_bytes()
            except OSError as exc:
                raise ProcessingError(
                    f"cannot read {relative.as_posix()}: {exc}"
                ) from exc
            try:
                decoded = decode_source(raw)
            except InputError as exc:
                raise InputError(
                    f"{relative.as_posix()}: {exc}"
                ) from exc

            output_clean = clean_comments(decoded.text, comment_options)
            analysis_clean = clean_comments(
                decoded.text,
                CommentOptions(remove_all=True, keep_ws=True),
            )
            try:
                output_bytes = encode_source(decoded, output_clean.text)
            except UnicodeEncodeError as exc:
                raise ProcessingError(
                    f"cannot re-encode {relative.as_posix()}: {exc}"
                ) from exc

            results[relative] = TexFileResult(
                rel=relative,
                original_text=decoded.text,
                output_text=output_clean.text,
                output_bytes=output_bytes,
                analysis_text=analysis_clean.text,
                comments_removed=output_clean.comments_removed,
                magic_comments_kept=output_clean.magic_comments_kept,
                magic_comments=output_clean.magic_comments,
                warnings=output_clean.warnings,
            )
            warnings.extend(output_clean.warnings)

        deleted = ()
        skip_reasons = ()
        if options.deps:
            plan = analyze_dependencies(workspace, results)
            warnings.extend(plan.warnings)
            skip_reasons = plan.skip_reasons
            deleted = () if skip_reasons else plan.deletions

        overrides = {
            relative: result.output_bytes
            for relative, result in results.items()
        }
        write_output(
            spec,
            workspace,
            output_path,
            overrides,
            set(deleted),
        )

    return RunReport(
        output_path=output_path,
        tex_files=len(tex_files),
        comments_removed=sum(result.comments_removed for result in results.values()),
        magic_comments_kept=sum(
            result.magic_comments_kept for result in results.values()
        ),
        deps_requested=options.deps,
        deleted=tuple(deleted),
        dependency_skipped_reasons=tuple(skip_reasons),
        warnings=tuple(sorted(set(warnings))),
    )
