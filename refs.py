"""References: HEAD, branches, tags, and turning names into object ids."""

from __future__ import annotations

import os

from .objects import find_objects
from .repo import GitError, Repo


def _packed_refs(repo: Repo) -> dict[str, str]:
    """Real git moves refs into .git/packed-refs on gc, so read them too."""
    path = repo.gitdir / "packed-refs"
    refs: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            if line and line[0] not in "#^":
                oid, _, name = line.partition(" ")
                refs[name] = oid
    return refs


def read_ref(repo: Repo, ref: str, depth: int = 0) -> str | None:
    """Follow ``ref`` (e.g. "HEAD" or "refs/heads/main") to an object id."""
    if depth > 5:
        raise GitError(f"symbolic ref loop at {ref}")
    path = repo.gitdir / ref
    if path.is_file():
        value = path.read_text().strip()
        if value.startswith("ref: "):
            return read_ref(repo, value[5:], depth + 1)
        return value
    return _packed_refs(repo).get(ref)


def update_ref(repo: Repo, ref: str, oid: str) -> None:
    path = repo.gitdir / ref
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".lock")
    tmp.write_text(oid + "\n")
    os.replace(tmp, path)


def head_target(repo: Repo) -> str | None:
    """The ref HEAD points at ("refs/heads/main"), or None when HEAD is detached."""
    value = (repo.gitdir / "HEAD").read_text().strip()
    return value[5:] if value.startswith("ref: ") else None


def current_branch(repo: Repo) -> str | None:
    target = head_target(repo)
    return target.removeprefix("refs/heads/") if target else None


def set_head(repo: Repo, *, branch: str | None = None, oid: str | None = None) -> None:
    content = f"ref: refs/heads/{branch}\n" if branch else f"{oid}\n"
    (repo.gitdir / "HEAD").write_text(content)


def update_head(repo: Repo, oid: str) -> None:
    """Move whatever HEAD points at (a branch, or HEAD itself if detached)."""
    target = head_target(repo)
    if target:
        update_ref(repo, target, oid)
    else:
        set_head(repo, oid=oid)


def list_branches(repo: Repo) -> list[str]:
    heads = repo.gitdir / "refs" / "heads"
    names = {p.relative_to(heads).as_posix() for p in heads.rglob("*") if p.is_file()}
    names |= {r.removeprefix("refs/heads/") for r in _packed_refs(repo) if r.startswith("refs/heads/")}
    return sorted(names)


def rev_parse(repo: Repo, name: str) -> str:
    """Resolve HEAD, a branch, a tag, or an (abbreviated) object id."""
    candidates = [name] if name == "HEAD" or name.startswith("refs/") else []
    candidates += [f"refs/heads/{name}", f"refs/tags/{name}"]
    for ref in candidates:
        if oid := read_ref(repo, ref):
            return oid
    matches = find_objects(repo, name)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise GitError(f"short object id {name} is ambiguous")
    if name == "HEAD":
        raise GitError("HEAD does not point at a commit yet (make your first commit)")
    raise GitError(f"unknown revision {name!r}")
