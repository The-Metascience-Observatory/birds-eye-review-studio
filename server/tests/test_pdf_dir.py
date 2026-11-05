"""The user-configurable PDF download directory.

Covers the validator (the security boundary — this is the only user-supplied
filesystem path the server writes to), the WSL/Windows path translation, the
precedence chain, and the settings endpoint round-trip.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services import pdf_dir


# --- validation ------------------------------------------------------------

@pytest.mark.parametrize("raw", ["", None, "   "])
def test_blank_means_default(raw) -> None:
    assert pdf_dir.validate_pdf_dir(raw) is None


@pytest.mark.parametrize(
    "raw, expected",
    [
        (r"C:\Users\someone\PDFs", "/mnt/c/Users/someone/PDFs"),
        ("C:/Users/someone/PDFs", "/mnt/c/Users/someone/PDFs"),
        (r"D:\data\pdfs", "/mnt/d/data/pdfs"),
    ],
)
def test_windows_paths_translate_for_wsl(raw: str, expected: str) -> None:
    got = pdf_dir.validate_pdf_dir(raw)
    assert got is not None and got.path == expected


def test_windows_drive_warns_about_conversion() -> None:
    got = pdf_dir.validate_pdf_dir(r"C:\Users\someone\PDFs")
    assert got is not None and got.warning
    # The warning must mention the real consequence, not just "slow".
    assert "symlink" in got.warning.lower()


@pytest.mark.parametrize(
    "raw",
    [
        "/etc", "/etc/cron.d", "/usr/bin", "/bin", "/boot", "/root/pdfs",
        "/proc", "/sys", "/",
    ],
)
def test_system_directories_rejected(raw: str) -> None:
    with pytest.raises(ValueError):
        pdf_dir.validate_pdf_dir(raw)


@pytest.mark.parametrize("raw", ["/home/me/.ssh", "/home/me/.aws/x", "/home/me/.gnupg"])
def test_credential_directories_rejected(raw: str) -> None:
    with pytest.raises(ValueError):
        pdf_dir.validate_pdf_dir(raw)


@pytest.mark.parametrize("raw", ["/tmp/../etc/passwd", "/home/x/../../etc"])
def test_traversal_cannot_escape_into_system_dirs(raw: str) -> None:
    # Resolution happens before the denylist check, so ".." can't smuggle a
    # system path past it.
    with pytest.raises(ValueError):
        pdf_dir.validate_pdf_dir(raw)


@pytest.mark.parametrize("raw", ["relative/path", "pdfs", "./pdfs"])
def test_relative_paths_rejected(raw: str) -> None:
    with pytest.raises(ValueError):
        pdf_dir.validate_pdf_dir(raw)


@pytest.mark.parametrize("raw", [r"\\server\share", r"\\192.168.1.5\pdfs"])
def test_unc_paths_rejected(raw: str) -> None:
    # Regression: an earlier version normalized backslashes first, turning
    # \\server\share into the plausible-looking POSIX path /server/share.
    with pytest.raises(ValueError) as exc:
        pdf_dir.validate_pdf_dir(raw)
    assert "UNC" in str(exc.value) or "Network" in str(exc.value)


def test_ordinary_home_path_accepted_without_warning(tmp_path: Path) -> None:
    got = pdf_dir.validate_pdf_dir(str(tmp_path / "pdfs"))
    assert got is not None
    assert got.path == str(tmp_path / "pdfs")
    assert got.warning is None


def test_writable_check_flags_unwritable_parent() -> None:
    assert pdf_dir.writable_or_reason("/proc/nope/pdfs") is not None


def test_writable_check_passes_for_tmp(tmp_path: Path) -> None:
    assert pdf_dir.writable_or_reason(str(tmp_path / "new" / "pdfs")) is None


# --- precedence ------------------------------------------------------------

def _workspace(tmp_path: Path, base_pdf_dir: str | None) -> tuple[Path, Path]:
    ws = tmp_path / "ws"
    (ws / "projects" / "slug").mkdir(parents=True)
    dirs = f'dirs:\n  base_pdf_dir: "{base_pdf_dir}"\n' if base_pdf_dir is not None else ""
    (ws / "config.yaml").write_text(f"topic:\n  name: Slug\n{dirs}")
    return ws, ws / "projects" / "slug"


def test_precedence_review_config_wins(tmp_path: Path) -> None:
    ws, proj = _workspace(tmp_path, "/data/review_pdfs")
    got = pdf_dir.resolve_for_workspace(ws, proj, "/data/user_pdfs")
    assert got == Path("/data/review_pdfs")


def test_precedence_user_setting_when_no_review_override(tmp_path: Path) -> None:
    ws, proj = _workspace(tmp_path, None)
    got = pdf_dir.resolve_for_workspace(ws, proj, "/data/user_pdfs")
    assert got == Path("/data/user_pdfs")


def test_precedence_falls_back_to_project_default(tmp_path: Path) -> None:
    """The default is the per-paper store root; the type tree is built inside."""
    ws, proj = _workspace(tmp_path, None)
    assert pdf_dir.resolve_for_workspace(ws, proj, None) == proj / "papers"


def test_empty_base_pdf_dir_is_not_an_override(tmp_path: Path) -> None:
    # A config that ships `base_pdf_dir: ""` — that must mean "unset",
    # not "use the empty path".
    ws, proj = _workspace(tmp_path, "")
    assert pdf_dir.resolve_for_workspace(ws, proj, None) == proj / "papers"


# --- the review-level override is NOT trusted ------------------------------
# `dirs.base_pdf_dir` is settable by any review owner through
# PUT/PATCH /reviews/{id}/config, and it becomes `--output-dir` for a step that
# does mkdir(parents=True) and writes as the server user. It used to be taken
# verbatim, which made it an arbitrary-directory write. It must go through the
# SAME validator that guards the account-level setting.

@pytest.mark.parametrize(
    "bad",
    [
        "/etc/cron.d",
        "/root/pdfs",
        "/home/someone/.ssh",
        "/home/someone/.claude/pdfs",
        "relative/not/absolute",
    ],
)
def test_unsafe_review_override_is_ignored(tmp_path: Path, bad: str) -> None:
    ws, proj = _workspace(tmp_path, bad)
    got = pdf_dir.resolve_for_workspace(ws, proj, None)
    assert got == proj / "papers", f"{bad} must not be honored"


def test_unsafe_review_override_degrades_to_the_account_setting(tmp_path: Path) -> None:
    """Falls back rather than raising.

    A config.yaml written before this gate existed must not make an existing
    review permanently unrunnable — the value is dropped, not fatal.
    """
    ws, proj = _workspace(tmp_path, "/etc")
    assert pdf_dir.resolve_for_workspace(ws, proj, "/data/user_pdfs") == Path(
        "/data/user_pdfs"
    )


def test_safe_review_override_is_still_honored_and_normalized(tmp_path: Path) -> None:
    ws, proj = _workspace(tmp_path, str(tmp_path / "elsewhere" / ".." / "pdfs"))
    assert pdf_dir.resolve_for_workspace(ws, proj, None) == tmp_path / "pdfs"


# --- the two PDF steps must move together ----------------------------------

def test_fulltext_steps_thread_the_dir_into_both_steps(tmp_path: Path) -> None:
    """Download and convert must agree, and `produces` must follow.

    If only the download is redirected, convert reads an empty default dir and
    hard-errors, or `verify_produces` fails on a directory nothing wrote to.
    """
    from app.services import runner

    ws, proj = _workspace(tmp_path, None)
    steps = runner._fulltext_steps(ws, user_pdf_dir="/data/user_pdfs")
    download, convert = steps[0], steps[1]

    assert "--output-dir" in download.args
    assert download.args[download.args.index("--output-dir") + 1] == "/data/user_pdfs"
    assert "--papers-dir" in convert.args
    assert convert.args[convert.args.index("--papers-dir") + 1] == "/data/user_pdfs"
    # produces must point at the redirected dir, not the project default.
    assert download.produces == ["/data/user_pdfs/"]


def test_fulltext_steps_pass_the_flags_even_at_the_default(tmp_path: Path) -> None:
    """The resolved dir is passed ALWAYS, including when it is the default.

    Omitting the flag here used to look harmless — same value either way — but it
    handed the decision back to the script, which reads the raw, unvalidated
    `dirs.base_pdf_dir` from config.yaml itself (`paper_store.papers_root`).
    See the degraded case below for why that inverted the whole gate.
    """
    from app.services import runner

    ws, proj = _workspace(tmp_path, None)
    download, convert, install = runner._fulltext_steps(ws)[:3]
    default = str(proj / "papers")

    assert download.args[download.args.index("--output-dir") + 1] == default
    assert convert.args[convert.args.index("--papers-dir") + 1] == default
    assert install.args[install.args.index("--papers-dir") + 1] == default
    assert download.produces == [default + "/"]


def test_unsafe_override_is_overridden_on_the_command_line(tmp_path: Path) -> None:
    """Rejecting a value must not be what causes the value to be used.

    `resolve_for_workspace` degrades an unsafe `dirs.base_pdf_dir` to the project
    default, which makes it EQUAL to the default — so a `!= default` guard around
    the flags omitted them in exactly the case that needed them most, and
    `download_pdfs.py` then re-read `/etc` straight out of config.yaml and
    mkdir(parents=True)'d it. Every step must be told explicitly where the store
    is, so the config value it would otherwise read is never consulted.
    """
    from app.services import runner

    ws, proj = _workspace(tmp_path, "/etc")
    download, convert, install = runner._fulltext_steps(ws)[:3]
    default = str(proj / "papers")

    for step, flag in ((download, "--output-dir"), (convert, "--papers-dir"),
                       (install, "--papers-dir")):
        assert flag in step.args, f"{step.id} must be told the store location"
        assert step.args[step.args.index(flag) + 1] == default
        assert "/etc" not in step.args, f"{step.id} must not point at /etc"
