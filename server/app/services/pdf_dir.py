"""Validation + resolution for the user-configurable PDF download directory.

This is the FIRST user-supplied filesystem path the server writes to — every
other write target is server-derived (see workspace.py / runner.py) — so the
validation here is the whole security boundary. ``download_pdfs.py`` does
``mkdir(parents=True)`` and writes as the server user, so an unchecked value
like ``/etc`` or ``~/.ssh`` would be reachable.

Policy, local instance (one operator on their own machine): absolute paths the
server user can write are allowed, EXCEPT

* system roots (``/etc``, ``/usr``, …) and credential dot-dirs (``.ssh``,
  ``.config``, ``.claude``, ``.codex``, ``.local``, …);
* the server's own trees: anything under ``DATA_ROOT`` (every review's
  workspace — another user's included) or ``REPO_ROOT`` (the code it runs);
* any existing pipeline project (``…/projects/<slug>/`` with ``data/`` or
  ``papers/``), which would let one review write into another corpus;
* another user's home directory (``/home/<someone-else>``).

Windows paths are translated for WSL so ``C:\\Users\\me\\PDFs`` just works
when typed into a Linux-side app.

Policy, PUBLIC_MODE: custom PDF folders are DISABLED. On a hosted instance the
"user" choosing the path is a stranger and the filesystem is the operator's;
there is no folder outside the review's own workspace that a stranger should
direct the server to write into. Every review then uses its default store
(``projects/<slug>/papers`` in its workspace), which is already per-review and
per-user. A per-user root was considered and rejected: it would add a second
storage layout to support for no benefit to the hosted user.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

# Roots we refuse to write into. Matching is on the RESOLVED path, so symlink
# and ``..`` tricks can't sneak past. ``/mnt`` itself is absent on purpose:
# /mnt/c/... is legitimate on WSL and is handled by a warning, not a rejection.
_DENY_ROOTS = (
    "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/boot",
    "/dev", "/proc", "/sys", "/var/lib", "/var/run", "/run", "/root",
)

# Dot-directories that hold credentials/config; never a sane place for PDFs.
_DENY_NAMES = (".ssh", ".gnupg", ".aws", ".config", ".claude", ".codex", ".local")

_PUBLIC_DISABLED = (
    "Custom PDF folders are disabled on this hosted instance — each review "
    "keeps its PDFs in its own workspace."
)

# Parents of per-user home directories.
_HOME_PARENTS = ("/home", "/Users")

_WIN_DRIVE_RE = re.compile(r"^([A-Za-z]):[\\/]")

logger = logging.getLogger(__name__)


def is_wsl() -> bool:
    """True when running under WSL (either WSL1 or WSL2)."""
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def translate_windows_path(raw: str) -> str:
    r"""Map a Windows path to its WSL mount point.

    ``C:\Users\me\PDFs`` -> ``/mnt/c/Users/me/PDFs``. Returns *raw* unchanged
    when it isn't a drive-letter path. Backslash-separated paths are converted
    even off WSL so the value round-trips predictably (a Windows-only path is
    still rejected later by the absolute-path check if the mount is absent).
    """
    s = (raw or "").strip().strip('"').strip("'")
    m = _WIN_DRIVE_RE.match(s)
    if not m:
        # A UNC path (\\server\share) has no WSL equivalent. Return it AS-IS so
        # the caller still sees the leading backslashes and rejects it —
        # normalizing here would turn it into a plausible-looking POSIX path.
        return s
    drive = m.group(1).lower()
    rest = PureWindowsPath(s).as_posix()
    rest = rest[2:] if len(rest) > 2 and rest[1] == ":" else rest
    return f"/mnt/{drive}{rest}"


@dataclass(frozen=True)
class ValidatedPdfDir:
    path: str            # the normalized, absolute POSIX path to store
    warning: str | None  # non-fatal advice to surface in the UI


def _under(path: str, root: Path | str | None) -> bool:
    if not root:
        return False
    r = Path(root).resolve(strict=False).as_posix()
    return path == r or path.startswith(r.rstrip("/") + "/")


def _inside_pipeline_project(resolved: Path) -> bool:
    """True when *resolved* is inside an existing pipeline project directory
    (``…/projects/<slug>/`` holding ``data/`` or ``papers/``)."""
    parts = resolved.parts
    for i, part in enumerate(parts[:-1]):
        if part != "projects":
            continue
        candidate = Path(*parts[: i + 2])
        if (candidate / "data").is_dir() or (candidate / "papers").is_dir():
            return True
    return False


def _other_users_home(rs: str) -> bool:
    """True when *rs* is inside a home directory that is not the server user's."""
    home = Path.home().resolve(strict=False).as_posix()
    for parent in _HOME_PARENTS:
        if rs.startswith(parent + "/"):
            owner_home = f"{parent}/{rs[len(parent) + 1:].split('/')[0]}"
            if not (home == owner_home or home.startswith(owner_home + "/")):
                return True
    return False


def validate_pdf_dir(raw: str | None, *, user_id: str | None = None) -> ValidatedPdfDir | None:
    """Normalize and vet a user-supplied PDF directory.

    Returns ``None`` for an empty value (meaning "use the per-review default").
    Raises ``ValueError`` with a user-facing message when the path is unusable.
    *user_id* names who is asking; it is part of the signature so a per-user
    policy can be added without touching every caller.
    """
    from ..config import settings

    if raw is None:
        return None
    s = translate_windows_path(raw)
    if not s:
        return None
    if settings.PUBLIC_MODE:
        raise ValueError(_PUBLIC_DISABLED)

    if s.startswith("~"):
        s = os.path.expanduser(s)
    if s.startswith("\\\\") or _WIN_DRIVE_RE.match(s):
        raise ValueError(
            "Network (UNC) paths aren't supported. Map the share to a drive "
            "letter in Windows, or use a local folder."
        )
    if not s.startswith("/"):
        raise ValueError(
            "Enter an absolute path — e.g. /home/you/pdfs, or C:\\Users\\you\\PDFs "
            "on Windows."
        )

    # Resolve to defeat symlinks and ".." before any comparison. strict=False so
    # a not-yet-created directory is still checkable.
    resolved = Path(s).resolve(strict=False)
    rs = resolved.as_posix()

    if rs == "/":
        raise ValueError("Refusing to use the filesystem root as a PDF folder.")
    for bad in _DENY_ROOTS:
        if rs == bad or rs.startswith(bad + "/"):
            raise ValueError(
                f"{rs} is a system directory. Choose a folder in your home "
                "directory or on a data drive."
            )
    if any(part in _DENY_NAMES for part in resolved.parts):
        raise ValueError(
            f"{rs} looks like a private config/credentials folder. Choose "
            "somewhere else for PDFs."
        )
    if _under(rs, settings.DATA_ROOT) or _under(rs, settings.REPO_ROOT):
        raise ValueError(
            f"{rs} is inside the studio's own files. Choose a folder outside "
            "the application and its data directory."
        )
    if _inside_pipeline_project(resolved):
        raise ValueError(
            f"{rs} is inside an existing review project. Choose a separate "
            "folder for PDFs."
        )
    if _other_users_home(rs):
        raise ValueError(
            f"{rs} is in another user's home directory. Choose a folder in "
            "your own home directory or on a data drive."
        )

    warning = None
    # /mnt/<drive> on WSL is the Windows filesystem: symlinks (used by PDF
    # conversion) fail there, and IO is much slower. Warn, don't block —
    # downloading to a big Windows drive is a legitimate thing to want.
    if re.match(r"^/mnt/[a-z](/|$)", rs):
        warning = (
            "This folder is on the Windows drive. PDFs will download fine, but "
            "PDF→text conversion needs symlinks, which Windows drives don't "
            "support — and disk access is much slower. A folder under your "
            "Linux home works better."
        )
    elif not is_wsl() and rs.startswith("/mnt/"):
        warning = "Make sure this drive is mounted before running the pipeline."

    return ValidatedPdfDir(path=rs, warning=warning)


def resolve_for_workspace(
    workspace: Path, project_dir: Path, user_pdf_dir: str | None
) -> Path:
    """Where this review's papers live, applying the precedence chain.

    review ``dirs.base_pdf_dir`` -> account ``user.pdf_dir`` -> the default
    ``projects/<slug>/papers``. The review-level key wins because it's the more
    specific setting, matching how ``llm_prefs`` resolves the LLM provider.

    The returned path is the ROOT of the per-paper store: the pipeline builds
    ``<root>/<Type>/<stem>/`` underneath it, one folder per paper (see
    ``paper_store.py``). A user-configured directory is therefore still a plain
    directory they chose — it just gains structure inside.

    NOTE: ``dirs.base_pdf_dir`` was previously honored by the old PDF-reorganize script,
    evidence.py and the old GUI but NOT by the downloader (download_pdfs
    never read it), so a configured directory was half-applied. Resolving it in
    one place and passing it explicitly to every PDF step fixes that.

    SECURITY: the review-level value goes through :func:`validate_pdf_dir` here,
    exactly like the account-level one does in ``routers/auth.py``. It used to be
    trusted verbatim, which made ``PUT/PATCH /reviews/{id}/config`` an
    arbitrary-directory write as the server user — the value becomes
    ``--output-dir``/``--papers-dir`` for steps that ``mkdir(parents=True)``, so
    ``~/.ssh`` or ``/root`` were reachable. ``routers/reviews._validate_config``
    now rejects a bad value at write time; this is the read-side backstop for a
    ``config.yaml`` written before that gate existed (or edited on disk), and it
    DEGRADES rather than raising — a stored bad value must not make an existing
    review permanently unrunnable.
    """
    import yaml  # lazy: keeps module import light for non-pipeline callers

    from ..config import settings

    if settings.PUBLIC_MODE:
        # Custom folders are disabled on a hosted instance (see module
        # docstring) — including ones stored before PUBLIC_MODE was turned on.
        return project_dir / "papers"
    cfg_path = workspace / "config.yaml"
    try:
        cfg = yaml.safe_load(cfg_path.read_text()) or {}
        configured = ((cfg.get("dirs") or {}).get("base_pdf_dir") or "").strip()
    except (OSError, yaml.YAMLError):
        configured = ""
    if configured:
        try:
            checked = validate_pdf_dir(configured)
        except ValueError:
            logger.warning(
                "Ignoring unsafe dirs.base_pdf_dir %r in %s; falling back to the "
                "account/default papers directory.",
                configured,
                cfg_path,
            )
            checked = None
        if checked is not None:
            return Path(checked.path)
    if user_pdf_dir:
        try:
            checked = validate_pdf_dir(user_pdf_dir)
        except ValueError:
            logger.warning("Ignoring unsafe account pdf_dir %r.", user_pdf_dir)
            checked = None
        if checked is not None:
            return Path(checked.path)
    return project_dir / "papers"


def writable_or_reason(path: str) -> str | None:
    """Why *path* can't be used, or None if it looks usable.

    Checked at save time so the user finds out in Settings rather than three
    steps into a pipeline run. Creating the directory is left to the pipeline.
    """
    p = Path(path)
    probe = p
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    if not probe.is_dir():
        return f"{probe} exists but is not a directory."
    if not os.access(probe, os.W_OK):
        return f"No permission to write in {probe}."
    return None
