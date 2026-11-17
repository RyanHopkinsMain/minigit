"""minigit: git's core data model in a few hundred lines of Python."""

from .repo import GitError, Repo

__all__ = ["GitError", "Repo"]
__version__ = "0.1.0"
