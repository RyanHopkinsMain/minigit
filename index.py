"""The index (a.k.a. staging area): a binary file at .git/index.

Layout (version 2), all integers big-endian::

    "DIRC" | version:u32 | entry count:u32
    entries, sorted by path, each:
        ctime_s ctime_ns mtime_s mtime_ns dev ino mode uid gid size  (10 x u32)
        sha1 (20 bytes) | flags:u16 (low 12 bits = path length) | path | 1-8 NULs
    optional extensions (git's TREE cache etc.; we skip them on read)
    SHA-1 of everything above

The stat fields let `status` skip re-hashing files that haven't been touched.
"""

from __future__ import annotations

import hashlib
import os
import struct
from dataclasses import dataclass
from pathlib import Path

from .repo import GitError, Repo

_FIXED = struct.Struct(">10I20sH")  # 62 bytes
_MASK32 = 0xFFFFFFFF


@dataclass
class IndexEntry:
    path: str
    oid: str
    mode: int = 0o100644
    size: int = 0
    ctime_s: int = 0
    ctime_ns: int = 0
    mtime_s: int = 0
    mtime_ns: int = 0
    dev: int = 0
    ino: int = 0
    uid: int = 0
    gid: int = 0

    @classmethod
    def from_file(cls, file: Path, path: str, oid: str) -> "IndexEntry":
        st = file.stat()
        return cls(
            path=path,
            oid=oid,
            mode=0o100755 if st.st_mode & 0o111 else 0o100644,
            size=st.st_size & _MASK32,
            ctime_s=int(st.st_ctime) & _MASK32,
            ctime_ns=st.st_ctime_ns % 1_000_000_000,
            mtime_s=int(st.st_mtime) & _MASK32,
            mtime_ns=st.st_mtime_ns % 1_000_000_000,
            dev=st.st_dev & _MASK32,
            ino=st.st_ino & _MASK32,
            uid=st.st_uid & _MASK32,
            gid=st.st_gid & _MASK32,
        )

    def stat_matches(self, file: Path) -> bool:
        """Cheap "probably unchanged" check, the same idea git uses."""
        st = file.stat()
        return (
            st.st_size & _MASK32 == self.size
            and int(st.st_mtime) & _MASK32 == self.mtime_s
            and st.st_mtime_ns % 1_000_000_000 == self.mtime_ns
        )


def read_index(repo: Repo) -> dict[str, IndexEntry]:
    path = repo.gitdir / "index"
    if not path.is_file():
        return {}
    data = path.read_bytes()
    body, checksum = data[:-20], data[-20:]
    if hashlib.sha1(body).digest() != checksum:
        raise GitError("index file is corrupt (bad checksum)")
    signature, version, count = struct.unpack(">4sII", body[:12])
    if signature != b"DIRC" or version not in (2, 3):
        raise GitError(f"unsupported index (signature {signature!r}, version {version})")

    entries: dict[str, IndexEntry] = {}
    offset = 12
    for _ in range(count):
        (ctime_s, ctime_ns, mtime_s, mtime_ns, dev, ino, mode, uid, gid, size,
         sha, flags) = _FIXED.unpack_from(body, offset)
        header_len = _FIXED.size
        if version == 3 and flags & 0x4000:  # "extended" flag adds 2 bytes
            header_len += 2
        name_start = offset + header_len
        name_end = body.index(b"\0", name_start)
        name = body[name_start:name_end].decode()
        entries[name] = IndexEntry(name, sha.hex(), mode, size, ctime_s, ctime_ns,
                                   mtime_s, mtime_ns, dev, ino, uid, gid)
        offset += (header_len + len(name.encode()) + 8) & ~7  # pad with 1-8 NULs
    return entries


def write_index(repo: Repo, entries: dict[str, IndexEntry]) -> None:
    parts = [struct.pack(">4sII", b"DIRC", 2, len(entries))]
    for path in sorted(entries, key=lambda p: p.encode()):
        e = entries[path]
        name = path.encode()
        packed = _FIXED.pack(e.ctime_s, e.ctime_ns, e.mtime_s, e.mtime_ns, e.dev,
                             e.ino, e.mode, e.uid, e.gid, e.size,
                             bytes.fromhex(e.oid), min(len(name), 0xFFF))
        record = packed + name
        record += b"\0" * (((len(record) + 8) & ~7) - len(record))
        parts.append(record)
    body = b"".join(parts)
    tmp = repo.gitdir / "index.lock"
    tmp.write_bytes(body + hashlib.sha1(body).digest())
    os.replace(tmp, repo.gitdir / "index")
