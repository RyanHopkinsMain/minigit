"""Porcelain: the commands people actually type, built on the plumbing modules."""

from __future__ import annotations

import fnmatch
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from . import refs
from .index import IndexEntry, read_index, write_index
from .objects import (Commit, TreeEntry, flatten_tree, hash_object, read_commit,
                      read_object, serialize_tree)
from .repo import GitError, Repo

# --------------------------------------------------------------------------- working tree


def _ignore_patterns(repo: Repo) -> list[str]:
    path = repo.worktree / ".gitignore"
    if not path.is_file():
        return []
    lines = (line.strip() for line in path.read_text().splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def _is_ignored(rel: str, patterns: list[str]) -> bool:
    """A small subset of .gitignore: globs match the basename or the full path,
    and "dir/" matches everything inside dir."""
    name = rel.rsplit("/", 1)[-1]
    for pattern in patterns:
        if pattern.endswith("/"):
            directory = pattern.rstrip("/").lstrip("/")
            if rel.startswith(directory + "/") or f"/{directory}/" in f"/{rel}":
                return True
        elif fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel, pattern.lstrip("/")):
            return True
    return False


def iter_worktree(repo: Repo, under: Path | None = None) -> Iterator[str]:
    """Yield repo-relative POSIX paths of files, skipping .git and ignored files."""
    patterns = _ignore_patterns(repo)
    root = under or repo.worktree
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        for filename in sorted(filenames):
            full = Path(dirpath) / filename
            if full.is_symlink() or not full.is_file():
                continue
            rel = full.relative_to(repo.worktree).as_posix()
            if not _is_ignored(rel, patterns):
                yield rel


def _rel(repo: Repo, path: str | Path) -> str:
    """Turn a user-supplied path (relative to cwd) into a repo-relative one."""
    full = (Path.cwd() / path).resolve()
    try:
        rel = full.relative_to(repo.worktree).as_posix()
    except ValueError:
        raise GitError(f"{path} is outside the repository") from None
    return "" if rel == "." else rel


# --------------------------------------------------------------------------- add / tree


def add(repo: Repo, paths: list[str]) -> list[str]:
    """Stage files. Directories are walked; tracked files that vanished are unstaged."""
    index = read_index(repo)
    staged: list[str] = []
    for arg in paths:
        rel = _rel(repo, arg)  # "" means the repo root
        full = repo.worktree / rel
        if full.is_dir():
            prefix = f"{rel}/" if rel else ""
            for path in iter_worktree(repo, full):
                staged.append(_stage_file(repo, index, path))
            for path in [p for p in index if p.startswith(prefix)]:
                if not (repo.worktree / path).exists():
                    del index[path]
        elif full.is_file():
            staged.append(_stage_file(repo, index, rel))
        elif rel in index:
            del index[rel]  # `add` of a deleted file stages the deletion
        else:
            raise GitError(f"pathspec {arg!r} did not match any files")
    write_index(repo, index)
    return staged


def _stage_file(repo: Repo, index: dict[str, IndexEntry], path: str) -> str:
    file = repo.worktree / path
    old = index.get(path)
    if old and old.stat_matches(file):
        return path  # untouched since last add: skip the re-hash
    oid = hash_object(file.read_bytes(), "blob", repo)
    index[path] = IndexEntry.from_file(file, path, oid)
    return path


def write_tree(repo: Repo, entries: dict[str, IndexEntry] | None = None) -> str:
    """Turn the flat list of index paths into nested tree objects; return the root."""
    entries = read_index(repo) if entries is None else entries
    root: dict = {}
    for entry in entries.values():
        *dirs, filename = entry.path.split("/")
        node = root
        for d in dirs:
            node = node.setdefault(d, {})
        node[filename] = entry

    def build(node: dict) -> str:
        tree = []
        for name, child in node.items():
            if isinstance(child, dict):
                tree.append(TreeEntry("40000", name, build(child)))
            else:
                tree.append(TreeEntry(f"{child.mode:o}", name, child.oid))
        return hash_object(serialize_tree(tree), "tree", repo)

    return build(root)


# --------------------------------------------------------------------------- commit / log


def _identity(repo: Repo, role: str) -> str:
    name = os.environ.get(f"GIT_{role}_NAME") or repo.config_get("user.name") or "Mini Git"
    email = os.environ.get(f"GIT_{role}_EMAIL") or repo.config_get("user.email") or "minigit@localhost"
    if date := os.environ.get(f"GIT_{role}_DATE"):  # "<unix seconds> <+hhmm>"
        stamp = date.lstrip("@")
    else:
        offset = datetime.now().astimezone().utcoffset() or timedelta()
        minutes = int(offset.total_seconds() // 60)
        sign = "+" if minutes >= 0 else "-"
        stamp = f"{int(time.time())} {sign}{abs(minutes) // 60:02d}{abs(minutes) % 60:02d}"
    return f"{name} <{email}> {stamp}"


def head_commit(repo: Repo) -> str | None:
    try:
        return refs.rev_parse(repo, "HEAD")
    except GitError:
        return None


def commit(repo: Repo, message: str) -> str:
    index = read_index(repo)
    if not index:
        raise GitError("nothing to commit (use `minigit add`)")
    tree = write_tree(repo, index)
    parent = head_commit(repo)
    if parent and read_commit(repo, parent).tree == tree:
        raise GitError("nothing to commit, working tree clean")
    message = message if message.endswith("\n") else message + "\n"
    new = Commit(tree=tree, parents=[parent] if parent else [],
                 author=_identity(repo, "AUTHOR"), committer=_identity(repo, "COMMITTER"),
                 message=message)
    oid = hash_object(new.serialize(), "commit", repo)
    refs.update_head(repo, oid)
    return oid


def format_signature(signature: str) -> tuple[str, str]:
    """Split "Name <email> 1700000000 -0600" into ("Name <email>", git-style date)."""
    who, _, rest = signature.rpartition("> ")
    seconds, tz = rest.split(" ")
    sign = -1 if tz[0] == "-" else 1
    offset = timezone(sign * timedelta(hours=int(tz[1:3]), minutes=int(tz[3:5])))
    dt = datetime.fromtimestamp(int(seconds), offset)
    return who + ">", f"{dt:%a %b} {dt.day} {dt:%H:%M:%S %Y} {tz}"


def log(repo: Repo, start: str = "HEAD", limit: int | None = None) -> list[tuple[str, Commit]]:
    """Walk first parents from ``start``."""
    history = []
    oid: str | None = refs.rev_parse(repo, start)
    while oid and (limit is None or len(history) < limit):
        c = read_commit(repo, oid)
        history.append((oid, c))
        oid = c.parents[0] if c.parents else None
    return history


# --------------------------------------------------------------------------- status


@dataclass
class Status:
    branch: str | None
    staged: list[tuple[str, str]] = field(default_factory=list)  # (kind, path)
    unstaged: list[tuple[str, str]] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.staged or self.unstaged or self.untracked)


def _head_files(repo: Repo) -> dict[str, TreeEntry]:
    head = head_commit(repo)
    return flatten_tree(repo, read_commit(repo, head).tree) if head else {}


def status(repo: Repo) -> Status:
    index = read_index(repo)
    head = _head_files(repo)
    result = Status(branch=refs.current_branch(repo))

    for path in sorted(set(index) | set(head)):
        if path not in head:
            result.staged.append(("new file", path))
        elif path not in index:
            result.staged.append(("deleted", path))
        elif head[path].oid != index[path].oid or head[path].mode != f"{index[path].mode:o}":
            result.staged.append(("modified", path))

    for path, entry in sorted(index.items()):
        file = repo.worktree / path
        if not file.is_file():
            result.unstaged.append(("deleted", path))
        elif not entry.stat_matches(file) and hash_object(file.read_bytes()) != entry.oid:
            result.unstaged.append(("modified", path))

    # Like git, collapse an untracked directory to "dir/" when nothing in it is tracked.
    tracked_dirs = {p.rsplit("/", i)[0] for p in index for i in range(1, p.count("/") + 1)}
    untracked = set()
    for path in iter_worktree(repo):
        if path in index:
            continue
        parts = path.split("/")
        for depth in range(1, len(parts)):
            directory = "/".join(parts[:depth])
            if directory not in tracked_dirs:
                untracked.add(directory + "/")
                break
        else:
            untracked.add(path)
    result.untracked = sorted(untracked)
    return result


# --------------------------------------------------------------------------- branches


def create_branch(repo: Repo, name: str, start: str = "HEAD") -> str:
    if not name or name.startswith("-") or ".." in name or name.endswith("/"):
        raise GitError(f"{name!r} is not a valid branch name")
    if refs.read_ref(repo, f"refs/heads/{name}"):
        raise GitError(f"a branch named {name!r} already exists")
    oid = refs.rev_parse(repo, start)
    refs.update_ref(repo, f"refs/heads/{name}", oid)
    return oid


def checkout(repo: Repo, target: str, create: bool = False) -> str:
    """Switch branches (or detach at a commit), rewriting the working tree."""
    if create:
        create_branch(repo, target)
        refs.set_head(repo, branch=target)
        return refs.rev_parse(repo, "HEAD")

    is_branch = refs.read_ref(repo, f"refs/heads/{target}") is not None
    oid = refs.rev_parse(repo, target)
    obj_type, _ = read_object(repo, oid)
    if obj_type != "commit":
        raise GitError(f"{target} is a {obj_type}, not a commit")

    st = status(repo)
    if st.staged or st.unstaged:
        raise GitError("you have uncommitted changes; commit them before switching")
    current = _head_files(repo)
    wanted = flatten_tree(repo, read_commit(repo, oid).tree)
    for path in wanted:
        if path not in current and (repo.worktree / path).exists():
            raise GitError(f"untracked file {path!r} would be overwritten by checkout")

    for path in current.keys() - wanted.keys():
        file = repo.worktree / path
        file.unlink(missing_ok=True)
        for parent in file.parents:  # tidy up directories we emptied
            if parent == repo.worktree or any(parent.iterdir()):
                break
            parent.rmdir()

    index: dict[str, IndexEntry] = {}
    for path, entry in wanted.items():
        file = repo.worktree / path
        if path not in current or current[path] != entry:
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(read_object(repo, entry.oid)[1])
            file.chmod(0o755 if entry.mode == "100755" else 0o644)
        index[path] = IndexEntry.from_file(file, path, entry.oid)
    write_index(repo, index)

    if is_branch:
        refs.set_head(repo, branch=target)
    else:
        refs.set_head(repo, oid=oid)
    return oid
