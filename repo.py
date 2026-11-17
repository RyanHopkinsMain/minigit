"""Finding and creating repositories, plus a tiny git-config reader."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


class GitError(Exception):
    """A user-facing error: bad object id, missing repository, dirty tree..."""


@dataclass(frozen=True)
class Repo:
    """A repository with a working tree and a ``.git`` directory inside it."""

    worktree: Path

    @property
    def gitdir(self) -> Path:
        return self.worktree / ".git"

    @classmethod
    def find(cls, start: Path | None = None) -> "Repo":
        """Walk up from ``start`` (default: cwd) until a ``.git`` directory turns up."""
        path = (start or Path.cwd()).resolve()
        for candidate in (path, *path.parents):
            if (candidate / ".git").is_dir():
                return cls(candidate)
        raise GitError("not a git repository (or any of the parent directories): .git")

    @classmethod
    def init(cls, path: Path) -> "Repo":
        """Create the minimal on-disk layout that real git accepts."""
        path = path.resolve()
        gitdir = path / ".git"
        if gitdir.exists():
            raise GitError(f"{gitdir} already exists")
        for sub in ("objects", "refs/heads", "refs/tags"):
            (gitdir / sub).mkdir(parents=True)
        (gitdir / "HEAD").write_text("ref: refs/heads/main\n")
        (gitdir / "config").write_text(
            "[core]\n"
            "\trepositoryformatversion = 0\n"
            "\tfilemode = true\n"
            "\tbare = false\n"
        )
        (gitdir / "description").write_text(
            "Unnamed repository; edit this file 'description' to name the repository.\n"
        )
        return cls(path)

    def config_get(self, key: str) -> str | None:
        """Look up ``section.name`` in .git/config, then ~/.gitconfig."""
        for path in (self.gitdir / "config", Path.home() / ".gitconfig"):
            value = _read_config_value(path, key)
            if value is not None:
                return value
        return None


_SECTION = re.compile(r'^\[\s*([A-Za-z0-9.-]+)(?:\s+"[^"]*")?\s*\]')
_ENTRY = re.compile(r"^([A-Za-z][A-Za-z0-9-]*)\s*=\s*(.*)$")


def _read_config_value(path: Path, key: str) -> str | None:
    """Read one value from a git-style config file (just enough for user.name)."""
    if not path.is_file():
        return None
    want_section, _, want_name = key.lower().rpartition(".")
    section = None
    found = None
    for raw in path.read_text(errors="replace").splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if m := _SECTION.match(line):
            section = m.group(1).lower()
        elif (m := _ENTRY.match(line)) and section == want_section:
            if m.group(1).lower() == want_name:
                found = m.group(2).strip().strip('"')  # last one wins, like git
    return found
