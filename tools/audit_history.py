"""Audit the git history and working tree for leaked secrets and private data.

Scans every commit's tree (not just HEAD), because a value removed in a later
commit is still reachable in history and would be published with the repo.

The exact strings that matter for a given deployment are private, so they are
read from an optional, git-ignored file next to this script:

    tools/audit_patterns.local.txt      one entry per line, "label|text"

Without that file the script still runs, using generic heuristics that catch
the usual shapes (API keys, private LAN addresses, private key blocks, emails,
personal absolute paths). Those catch accidental leaks; the local file catches
regressions of values you already know about.

Usage:  python tools/audit_history.py
Exit code 0 means no findings.
"""
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATTERNS_FILE = os.path.join(REPO, "tools", "audit_patterns.local.txt")

# Generic shapes worth flagging in any repository.
GENERIC_PATTERNS = [
    ("OpenAI-style API key", r"sk-[A-Za-z0-9_\-]{20,}"),
    ("GitHub token", r"github_pat_[A-Za-z0-9_]{20,}"),
    ("GitHub classic token", r"ghp_[A-Za-z0-9]{20,}"),
    ("private key block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("private LAN address", r"\b(?:192\.168|10|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
    ("email address", r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
    ("personal absolute path", r"[A-Za-z]:[\\/](?:Users|home)[\\/][^\\/\s]+"),
]


def load_local_patterns():
    """label -> literal text, from the git-ignored local file."""
    entries = []
    if not os.path.exists(PATTERNS_FILE):
        return entries
    with open(PATTERNS_FILE, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            label, _, value = line.partition("|")
            if value:
                entries.append((label.strip(), value))
    return entries


def run(args):
    return subprocess.run(args, cwd=REPO, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def scan_text_history(patterns):
    revs = run(["git", "rev-list", "--all"]).stdout.split()
    print("commits in history: %d" % len(revs))
    findings = 0
    for label, pattern, kind in patterns:
        args = ["git", "grep", "-I", "-l"]
        if kind == "regex":
            args.append("-E")
        args += [pattern] + revs + ["--"]
        res = run(args)
        hits = [h for h in res.stdout.splitlines() if h.strip()]
        if hits:
            findings += 1
            print("  LEAK  %-26s %s" % (label, pattern))
            seen = set()
            for line in hits:
                _, _, path = line.partition(":")
                if path not in seen:
                    seen.add(path)
                    print("         in %s" % path)
        else:
            print("  ok    %-26s %s" % (label, pattern))
    return findings


def scan_binaries(patterns):
    print("\nbinary blobs referenced by history:")
    revs = run(["git", "rev-list", "--objects", "--all"]).stdout.splitlines()
    findings = 0
    checked = 0
    for line in revs:
        sha, _, path = line.partition(" ")
        if not path.lower().endswith((".apk", ".jar", ".zip", ".png", ".jpg", ".jks", ".keystore")):
            continue
        checked += 1
        blob = subprocess.run(["git", "cat-file", "blob", sha], cwd=REPO,
                              capture_output=True).stdout
        hits = []
        for label, pattern, kind in patterns:
            if kind == "literal":
                if pattern.encode("utf-8", "ignore") in blob:
                    hits.append(label)
        if hits:
            findings += 1
            print("  LEAK  %-46s %s" % (path, ", ".join(hits)))
    print("  %d binary blobs checked, %d with hits" % (checked, findings))
    return findings


def main():
    patterns = [(l, p, "regex") for l, p in GENERIC_PATTERNS]
    patterns += [(l, p, "literal") for l, p in load_local_patterns()]
    if not os.path.exists(PATTERNS_FILE):
        print("note: %s not found - generic heuristics only\n"
              % os.path.relpath(PATTERNS_FILE, REPO))

    print("=" * 72)
    print("GIT HISTORY AUDIT (every commit, every tracked file)")
    print("=" * 72)
    findings = scan_text_history(patterns)
    findings += scan_binaries(patterns)

    print("\n" + "=" * 72)
    print("CURRENT TREE")
    print("=" * 72)
    files = run(["git", "ls-files"]).stdout.splitlines()
    print("tracked files at HEAD: %d" % len(files))
    risky = [f for f in files
             if not f.endswith(".example")
             and any(k in f.lower() for k in ("jks", "keystore", ".db", ".log",
                                              "token", "nas.env", ".pem"))]
    print("files that look like secrets: %s" % (risky if risky else "none"))
    if risky:
        findings += len(risky)

    print("\nRESULT: %s (%d findings)"
          % ("CLEAN" if findings == 0 else "PROBLEMS", findings))
    return 0 if findings == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
