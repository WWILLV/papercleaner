from __future__ import annotations

import io
import os
import stat
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

import pytest

from papercleaner import workspace as workspace_module
from papercleaner.models import (
    ArchiveKind,
    InputError,
    InputKind,
    InputSpec,
    OutputError,
    ProcessingError,
)
from papercleaner.workspace import (
    detect_input,
    open_workspace,
    resolve_output,
    scan_directory,
    write_archive_output,
    write_directory_output,
    write_output,
)


def test_detect_directory(tmp_path: Path) -> None:
    source = tmp_path / "paper"
    source.mkdir()
    spec = detect_input(source)
    assert spec.kind == InputKind.DIRECTORY
    assert spec.path == source.absolute()
    assert spec.archive_kind is None


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("paper.zip", ArchiveKind.ZIP),
        ("PAPER.ZIP", ArchiveKind.ZIP),
        ("paper.tar", ArchiveKind.TAR),
        ("paper.tar.gz", ArchiveKind.TAR_GZ),
        ("paper.tgz", ArchiveKind.TAR_GZ),
        ("paper.tar.bz2", ArchiveKind.TAR_BZ2),
        ("paper.tar.xz", ArchiveKind.TAR_XZ),
    ],
)
def test_detect_supported_archives(tmp_path: Path, name: str, kind: ArchiveKind) -> None:
    archive = tmp_path / name
    archive.write_bytes(b"not parsed during detection")
    spec = detect_input(archive)
    assert spec.kind == InputKind.ARCHIVE
    assert spec.archive_kind == kind
    assert spec.archive_suffix is not None


def test_unsupported_archive_extension_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "paper.rar"
    archive.write_bytes(b"irrelevant")
    with pytest.raises(InputError):
        detect_input(archive)


def test_missing_input_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(InputError):
        detect_input(tmp_path / "missing")


def test_archive_suffix_matching_uses_casefold(tmp_path: Path) -> None:
    archive = tmp_path / "PAPER.ZiP"
    archive.write_bytes(b"placeholder")
    spec = detect_input(archive)
    assert spec.archive_kind == ArchiveKind.ZIP
    assert spec.archive_suffix == ".ZiP"
    output = resolve_output(spec, tmp_path / "OUT.ZIP")
    assert output.name == "OUT.ZIP"


def test_default_directory_output_name(tmp_path: Path) -> None:
    source = tmp_path / "submission"
    source.mkdir()
    spec = detect_input(source)
    assert resolve_output(spec, None) == tmp_path / "submission-cleaned"


@pytest.mark.parametrize(
    ("input_name", "output_name"),
    [
        ("paper.zip", "paper-cleaned.zip"),
        ("paper.tar", "paper-cleaned.tar"),
        ("paper.tar.gz", "paper-cleaned.tar.gz"),
        ("paper.tgz", "paper-cleaned.tgz"),
        ("paper.tar.bz2", "paper-cleaned.tar.bz2"),
        ("paper.tar.xz", "paper-cleaned.tar.xz"),
    ],
)
def test_default_archive_output_name(
    tmp_path: Path, input_name: str, output_name: str
) -> None:
    archive = tmp_path / input_name
    archive.write_bytes(b"placeholder")
    spec = detect_input(archive)
    assert resolve_output(spec, None) == tmp_path / output_name


def test_tar_gz_and_tgz_are_the_same_output_format(tmp_path: Path) -> None:
    archive = tmp_path / "paper.tar.gz"
    archive.write_bytes(b"placeholder")
    spec = detect_input(archive)
    output = tmp_path / "other.tgz"
    assert resolve_output(spec, output) == output


def test_mismatched_archive_output_format_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "paper.zip"
    archive.write_bytes(b"placeholder")
    spec = detect_input(archive)
    with pytest.raises(OutputError):
        resolve_output(spec, tmp_path / "other.tar.gz")


def test_existing_output_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "paper"
    source.mkdir()
    output = tmp_path / "paper-cleaned"
    output.mkdir()
    with pytest.raises(OutputError):
        resolve_output(detect_input(source), output)


def test_output_inside_input_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "paper"
    source.mkdir()
    output = source / "nested" / "paper-cleaned"
    with pytest.raises(OutputError):
        resolve_output(detect_input(source), output)


def test_scan_excludes_vcs_and_keeps_empty_dirs_and_hidden_files(
    tmp_path: Path,
) -> None:
    (tmp_path / "main.tex").write_text("x", encoding="utf-8")
    (tmp_path / "empty").mkdir()
    (tmp_path / ".hidden").write_text("x", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("x", encoding="utf-8")
    workspace = scan_directory(tmp_path)
    assert workspace.files == (
        PurePosixPath(".hidden"),
        PurePosixPath("main.tex"),
    )
    assert workspace.directories == (PurePosixPath("empty"),)


def test_scan_rejects_symbolic_links(tmp_path: Path) -> None:
    target = tmp_path / "target.tex"
    target.write_text("x", encoding="utf-8")
    link = tmp_path / "link.tex"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available in this environment")
    with pytest.raises(InputError):
        scan_directory(tmp_path)


def _make_zip(path: Path, members: list[tuple[str, bytes]]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members:
            archive.writestr(name, data)


def _make_tar(
    path: Path, mode: str, members: list[tuple[str, bytes, bool]]
) -> None:
    with tarfile.open(path, mode) as archive:
        for name, data, is_dir in members:
            info = tarfile.TarInfo(name)
            if is_dir:
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                archive.addfile(info)
            else:
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))


def test_open_zip_workspace_and_temp_cleanup(tmp_path: Path) -> None:
    archive_path = tmp_path / "paper.zip"
    _make_zip(
        archive_path,
        [
            ("main.tex", b"\\documentclass{article}\n"),
            ("empty/", b""),
        ],
    )
    spec = detect_input(archive_path)
    with open_workspace(spec) as workspace:
        assert workspace.files == (PurePosixPath("main.tex"),)
        assert workspace.directories == (PurePosixPath("empty"),)
        temp_root = workspace.root
        assert temp_root.is_dir()
    assert not temp_root.exists()


@pytest.mark.parametrize(
    "member",
    ["../evil.txt", "..\\evil.txt", "/evil.txt", "C:\\evil.txt"],
)
def test_archive_traversal_is_rejected(tmp_path: Path, member: str) -> None:
    archive_path = tmp_path / "paper.zip"
    _make_zip(archive_path, [(member, b"x")])
    with pytest.raises(InputError):
        with open_workspace(detect_input(archive_path)):
            pass


def test_archive_backslashes_are_normalized_when_safe(tmp_path: Path) -> None:
    archive_path = tmp_path / "paper.zip"
    _make_zip(archive_path, [("chapters\\intro.tex", b"x")])
    with open_workspace(detect_input(archive_path)) as workspace:
        assert workspace.files == (PurePosixPath("chapters/intro.tex"),)


def test_zip_symlink_member_is_rejected(tmp_path: Path) -> None:
    archive_path = tmp_path / "paper.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "target.tex")
    with pytest.raises(InputError):
        with open_workspace(detect_input(archive_path)):
            pass


def test_zip_duplicate_path_is_rejected(tmp_path: Path) -> None:
    archive_path = tmp_path / "paper.zip"
    # zipfile warns about the duplicate name; that warning is the fixture's
    # expected behavior, not an error.
    with pytest.warns(UserWarning, match="Duplicate name"):
        _make_zip(archive_path, [("dup.txt", b"a"), ("dup.txt", b"b")])
    with pytest.raises(InputError):
        with open_workspace(detect_input(archive_path)):
            pass


def test_zip_file_directory_conflict_is_rejected(tmp_path: Path) -> None:
    archive_path = tmp_path / "paper.zip"
    _make_zip(archive_path, [("a", b"x"), ("a/b.txt", b"y")])
    with pytest.raises(InputError):
        with open_workspace(detect_input(archive_path)):
            pass


def test_tar_symlink_member_is_rejected(tmp_path: Path) -> None:
    archive_path = tmp_path / "paper.tar"
    with tarfile.open(archive_path, "w") as archive:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "target.tex"
        archive.addfile(info)
    with pytest.raises(InputError):
        with open_workspace(detect_input(archive_path)):
            pass


@pytest.mark.parametrize(
    ("suffix", "mode", "kind"),
    [
        (".tar", "w", ArchiveKind.TAR),
        (".tar.gz", "w:gz", ArchiveKind.TAR_GZ),
        (".tgz", "w:gz", ArchiveKind.TAR_GZ),
        (".tar.bz2", "w:bz2", ArchiveKind.TAR_BZ2),
        (".tar.xz", "w:xz", ArchiveKind.TAR_XZ),
    ],
)
def test_all_tar_variants_extract(
    tmp_path: Path, suffix: str, mode: str, kind: ArchiveKind
) -> None:
    archive_path = tmp_path / f"paper{suffix}"
    _make_tar(
        archive_path,
        mode,
        [("empty", b"", True), ("main.tex", b"\\documentclass{article}\n", False)],
    )
    spec = detect_input(archive_path)
    assert spec.archive_kind == kind
    with open_workspace(spec) as workspace:
        assert workspace.files == (PurePosixPath("main.tex"),)
        assert workspace.directories == (PurePosixPath("empty"),)


def test_corrupt_archive_is_rejected(tmp_path: Path) -> None:
    archive_path = tmp_path / "paper.zip"
    archive_path.write_bytes(b"not a zip")
    with pytest.raises(InputError):
        with open_workspace(detect_input(archive_path)):
            pass


def write_tree(root: Path, files: dict[str, str | bytes]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8", newline="")


def _snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        path.relative_to(root).as_posix(): (
            path.read_bytes(),
            path.stat().st_mtime_ns,
        )
        for path in root.rglob("*")
        if path.is_file()
    }


def test_write_directory_copies_overrides_and_skips_deleted(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    write_tree(
        source,
        {
            "main.tex": "original\n",
            "keep.txt": "keep\n",
            "unused.png": b"png",
            ".hidden": "hidden\n",
        },
    )
    (source / "empty").mkdir()
    before = _snapshot(source)
    workspace = scan_directory(source)
    output = tmp_path / "output"

    write_directory_output(
        workspace,
        output,
        {PurePosixPath("main.tex"): b"cleaned\n"},
        {PurePosixPath("unused.png")},
    )

    assert (output / "main.tex").read_bytes() == b"cleaned\n"
    assert (output / "keep.txt").read_text(encoding="utf-8") == "keep\n"
    assert (output / ".hidden").read_text(encoding="utf-8") == "hidden\n"
    assert (output / "empty").is_dir()
    assert not (output / "unused.png").exists()
    assert _snapshot(source) == before


def test_write_directory_refuses_existing_target(tmp_path: Path) -> None:
    source = tmp_path / "source"
    write_tree(source, {"main.tex": "x"})
    output = tmp_path / "output"
    output.mkdir()
    (output / "sentinel").write_text("keep", encoding="utf-8")
    with pytest.raises(OutputError):
        write_directory_output(
            scan_directory(source), output, {}, set()
        )
    assert (output / "sentinel").read_text(encoding="utf-8") == "keep"


def test_write_directory_failure_cleans_temporary_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    write_tree(source, {"main.tex": "x"})
    output = tmp_path / "output"

    def fail_replace(source_path, target_path):
        raise OSError("simulated commit failure")

    monkeypatch.setattr(workspace_module.os, "replace", fail_replace)
    with pytest.raises(ProcessingError):
        write_directory_output(scan_directory(source), output, {}, set())

    assert not output.exists()
    assert not any(
        path.name.startswith(".papercleaner-") for path in tmp_path.iterdir()
    )


@pytest.mark.parametrize(
    ("kind", "suffix"),
    [
        (ArchiveKind.ZIP, ".zip"),
        (ArchiveKind.TAR, ".tar"),
        (ArchiveKind.TAR_GZ, ".tar.gz"),
        (ArchiveKind.TAR_GZ, ".tgz"),
        (ArchiveKind.TAR_BZ2, ".tar.bz2"),
        (ArchiveKind.TAR_XZ, ".tar.xz"),
    ],
)
def test_write_archive_outputs_preserve_structure_and_overrides(
    tmp_path: Path, kind: ArchiveKind, suffix: str
) -> None:
    source = tmp_path / "source"
    write_tree(
        source,
        {
            "main.tex": "original\n",
            "keep.txt": "keep\n",
            "unused.png": b"png",
        },
    )
    (source / "empty").mkdir()
    workspace = scan_directory(source)
    output = tmp_path / f"output{suffix}"
    spec = InputSpec(
        path=tmp_path / f"input{suffix}",
        kind=InputKind.ARCHIVE,
        archive_kind=kind,
        archive_suffix=suffix,
    )

    write_archive_output(
        spec,
        workspace,
        output,
        {PurePosixPath("main.tex"): b"cleaned\n"},
        {PurePosixPath("unused.png")},
    )

    if kind == ArchiveKind.ZIP:
        with zipfile.ZipFile(output) as archive:
            names = archive.namelist()
            assert "unused.png" not in names
            assert any(name.rstrip("/") == "empty" for name in names)
            assert archive.read("main.tex") == b"cleaned\n"
            assert archive.read("keep.txt") == b"keep\n"
    else:
        with tarfile.open(output, "r:*") as archive:
            names = archive.getnames()
            assert "unused.png" not in names
            assert any(name.rstrip("/") == "empty" for name in names)
            main_member = archive.getmember("main.tex")
            keep_member = archive.getmember("keep.txt")
            assert archive.extractfile(main_member).read() == b"cleaned\n"
            assert archive.extractfile(keep_member).read() == b"keep\n"


def test_zip_files_are_deflated_and_deterministic(tmp_path: Path) -> None:
    source = tmp_path / "source"
    write_tree(source, {"main.tex": "x " * 500})
    spec = InputSpec(
        path=tmp_path / "input.zip",
        kind=InputKind.ARCHIVE,
        archive_kind=ArchiveKind.ZIP,
        archive_suffix=".zip",
    )
    outputs = []
    for name in ("one.zip", "two.zip"):
        output = tmp_path / name
        write_archive_output(spec, scan_directory(source), output, {}, set())
        with zipfile.ZipFile(output) as archive:
            info = archive.getinfo("main.tex")
            assert info.compress_type == zipfile.ZIP_DEFLATED
            assert archive.read("main.tex") == b"x " * 500
        outputs.append(output.read_bytes())
    assert outputs[0] == outputs[1]


def test_write_archive_refuses_existing_target(tmp_path: Path) -> None:
    source = tmp_path / "source"
    write_tree(source, {"main.tex": "x"})
    output = tmp_path / "output.zip"
    output.write_bytes(b"sentinel")
    spec = InputSpec(
        path=tmp_path / "input.zip",
        kind=InputKind.ARCHIVE,
        archive_kind=ArchiveKind.ZIP,
        archive_suffix=".zip",
    )
    with pytest.raises(OutputError):
        write_archive_output(spec, scan_directory(source), output, {}, set())
    assert output.read_bytes() == b"sentinel"


def test_write_archive_failure_cleans_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    write_tree(source, {"main.tex": "x"})
    output = tmp_path / "output.zip"
    spec = InputSpec(
        path=tmp_path / "input.zip",
        kind=InputKind.ARCHIVE,
        archive_kind=ArchiveKind.ZIP,
        archive_suffix=".zip",
    )

    def fail_replace(source_path, target_path):
        raise OSError("simulated commit failure")

    monkeypatch.setattr(workspace_module.os, "replace", fail_replace)
    with pytest.raises(ProcessingError):
        write_archive_output(spec, scan_directory(source), output, {}, set())
    assert not output.exists()
    assert not any(
        path.name.startswith(".papercleaner-") for path in tmp_path.iterdir()
    )


def test_write_output_dispatches_by_input_kind(tmp_path: Path) -> None:
    source = tmp_path / "source"
    write_tree(source, {"main.tex": "x"})
    workspace = scan_directory(source)

    directory_spec = InputSpec(
        path=source, kind=InputKind.DIRECTORY
    )
    directory_output = tmp_path / "directory-output"
    write_output(directory_spec, workspace, directory_output, {}, set())
    assert directory_output.is_dir()

    archive_spec = InputSpec(
        path=tmp_path / "input.zip",
        kind=InputKind.ARCHIVE,
        archive_kind=ArchiveKind.ZIP,
        archive_suffix=".zip",
    )
    archive_output = tmp_path / "archive-output.zip"
    write_output(archive_spec, workspace, archive_output, {}, set())
    assert zipfile.is_zipfile(archive_output)
