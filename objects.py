"""The object database: blobs, trees and commits stored as zlib'd loose files.

Every git object is stored as::

    zlib( b"<type> <size>\\0" + content )

at ``.git/objects/<first 2 hex chars>/<remaining 38>``, where the file name is the
SHA-1 of the uncompressed bytes. That is the whole trick behind content addressing.
"""

from __future__ import annotations

import hashlib
import os
import zlib
from dataclasses import dataclass, field

from .repo import GitError, Repo

OBJECT_TYPES = ("blob", "tree", "commit", "tag")


def hash_object(data: bytes, obj_type: str = "blob", repo: Repo | None = None) -> str:
    """Return the object id for ``data``; if ``repo`` is given, also store it."""
    if obj_type not in OBJECT_TYPES:
        raise GitError(f"invalid object type {obj_type!r}")
    store = f"{obj_type} {len(data)}".encode() + b"\0" + data
    oid = hashlib.sha1(store).hexdigest()
    if repo is not None:
        path = repo.gitdir / "objects" / oid[:2] / oid[2:]
        if not path.exists():
            path.parent.mkdir(exist_ok=True)
            tmp = path.parent / f"tmp_{oid[2:]}_{os.getpid()}"
            tmp.write_bytes(zlib.compress(store))
            os.chmod(tmp, 0o444)  # git keeps objects read-only
            os.replace(tmp, path)
    return oid


def read_object(repo: Repo, oid: str) -> tuple[str, bytes]:
    """Return ``(type, content)`` for a full object id."""
    path = repo.gitdir / "objects" / oid[:2] / oid[2:]
    if not path.is_file():
        if any((repo.gitdir / "objects" / "pack").glob("*.pack")):
            raise GitError(f"object {oid} not found (packfiles aren't supported, try `git unpack-objects`)")
        raise GitError(f"object {oid} not found")
    raw = zlib.decompress(path.read_bytes())
    header, _, content = raw.partition(b"\0")
    obj_type, size = header.decode().split(" ")
    if int(size) != len(content):
        raise GitError(f"object {oid} is corrupt: size mismatch")
    return obj_type, content


def find_objects(repo: Repo, prefix: str) -> list[str]:
    """All loose object ids starting with ``prefix`` (at least 4 hex chars)."""
    prefix = prefix.lower()
    if len(prefix) < 4 or any(c not in "0123456789abcdef" for c in prefix):
        return []
    directory = repo.gitdir / "objects" / prefix[:2]
    if not directory.is_dir():
        return []
    return sorted(prefix[:2] + p.name for p in directory.iterdir() if p.name.startswith(prefix[2:]) and len(p.name) == 38)


# --------------------------------------------------------------------------- trees

TREE_MODE = "40000"


@dataclass(frozen=True)
class TreeEntry:
    mode: str  # "100644", "100755", "120000", "40000" or "160000" -- octal, no padding
    name: str
    oid: str

    @property
    def is_tree(self) -> bool:
        return self.mode == TREE_MODE

    @property
    def kind(self) -> str:
        return {"40000": "tree", "160000": "commit"}.get(self.mode, "blob")


def parse_tree(data: bytes) -> list[TreeEntry]:
    """Trees are a run of ``<mode> <name>\\0<20 raw sha bytes>`` records."""
    entries, i = [], 0
    while i < len(data):
        space = data.index(b" ", i)
        nul = data.index(b"\0", space)
        mode = data[i:space].decode()
        name = data[space + 1 : nul].decode()
        oid = data[nul + 1 : nul + 21].hex()
        entries.append(TreeEntry(mode, name, oid))
        i = nul + 21
    return entries


def tree_sort_key(entry: TreeEntry) -> bytes:
    # Git compares directory names as though they ended in "/". So the file "a.txt"
    # sorts *before* the directory "a" ('.' is 0x2e, '/' is 0x2f), while "a0" sorts
    # after it. A plain name sort gets this wrong, and then `git fsck` complains.
    name = entry.name.encode()
    return name + b"/" if entry.is_tree else name


def serialize_tree(entries: list[TreeEntry]) -> bytes:
    return b"".join(
        f"{e.mode} {e.name}".encode() + b"\0" + bytes.fromhex(e.oid)
        for e in sorted(entries, key=tree_sort_key)
    )


def read_tree(repo: Repo, oid: str) -> list[TreeEntry]:
    obj_type, data = read_object(repo, oid)
    if obj_type == "commit":
        return read_tree(repo, parse_commit(data).tree)
    if obj_type != "tree":
        raise GitError(f"{oid} is a {obj_type}, not a tree")
    return parse_tree(data)


def flatten_tree(repo: Repo, oid: str, prefix: str = "") -> dict[str, TreeEntry]:
    """Map every file path under a tree to its entry, recursing into subtrees."""
    files: dict[str, TreeEntry] = {}
    for entry in read_tree(repo, oid):
        path = f"{prefix}{entry.name}"
        if entry.is_tree:
            files.update(flatten_tree(repo, entry.oid, path + "/"))
        else:
            files[path] = entry
    return files


# --------------------------------------------------------------------------- commits


@dataclass
class Commit:
    tree: str
    parents: list[str] = field(default_factory=list)
    author: str = ""
    committer: str = ""
    message: str = ""

    def serialize(self) -> bytes:
        lines = [f"tree {self.tree}"]
        lines += [f"parent {p}" for p in self.parents]
        lines += [f"author {self.author}", f"committer {self.committer}"]
        return ("\n".join(lines) + "\n\n" + self.message).encode()


def parse_commit(data: bytes) -> Commit:
    text = data.decode(errors="replace")
    headers, _, message = text.partition("\n\n")
    commit = Commit(tree="", message=message)
    for line in headers.split("\n"):
        if line.startswith(" "):  # continuation of a multi-line header (e.g. gpgsig)
            continue
        key, _, value = line.partition(" ")
        if key == "tree":
            commit.tree = value
        elif key == "parent":
            commit.parents.append(value)
        elif key == "author":
            commit.author = value
        elif key == "committer":
            commit.committer = value
    return commit


def read_commit(repo: Repo, oid: str) -> Commit:
    obj_type, data = read_object(repo, oid)
    if obj_type != "commit":
        raise GitError(f"{oid} is a {obj_type}, not a commit")
    return parse_commit(data)
