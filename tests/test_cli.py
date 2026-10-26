from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from papercleaner import cli
from papercleaner.models import ProcessingError

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def run_cli(
    *arguments: object, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    source_path = str(PROJECT_ROOT / "src")
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_path if not existing else source_path + os.pathsep + existing
    )
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "papercleaner",
            *(str(argument) for argument in arguments),
        ],
        cwd=PROJECT_ROOT if cwd is None else cwd,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def write_tree(root: Path, files: dict[str, str | bytes]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8", newline="")


def snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        path.relative_to(root).as_posix(): (
            path.read_bytes(),
            path.stat().st_mtime_ns,
        )
        for path in root.rglob("*")
        if path.is_file()
    }


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_default_directory_run_is_exit_0_and_does_not_delete(
    tmp_path: Path,
) -> None:
    source = tmp_path / "paper"
    (source / "chapters").mkdir(parents=True)
    (source / "figures").mkdir()
    write_tree(
        source,
        {
            "main.tex": (
                "\\documentclass{article}\n"
                "% whole comment\n"
                "\\input{chapters/intro}\n"
                "\\includegraphics{figures/used}\n"
                "text   % eol comment\n"
                "% !TEX root = main.tex\n"
            ),
            "chapters/intro.tex": "intro % intro comment\n",
            "figures/used.pdf": b"used",
            "figures/unused.png": b"unused",
            "main.pdf": b"root pdf",
            "README.md": "readme\n",
            "refs.bib": "% bib comment\n@article{x,}\n",
        },
    )
    before = snapshot(source)
    result = run_cli(source)
    output = tmp_path / "paper-cleaned"

    assert result.returncode == 0, result.stderr
    assert output.is_dir()
    main_text = (output / "main.tex").read_text(encoding="utf-8")
    assert "% whole comment" not in main_text
    assert "text\n" in main_text
    assert "% !TEX root = main.tex" in main_text
    assert (output / "chapters/intro.tex").read_text(encoding="utf-8") == "intro\n"
    assert (output / "figures/unused.png").exists()
    assert (output / "figures/used.pdf").exists()
    assert (output / "main.pdf").exists()
    assert (output / "README.md").exists()
    assert (output / "refs.bib").read_bytes() == (source / "refs.bib").read_bytes()
    assert snapshot(source) == before
    assert f"output: {output}" in result.stdout
    assert "tex files: 2" in result.stdout
    assert "comments removed: 3" in result.stdout
    assert "magic comments kept: 1" in result.stdout

    repeated = run_cli(source)
    assert repeated.returncode == 2
    assert "already exists" in repeated.stderr


def test_deps_deletes_only_unreferenced_assets(tmp_path: Path) -> None:
    source = tmp_path / "paper"
    write_tree(
        source,
        {
            "main.tex": (
                "\\documentclass{article}\n"
                "\\includegraphics{used}\n"
            ),
            "used.png": b"used",
            "unused.png": b"unused",
            "main.pdf": b"root pdf",
        },
    )
    output = tmp_path / "nested" / "deps-output"
    result = run_cli(source, "-d", "-o", output)
    assert result.returncode == 0, result.stderr
    assert (output / "used.png").exists()
    assert not (output / "unused.png").exists()
    assert (output / "main.pdf").exists()
    assert "dependencies deleted: 1" in result.stdout
    assert "unused.png" in result.stdout


def test_keep_ws_and_default_whitespace_behavior(tmp_path: Path) -> None:
    source = tmp_path / "paper"
    write_tree(source, {"main.tex": "text   % note\r\n"})

    default_output = tmp_path / "default"
    result = run_cli(source, "-o", default_output)
    assert result.returncode == 0, result.stderr
    assert (default_output / "main.tex").read_bytes() == b"text\r\n"

    kept_output = tmp_path / "kept"
    result = run_cli(source, "--keep-ws", "-o", kept_output)
    assert result.returncode == 0, result.stderr
    assert (kept_output / "main.tex").read_bytes() == b"text   \r\n"


def test_keep_eol_and_all_behavior(tmp_path: Path) -> None:
    source = tmp_path / "paper"
    write_tree(
        source,
        {
            "main.tex": (
                "% !TEX root = main.tex\n"
                "% whole\n"
                "text % eol\n"
            )
        },
    )

    eol_output = tmp_path / "eol"
    result = run_cli(source, "--keep-eol", "-o", eol_output)
    assert result.returncode == 0, result.stderr
    assert (eol_output / "main.tex").read_text(encoding="utf-8") == (
        "% !TEX root = main.tex\n"
        "text % eol\n"
    )

    all_output = tmp_path / "all"
    result = run_cli(source, "-a", "-o", all_output)
    assert result.returncode == 0, result.stderr
    assert (all_output / "main.tex").read_text(encoding="utf-8") == "text\n"

    invalid_output = tmp_path / "invalid"
    result = run_cli(source, "-a", "--keep-eol", "-o", invalid_output)
    assert result.returncode == 2
    assert not invalid_output.exists()


def test_missing_tex_input_is_exit_2_without_output(tmp_path: Path) -> None:
    source = tmp_path / "empty"
    source.mkdir()
    output = tmp_path / "output"
    result = run_cli(source, "-o", output)
    assert result.returncode == 2
    assert not output.exists()


def test_unsupported_tex_encoding_is_exit_2_without_output(
    tmp_path: Path,
) -> None:
    source = tmp_path / "paper"
    source.mkdir()
    (source / "main.tex").write_bytes(
        b"\xff\xfe" + "\\documentclass{article}".encode("utf-16-le")
    )
    output = tmp_path / "output"
    result = run_cli(source, "-o", output)
    assert result.returncode == 2
    assert not output.exists()
    assert "UTF-16" in result.stderr


def test_no_root_with_deps_is_exit_3_but_still_writes_output(
    tmp_path: Path,
) -> None:
    source = tmp_path / "paper"
    write_tree(
        source,
        {"main.tex": "not a root\n", "unused.png": b"unused"},
    )
    output = tmp_path / "output"
    result = run_cli(source, "-d", "-o", output)
    assert result.returncode == 3
    assert output.is_dir()
    assert (output / "main.tex").exists()
    assert (output / "unused.png").exists()
    assert "dependency cleanup skipped" in result.stderr
    assert "reason:" in result.stderr


def test_missing_reference_with_deps_is_exit_3(tmp_path: Path) -> None:
    source = tmp_path / "paper"
    write_tree(
        source,
        {
            "main.tex": (
                "\\documentclass{article}\n"
                "\\includegraphics{missing}\n"
            ),
            "unused.png": b"unused",
        },
    )
    output = tmp_path / "output"
    result = run_cli(source, "-d", "-o", output)
    assert result.returncode == 3
    assert output.is_dir()
    assert (output / "unused.png").exists()
    assert "unresolved reference" in result.stderr


def test_zip_round_trip_keeps_format_and_input_hash(tmp_path: Path) -> None:
    archive_path = tmp_path / "paper.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            "main.tex",
            "\\documentclass{article}\n"
            "\\includegraphics{used}\n"
            "% comment\n",
        )
        archive.writestr("used.png", b"used")
        archive.writestr("unused.png", b"unused")
        archive.writestr("README.md", "readme\n")
    before = digest(archive_path)

    result = run_cli(archive_path, "-d")
    output = tmp_path / "paper-cleaned.zip"
    assert result.returncode == 0, result.stderr
    assert output.is_file()
    assert zipfile.is_zipfile(output)
    assert digest(archive_path) == before
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        assert "unused.png" not in names
        assert "used.png" in names
        assert "% comment" not in archive.read("main.tex").decode("utf-8")
        assert "README.md" in names


def test_relative_output_with_zip_input_lands_in_cwd(tmp_path: Path) -> None:
    workdir = tmp_path / "run"
    workdir.mkdir()
    archive_path = workdir / "paper.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("main.tex", "\\documentclass{article}\n% comment\n")

    # A relative -o is resolved against the caller's cwd before the archive
    # is extracted to its temporary directory, so the output must land in
    # the cwd and never inside the extraction temp.
    result = run_cli("paper.zip", "-o", "out.zip", cwd=workdir)

    expected = workdir / "out.zip"
    assert result.returncode == 0, result.stderr
    assert expected.is_file()
    assert zipfile.is_zipfile(expected)
    assert f"output: {expected}" in result.stdout
    assert sorted(path.name for path in workdir.iterdir()) == [
        "out.zip",
        "paper.zip",
    ]


def test_output_inside_input_is_exit_2(tmp_path: Path) -> None:
    source = tmp_path / "paper"
    write_tree(source, {"main.tex": "x\n"})
    output = source / "nested" / "output"
    result = run_cli(source, "-o", output)
    assert result.returncode == 2
    assert not output.exists()


def test_existing_output_is_exit_2_and_untouched(tmp_path: Path) -> None:
    source = tmp_path / "paper"
    write_tree(source, {"main.tex": "x\n"})
    output = tmp_path / "existing"
    output.mkdir()
    (output / "sentinel").write_text("keep", encoding="utf-8")
    result = run_cli(source, "-o", output)
    assert result.returncode == 2
    assert (output / "sentinel").read_text(encoding="utf-8") == "keep"


def test_help_and_version() -> None:
    help_result = run_cli("--help")
    assert help_result.returncode == 0
    for flag in (
        "--output",
        "--deps",
        "--all",
        "--keep-eol",
        "--keep-ws",
        "-v",
        "--version",
    ):
        assert flag in help_result.stdout
    assert "<paper directory or archive>" in help_result.stdout
    assert "paper directory or archive to process" in help_result.stdout
    assert "-o <output path>, --output <output path>" in help_result.stdout
    for flag in ("-v", "--version"):
        version_result = run_cli(flag)
        assert version_result.returncode == 0
        assert "papercleaner 0.1.0" in version_result.stdout


def test_missing_input_error_explains_the_expected_argument() -> None:
    result = run_cli()
    assert result.returncode == 2
    assert "missing input" in result.stderr
    assert "<paper directory or archive>" in result.stderr


def test_processing_error_maps_to_exit_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    def fail(options):
        raise ProcessingError("simulated disk failure")

    monkeypatch.setattr(cli, "run", fail)
    exit_code = cli.main([str(tmp_path / "input")])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "error: simulated disk failure" in captured.err


def test_readme_and_help_document_the_same_flags() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    help_result = run_cli("--help")
    assert help_result.returncode == 0
    for flag in (
        "--output",
        "--deps",
        "--all",
        "--keep-eol",
        "--keep-ws",
        "-v",
        "--version",
    ):
        assert flag in readme
        assert flag in help_result.stdout
