"""Command-line interface: `minigit <command> ...`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import commands, refs
from .index import read_index
from .objects import hash_object, parse_tree, read_object
from .repo import GitError, Repo


def cmd_init(args: argparse.Namespace) -> None:
    repo = Repo.init(Path(args.path))
    print(f"Initialized empty Git repository in {repo.gitdir}/")


def cmd_hash_object(args: argparse.Namespace) -> None:
    data = sys.stdin.buffer.read() if args.stdin else Path(args.file).read_bytes()
    print(hash_object(data, args.type, Repo.find() if args.write else None))


def cmd_cat_file(args: argparse.Namespace) -> None:
    repo = Repo.find()
    obj_type, data = read_object(repo, refs.rev_parse(repo, args.object))
    if args.mode == "-t":
        print(obj_type)
    elif args.mode == "-s":
        print(len(data))
    elif obj_type == "tree":
        for e in parse_tree(data):
            print(f"{e.mode:0>6} {e.kind} {e.oid}\t{e.name}")
    else:
        sys.stdout.buffer.write(data)


def cmd_add(args: argparse.Namespace) -> None:
    commands.add(Repo.find(), args.paths)


def cmd_ls_files(args: argparse.Namespace) -> None:
    for path, entry in sorted(read_index(Repo.find()).items()):
        print(f"{entry.mode:o} {entry.oid} 0\t{path}" if args.stage else path)


def cmd_write_tree(args: argparse.Namespace) -> None:
    print(commands.write_tree(Repo.find()))


def cmd_commit(args: argparse.Namespace) -> None:
    repo = Repo.find()
    is_root = commands.head_commit(repo) is None
    oid = commands.commit(repo, args.message)
    branch = refs.current_branch(repo) or "detached HEAD"
    root = " (root-commit)" if is_root else ""
    print(f"[{branch}{root} {oid[:7]}] {args.message.splitlines()[0]}")


def cmd_log(args: argparse.Namespace) -> None:
    repo = Repo.find()
    for i, (oid, c) in enumerate(commands.log(repo, args.rev, args.n)):
        if args.oneline:
            print(f"{oid[:7]} {c.message.splitlines()[0] if c.message else ''}")
            continue
        who, when = commands.format_signature(c.author)
        if i:
            print()
        print(f"commit {oid}\nAuthor: {who}\nDate:   {when}\n")
        for line in c.message.rstrip("\n").split("\n"):
            print(f"    {line}".rstrip())


def cmd_status(args: argparse.Namespace) -> None:
    st = commands.status(Repo.find())
    print(f"On branch {st.branch}" if st.branch else "HEAD detached")
    sections = [
        ("Changes to be committed:", [f"{k + ':':<12}{p}" for k, p in st.staged]),
        ("Changes not staged for commit:", [f"{k + ':':<12}{p}" for k, p in st.unstaged]),
        ("Untracked files:", st.untracked),
    ]
    for title, lines in sections:
        if lines:
            print(f"\n{title}")
            for line in lines:
                print(f"\t{line}")
    if st.clean:
        print("nothing to commit, working tree clean")


def cmd_branch(args: argparse.Namespace) -> None:
    repo = Repo.find()
    if args.name:
        commands.create_branch(repo, args.name)
        return
    current = refs.current_branch(repo)
    for name in refs.list_branches(repo):
        print(f"{'*' if name == current else ' '} {name}")


def cmd_checkout(args: argparse.Namespace) -> None:
    repo = Repo.find()
    commands.checkout(repo, args.target, create=args.b)
    if args.b:
        print(f"Switched to a new branch '{args.target}'")
    elif refs.current_branch(repo):
        print(f"Switched to branch '{args.target}'")
    else:
        print(f"HEAD is now at {refs.rev_parse(repo, 'HEAD')[:7]}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minigit", description="A tiny, readable git.")
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    p = sub.add_parser("init", help="create an empty repository")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("hash-object", help="compute (and optionally store) an object id")
    p.add_argument("-w", dest="write", action="store_true", help="write the object")
    p.add_argument("-t", dest="type", default="blob")
    p.add_argument("--stdin", action="store_true")
    p.add_argument("file", nargs="?")
    p.set_defaults(func=cmd_hash_object)

    p = sub.add_parser("cat-file", help="show an object's type, size or content")
    mode = p.add_mutually_exclusive_group(required=True)
    for flag, text in (("-t", "type"), ("-s", "size"), ("-p", "pretty-printed content")):
        mode.add_argument(flag, dest="mode", action="store_const", const=flag, help=f"show {text}")
    p.add_argument("object")
    p.set_defaults(func=cmd_cat_file)

    p = sub.add_parser("add", help="stage files")
    p.add_argument("paths", nargs="+")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("ls-files", help="list staged files")
    p.add_argument("-s", "--stage", action="store_true")
    p.set_defaults(func=cmd_ls_files)

    p = sub.add_parser("write-tree", help="write the index as a tree object")
    p.set_defaults(func=cmd_write_tree)

    p = sub.add_parser("commit", help="record the staged snapshot")
    p.add_argument("-m", dest="message", required=True)
    p.set_defaults(func=cmd_commit)

    p = sub.add_parser("log", help="show commit history")
    p.add_argument("rev", nargs="?", default="HEAD")
    p.add_argument("-n", type=int, default=None)
    p.add_argument("--oneline", action="store_true")
    p.set_defaults(func=cmd_log)

    p = sub.add_parser("status", help="show staged, unstaged and untracked changes")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("branch", help="list or create branches")
    p.add_argument("name", nargs="?")
    p.set_defaults(func=cmd_branch)

    p = sub.add_parser("checkout", help="switch branches")
    p.add_argument("-b", action="store_true", help="create the branch first")
    p.add_argument("target")
    p.set_defaults(func=cmd_checkout)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "hash-object" and not (args.stdin or args.file):
        build_parser().error("hash-object needs a file or --stdin")
    try:
        args.func(args)
    except GitError as err:
        print(f"fatal: {err}", file=sys.stderr)
        return 128
    except BrokenPipeError:
        pass
    return 0
