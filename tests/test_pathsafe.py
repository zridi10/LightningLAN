import os
from pathlib import Path

import pytest

from lightninglan.pathsafe import (
    UnsafePathError,
    display_name,
    numbered_names,
    safe_join,
    safe_local_filename,
    split_extension,
    split_relative,
)

from .conftest import can_symlink, make_junction, windows_only

TRAVERSAL_AND_ABSOLUTE = [
    "..",
    "../secret.txt",
    "..\\secret.txt",
    "a/../../secret.txt",
    "a\\..\\..\\secret.txt",
    "sub/../../../etc/passwd",
    "./../x",
    "a/..",
    "/etc/passwd",
    "/",
    "\\Windows\\win.ini",
    "C:\\Windows\\win.ini",
    "C:/Windows/win.ini",
    "c:",
    "C:secret.txt",  # drive-relative
    "\\\\server\\share\\file.txt",  # UNC
    "//server/share/file.txt",
    "\\\\?\\C:\\Windows\\win.ini",  # extended-length prefix
    "\\\\.\\PhysicalDrive0",  # device namespace
    "\\\\?\\UNC\\server\\share",
]

NOT_PORTABLE = [
    "",
    ".",
    "a/./b",
    "a//b",
    "a/",
    "evil\x00.txt",
    "file.txt:hidden",  # NTFS alternate data stream
    "file.txt::$DATA",
    "CON",
    "con.txt",
    "NUL",
    "aux.tar.gz",
    "COM1",
    "LPT9.log",
    "a/CON/b.txt",
    "trailingdot.",
    "trailingspace ",
    "...",
    "a<b.txt",
    'a"b.txt',
    "a|b.txt",
    "a?b.txt",
    "a*b.txt",
    "tab\there.txt",
    "newline\n.txt",
    "x" * 300,
]

VALID = [
    "file.txt",
    "folder/file.txt",
    "folder\\file.txt",
    "deep/er/still/file.bin",
    "Résumé 2024.pdf",
    "файл.txt",
    "日本語 ファイル.txt",
    "a..b.txt",
    "..not-traversal",
    "console.txt",  # only the exact device names are reserved
    "report (1).pdf",
]


@pytest.mark.parametrize("name", TRAVERSAL_AND_ABSOLUTE + NOT_PORTABLE)
def test_split_relative_rejects(name):
    with pytest.raises(UnsafePathError):
        split_relative(name)


@pytest.mark.parametrize("name", TRAVERSAL_AND_ABSOLUTE + NOT_PORTABLE)
def test_safe_join_rejects(tmp_path, name):
    with pytest.raises(UnsafePathError):
        safe_join(tmp_path, name)


@pytest.mark.parametrize("name", VALID)
def test_safe_join_accepts_and_stays_inside(tmp_path, name):
    root = tmp_path.resolve()
    result = safe_join(tmp_path, name)
    assert root in result.parents
    assert result.name == Path(name.replace("\\", "/")).name


def test_split_relative_normalises_separators():
    assert split_relative("a\\b/c.txt") == ["a", "b", "c.txt"]


def test_safe_join_rejects_non_string(tmp_path):
    with pytest.raises(UnsafePathError):
        safe_join(tmp_path, None)  # type: ignore[arg-type]


def test_symlink_escaping_root_is_rejected(tmp_path):
    if not can_symlink(tmp_path):
        pytest.skip("creating symlinks needs privileges on this machine")
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    os.symlink(outside / "secret.txt", root / "link.txt")
    os.symlink(outside, root / "linkdir", target_is_directory=True)
    with pytest.raises(UnsafePathError):
        safe_join(root, "link.txt")
    with pytest.raises(UnsafePathError):
        safe_join(root, "linkdir/secret.txt")


def test_symlink_inside_root_is_allowed(tmp_path):
    if not can_symlink(tmp_path):
        pytest.skip("creating symlinks needs privileges on this machine")
    (tmp_path / "real.txt").write_text("x")
    os.symlink(tmp_path / "real.txt", tmp_path / "alias.txt")
    assert safe_join(tmp_path, "alias.txt") == (tmp_path / "real.txt").resolve()


@windows_only
def test_junction_escaping_root_is_rejected(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    make_junction(outside, root / "junction")
    with pytest.raises(UnsafePathError):
        safe_join(root, "junction/secret.txt")
    with pytest.raises(UnsafePathError):
        safe_join(root, "junction/new-upload.txt")


def test_display_name_strips_bidi_and_control_characters():
    assert display_name("invoice\u202etxt.exe") == "invoicetxt.exe"
    assert display_name("a\x07b\x1bc") == "a\ufffdb\ufffdc"
    # HTML is left as text; the UI renders names with textContent only.
    assert display_name("<img src=x onerror=alert(1)>.txt") == "<img src=x onerror=alert(1)>.txt"


@pytest.mark.parametrize(
    "remote, expected",
    [
        ("report.pdf", "report.pdf"),
        ("folder/sub/report.pdf", "report.pdf"),
        ("..\\..\\Windows\\evil.exe", "evil.exe"),
        ("../../", "download"),
        ("..", "download"),
        ("", "download"),
        ("C:\\Windows\\System32\\drivers", "drivers"),
        ("a:b*c?.txt", "a_b_c_.txt"),
        ("CON.txt", "_CON.txt"),
        ("nul", "_nul"),
        (".bashrc", "_bashrc"),
        ("name. . .", "name"),
        ("bidi\u202egnp.exe", "bidignp.exe"),
        ("ctrl\x01char.txt", "ctrl_char.txt"),
    ],
)
def test_safe_local_filename(remote, expected):
    assert safe_local_filename(remote) == expected


def test_safe_local_filename_truncates_long_names_keeping_extension():
    name = safe_local_filename("x" * 400 + ".mp4")
    assert name.endswith(".mp4")
    assert len(name.encode("utf-8")) <= 200


@pytest.mark.parametrize(
    "filename, expected",
    [
        ("report.pdf", ("report", ".pdf")),
        ("archive.tar.gz", ("archive", ".tar.gz")),
        ("noext", ("noext", "")),
        (".bashrc", (".bashrc", "")),
        ("a.b.c", ("a.b", ".c")),
    ],
)
def test_split_extension(filename, expected):
    assert split_extension(filename) == expected


def test_numbered_names():
    gen = numbered_names("report.pdf")
    assert [next(gen) for _ in range(4)] == ["report.pdf", "report (1).pdf", "report (2).pdf", "report (3).pdf"]
    gen = numbered_names("archive.tar.gz")
    assert [next(gen) for _ in range(2)] == ["archive.tar.gz", "archive (1).tar.gz"]
