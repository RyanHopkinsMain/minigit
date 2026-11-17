"""Tests that check minigit against the real `git` binary.

The strongest test of a git clone is that real git agrees with it: same object
ids, a clean `git fsck`, and `git status` happy with the index we wrote.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from minigit import commands, refs
from minigit.index import read_index
from minigit.objects import flatten_tree, hash_object, read_commit
from minigit.repo import GitError, Repo

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs real git")

IDENTITY = {
    "GIT_AUTHOR_NAME": "Ada Lovelace",
    "GIT_AUTHOR_EMAIL": "ada@example.com",
    "GIT_AUTHOR_DATE": "1700000000 -0600",
    "GIT_COMMITTER_NAME": "Ada Lovelace",
    "GIT_COMMITTER_EMAIL": "ada@example.com",
    "GIT_COMMITTER_DATE": "1700000000 -0600",
}


@pytest.fixture(autouse=True)
def identity(monkeypatch):
    for key, value in IDENTITY.items():
        monkeypatch.setenv(key, value)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout


def minigit(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    root = Path(__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONPATH": str(root)}
    return subprocess.run([sys.executable, "-m", "minigit", *args], cwd=cwd, env=env,
                          capture_output=True, text=True)


def write(root: Path, files: dict[str, str]) -> None:
    for path, content in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(content)


SAMPLE = {
    "README.md": "# hello\n",
    "src/app.py": "print('hi')\n",
    "src/util/strings.py": "def shout(s):\n    return s.upper()\n",
    # The ordering trap: git sorts the directory "a" as "a/", after "a.txt" and "a-b".
    "a.txt": "file a.txt\n",
    "a-b": "file a-b\n",
    "a/inner.txt": "inside a\n",
    "a0": "file a0\n",
}


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Repo:
    monkeypatch.chdir(tmp_path)
    return Repo.init(tmp_path)


def test_hash_object_matches_git(tmp_path):
    for data in [b"", b"hello\n", bytes(range(256)) * 10]:
        f = tmp_path / "blob"
        f.write_bytes(data)
        assert hash_object(data) == git(tmp_path, "hash-object", str(f)).strip()


def test_write_tree_matches_git_byte_for_byte(repo, tmp_path):
    write(tmp_path, SAMPLE)
    (tmp_path / "run.sh").write_text("#!/bin/sh\necho hi\n")
    (tmp_path / "run.sh").chmod(0o755)
    commands.add(repo, ["."])
    ours = commands.write_tree(repo)

    other = tmp_path.parent / "real-git"
    other.mkdir()
    git(other, "init", "-q")
    write(other, SAMPLE)
    (other / "run.sh").write_text("#!/bin/sh\necho hi\n")
    (other / "run.sh").chmod(0o755)
    git(other, "add", ".")
    assert ours == git(other, "write-tree").strip()


def test_real_git_accepts_our_repository(repo, tmp_path):
    write(tmp_path, SAMPLE)
    commands.add(repo, ["."])
    first = commands.commit(repo, "first commit")
    (tmp_path / "README.md").write_text("# hello again\n")
    (tmp_path / "src/app.py").unlink()
    commands.add(repo, ["."])
    second = commands.commit(repo, "second commit\n\nwith a body")

    git(tmp_path, "fsck", "--strict", "--no-dangling")
    assert git(tmp_path, "rev-parse", "HEAD").strip() == second
    assert git(tmp_path, "rev-parse", "HEAD~1").strip() == first
    assert git(tmp_path, "log", "--format=%s|%an|%ae").splitlines() == [
        "second commit|Ada Lovelace|ada@example.com",
        "first commit|Ada Lovelace|ada@example.com",
    ]
    # git reads our binary index and finds nothing to report.
    assert git(tmp_path, "status", "--porcelain") == ""
    assert git(tmp_path, "ls-files", "-s") == minigit(tmp_path, "ls-files", "-s").stdout


def test_we_read_repositories_made_by_git(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    git(tmp_path, "init", "-q", "-b", "main")
    write(tmp_path, SAMPLE)
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "made by git")
    repo = Repo.find()

    head = refs.rev_parse(repo, "HEAD")
    assert head == git(tmp_path, "rev-parse", "HEAD").strip()
    assert read_commit(repo, head).message == "made by git\n"
    assert set(flatten_tree(repo, head)) == set(SAMPLE)
    assert set(read_index(repo)) == set(SAMPLE)
    assert commands.status(repo).clean
    out = minigit(tmp_path, "cat-file", "-p", "HEAD").stdout
    assert out.startswith(f"tree {read_commit(repo, head).tree}")


def test_status_reports_all_three_kinds_of_change(repo, tmp_path):
    write(tmp_path, {"keep.txt": "1\n", "edit.txt": "1\n", "gone.txt": "1\n"})
    commands.add(repo, ["."])
    commands.commit(repo, "base")

    (tmp_path / "edit.txt").write_text("2\n")          # unstaged modification
    (tmp_path / "gone.txt").unlink()                    # unstaged deletion
    write(tmp_path, {"new.txt": "x\n", "junk/a.txt": "a", "junk/b.txt": "b"})
    commands.add(repo, ["new.txt"])                     # staged addition

    st = commands.status(repo)
    assert st.staged == [("new file", "new.txt")]
    assert st.unstaged == [("modified", "edit.txt"), ("deleted", "gone.txt")]
    assert st.untracked == ["junk/"]  # collapsed, like git


def test_gitignore(repo, tmp_path):
    write(tmp_path, {".gitignore": "*.log\nbuild/\n", "app.py": "", "debug.log": "",
                     "build/out.bin": "", "src/build/x": ""})
    assert sorted(commands.iter_worktree(repo)) == [".gitignore", "app.py"]


def test_branch_and_checkout_rewrite_the_working_tree(repo, tmp_path):
    write(tmp_path, {"shared.txt": "v1\n", "main-only.txt": "m\n"})
    commands.add(repo, ["."])
    commands.commit(repo, "on main")

    commands.checkout(repo, "feature", create=True)
    assert refs.current_branch(repo) == "feature"
    (tmp_path / "main-only.txt").unlink()
    write(tmp_path, {"shared.txt": "v2\n", "feature/new.txt": "f\n"})
    commands.add(repo, ["."])
    commands.commit(repo, "on feature")

    commands.checkout(repo, "main")
    assert (tmp_path / "shared.txt").read_text() == "v1\n"
    assert (tmp_path / "main-only.txt").exists()
    assert not (tmp_path / "feature").exists()  # emptied directories are removed
    assert commands.status(repo).clean
    assert git(tmp_path, "status", "--porcelain") == ""

    commands.checkout(repo, "feature")
    assert (tmp_path / "shared.txt").read_text() == "v2\n"
    assert sorted(refs.list_branches(repo)) == ["feature", "main"]


def test_checkout_refuses_to_lose_work(repo, tmp_path):
    write(tmp_path, {"f.txt": "1\n"})
    commands.add(repo, ["."])
    commands.commit(repo, "one")
    commands.create_branch(repo, "other")
    (tmp_path / "f.txt").write_text("dirty\n")
    with pytest.raises(GitError, match="uncommitted"):
        commands.checkout(repo, "other")


def test_nothing_to_commit(repo, tmp_path):
    write(tmp_path, {"f.txt": "1\n"})
    commands.add(repo, ["."])
    commands.commit(repo, "one")
    with pytest.raises(GitError, match="nothing to commit"):
        commands.commit(repo, "again")


def test_cli_end_to_end(tmp_path):
    assert minigit(tmp_path, "init").returncode == 0
    write(tmp_path, {"hello.txt": "hello\n"})
    assert minigit(tmp_path, "add", "hello.txt").returncode == 0
    out = minigit(tmp_path, "commit", "-m", "say hello").stdout
    assert out.startswith("[main (root-commit) ") and out.strip().endswith("say hello")
    log = minigit(tmp_path, "log").stdout
    assert "Author: Ada Lovelace <ada@example.com>" in log
    assert "Date:   Tue Nov 14 16:13:20 2023 -0600" in log
    assert "    say hello" in log
    blob = hash_object(b"hello\n")
    assert minigit(tmp_path, "cat-file", "-p", blob[:7]).stdout == "hello\n"
    assert minigit(tmp_path, "cat-file", "-t", "HEAD").stdout == "commit\n"
    bad = minigit(tmp_path, "cat-file", "-p", "nope")
    assert bad.returncode == 128 and "unknown revision" in bad.stderr
