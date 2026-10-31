#!/usr/bin/env python3
"""Refuse commits whose author or committer is not a GitHub no-reply address.

This public repository is a GENERATED tree: every commit is written by the
publishing tool through the GitHub API, never by hand. Every commit shows its
author and committer to anyone, so both must be a no-reply address
(``*@users.noreply.github.com`` or ``noreply@github.com``) — never a personal
mailbox, a company domain or a machine placeholder.

Usage::

    python scripts/check_commit_identity.py <rev-range>     # e.g. A..B
    python scripts/check_commit_identity.py --branches --tags

Runs in CI on every push (``.github/workflows/commit-identity.yml``).
"""
from __future__ import annotations

import subprocess
import sys
from typing import Iterable, List, Optional


def problem(email: str) -> Optional[str]:
    """Return why this address must not be published, or None when it is fine."""
    e = (email or "").strip().lower()
    if e.endswith("@users.noreply.github.com") or e == "noreply@github.com":
        return None
    return "not a GitHub no-reply address"


def check(rev_args: Iterable[str]) -> List[str]:
    out = subprocess.run(
        ["git", "log", "--format=%H%x00%an%x00%ae%x00%cn%x00%ce", *rev_args],
        check=True, capture_output=True, text=True,
    ).stdout
    bad = []
    for line in out.splitlines():
        if not line:
            continue
        sha, an, ae, cn, ce = line.split("\x00")
        for role, name, email in (("author", an, ae), ("committer", cn, ce)):
            why = problem(email)
            if why:
                bad.append(f"{sha[:10]} {role} {name} <{email}>: {why}")
    return bad


def main(argv: List[str]) -> int:
    bad = check(argv or ["HEAD", "-n", "1"])
    if bad:
        print("Refusing: these commits would publish a private or placeholder identity:", file=sys.stderr)
        for b in bad:
            print("  " + b, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
