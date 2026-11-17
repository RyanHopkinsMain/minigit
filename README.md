# minigit

**Git's core, rebuilt from scratch in under 1,000 lines of readable Python (no dependencies).**

minigit writes real git repositories. Every object it stores, every tree it builds,
and the binary index it maintains are byte-for-byte what `git` itself would produce,
so you can switch between `minigit` and `git` in the same repo and neither notices.

```text
$ minigit init .
Initialized empty Git repository in /home/ryan/demo/.git/
$ minigit status
On branch main

Untracked files:
	docs/
	hello.py
$ minigit add .
$ minigit commit -m "First commit"
[main (root-commit) 1e1a089] First commit
$ minigit cat-file -p HEAD
tree 9e27b34f8dfc2b3a5be5a24e5aa642c7f893af37
author Ryan <ryan@example.com> 1763325360 -0600
committer Ryan <ryan@example.com> 1763325360 -0600

First commit
$ minigit cat-file -p 9e27b34
040000 tree 2e004400caa826e16e93097a190330aefc0fb0dc	docs
100644 blob 58fd1933942cbde8495d08e071329191fa363dbc	hello.py
$ minigit checkout -b experiment
Switched to a new branch 'experiment'
$ minigit add hello.py
$ minigit commit -m "Try something new"
[experiment fbcd18d] Try something new
$ minigit log --oneline
fbcd18d Try something new
1e1a089 First commit
$ git fsck --strict && git log --oneline   # real git agrees
fbcd18d Try something new
1e1a089 First commit
```

## Why build this?

Git feels like magic until you see that it's four ideas stacked on top of each other:

1. **Content-addressed storage.** Every file version is a *blob* stored under the
   SHA-1 of its contents. Same content, same id, stored once.
2. **Trees.** A directory is just a list of `(mode, name, id)` entries, itself
   hashed and stored. Change one file and only the trees on its path get new ids.
3. **Commits.** A commit is a tiny text object pointing at one root tree and its
   parent commits. History is a linked list (well, a DAG) of hashes.
4. **Refs.** A branch is a 41-byte file containing a commit id. `HEAD` points at
   the branch you're on.

Once those clicked for me, the rest of git (branches being cheap, commits being
immutable, why `rebase` makes "new" commits) stopped being mysterious.

## What's implemented

| Command | What it does |
| --- | --- |
| `init` | Create `.git/` with objects, refs and `HEAD → refs/heads/main` |
| `hash-object [-w] FILE` | Compute (and optionally store) a blob id |
| `cat-file -t/-s/-p OBJ` | Inspect any object; accepts short ids and branch names |
| `add PATH...` | Stage files and directories (deleted files get unstaged) |
| `ls-files [-s]` | List the index |
| `write-tree` | Turn the index into nested tree objects |
| `commit -m MSG` | Snapshot the index, link it to `HEAD`, move the branch |
| `log [--oneline] [-n N]` | Walk first-parent history |
| `status` | Staged vs unstaged vs untracked, with git's `dir/` collapsing |
| `branch [NAME]` | List or create branches |
| `checkout [-b] TARGET` | Switch branches or detach, rewriting the working tree safely |

It also honours a simple `.gitignore` (globs and `dir/` patterns) and reads
`user.name`/`user.email` from your git config or the `GIT_AUTHOR_*` variables.

## The fiddly bits worth reading

- **[`objects.py`](minigit/objects.py)**: the tree sort order. Git sorts directory
  entries *as if their names ended in `/`*, so the file `a.txt` comes before the
  directory `a`, but `a0` comes after it. A naive sort produces trees that
  `git fsck` rejects.
- **[`index.py`](minigit/index.py)**: the binary staging area: 62-byte fixed
  headers, variable-length paths, NUL padding to 8-byte boundaries, and a SHA-1
  trailer over the whole file. The cached `stat` data is why `git status` is fast,
  and minigit uses it the same way.
- **[`commands.py`](minigit/commands.py)**: `checkout` refuses to run when you
  have uncommitted changes or when an untracked file would be overwritten. Losing
  someone's work is the one unforgivable bug in a VCS.

## Running it

```bash
pip install -e .            # installs the `minigit` command
minigit init my-project
# or without installing:
python -m minigit --help
```

## Tests

The test suite runs minigit **against the real `git` binary**:

- object ids match `git hash-object`
- trees match `git write-tree` byte-for-byte (including the sort-order trap)
- repos written by minigit pass `git fsck --strict`, and `git status` reads our index cleanly
- minigit reads repositories and indexes written by real git

```bash
pip install pytest
pytest
```

## Limitations (deliberate)

No packfiles (run `git unpack-objects` on a cloned repo first), no merges, no
remotes, no diff. Those are where git gets genuinely complicated, and the point
here is the data model.

## License

MIT
