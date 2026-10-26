#!/usr/bin/env python3
"""
Publish a snapshot of this repository to the public GitHub repository as ONE new commit.

    scripts/publish_public.py [--ref HEAD] [-m "what changed"]          dry run: build, scan, show
    scripts/publish_public.py [--ref HEAD] [-m "what changed"] --push   ... and push it

The local history is never pushed: it holds secrets that were rotated but are still in old
commits. The public repository has its own history, and every update is a snapshot of
``--ref`` committed on top of it. Before anything is committed the snapshot must pass:

  * gitleaks (``gitleaks`` on PATH, or $GITLEAKS), apart from the known public values below;
  * no file that never belongs in public (keys, .env, private notes);
  * none of this machine's private strings: your home directory, your git e-mail, and every
    line of ~/.config/dlne/publish-deny.txt (kept out of the repository on purpose, since a
    list of what must not be published would publish it).

The commit is authored with the GitHub no-reply address, never your own e-mail.
"""

import argparse
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Iterable, List, Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PUBLIC_URL = "https://github.com/RGlabs84/DistributedLLMEngine.git"
PUBLIC_BRANCH = "main"
AUTHOR_NAME = "Ross"
AUTHOR_EMAIL = "51202846+RGlabs84@users.noreply.github.com"
DENY_FILE = os.path.expanduser("~/.config/dlne/publish-deny.txt")

# Left out of every snapshot (they stay in the repository, just not on GitHub):
#   builder/       the old desktop build tooling, including the obfuscator;
#   ABOUT-DLNE/    a placeholder page ("John Doe", a made-up company), never true;
#   discord/       the retired v1.0 product's channel texts and bot scripts (discord/adverts/ too).
EXCLUDED_DIRS = ("builder", "ABOUT-DLNE", "discord")

# Single files left out, as paths from the repository root with "/" separators:
#   download_dlne_models.sh   a legacy model downloader whose file names do not match what they download;
#   the wallpaper with logos  it shows another app's icon beside DLNE's, and DLNE has one icon only;
#   the fakes                 test files made of invented credentials (the owner: "dont push the fakers"):
#                             the end-to-end encryption vectors, the secret-redaction tests and their fake-secret
#                             helper, the tests that read them, and the extension's security-review tests (an
#                             invented password in a curl line). They stay in the repository and its CI.
EXCLUDED_FILES = (
    "download_dlne_models.sh",
    "wallpapers/dlne_background_1440p_16_9_with_logos.png",
    "tests/fixtures/e2e_vectors.json",
    "tests/test_e2e.py",
    "vscode-extension/test/privacy-e2e.test.js",
    "vscode-extension/test/redact.test.js",
    "vscode-extension/test/helpers/fake-secrets.js",
    "vscode-extension/test/privacy-hooks.test.js",
    "vscode-extension/test/security-review.test.js",
)

# Never public, wherever they turn up in the tree.
FORBIDDEN_PATTERNS = (
    ".env", "*.env", ".env.*", "*.pem", "*.key", "id_rsa*", "id_ed25519*",
    "api_keys.json", "starter_quota.json", "config.txt", "worker_invite*",
    "RESUME_HANDOFF.md", "To-Do.md", "Next direction.md", "implementation_plan.md",
    ".agent_rules.md", "*_backup*", "DLNE Backup*",
)

# gitleaks findings that are public by design: the swarm's published network key and fake
# keys in the test suite (tests/test_search.py proves a Brave key never reaches a log).
KNOWN_PUBLIC = frozenset({
    "b8e1f0c9a7d24e3f8a6b5c4d3e2f1a0b9c8d7e6f5a4b3c2d1e0f9a8b7c6d5e4f",
    "bd1d9bbf50b93e10a963fa2d0b6ee44e1138ca20f4036b765dbc51dd7bc7f083",
    "BSA-test-key-must-never-leak-7c1f",
})


class PublishError(Exception):
    """Something the maintainer needs to fix. The message is the whole story."""


def _run(args: List[str], cwd: Optional[str] = None, env: Optional[dict] = None) -> str:
    result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        raise PublishError(f"{' '.join(args[:3])} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


# --------------------------------------------------------------------------------------
# The snapshot and its checks
# --------------------------------------------------------------------------------------

def make_snapshot(repo: str, ref: str, dest: str) -> None:
    """The committed tree at ``ref`` (never the working tree), minus EXCLUDED_DIRS and EXCLUDED_FILES."""
    os.makedirs(dest, exist_ok=True)
    archive = subprocess.run(["git", "-C", repo, "archive", ref], capture_output=True)
    if archive.returncode != 0:
        raise PublishError(f"git archive {ref} failed: {archive.stderr.decode(errors='replace').strip()}")
    subprocess.run(["tar", "-x", "-C", dest], input=archive.stdout, check=True)
    for name in EXCLUDED_DIRS:
        shutil.rmtree(os.path.join(dest, name), ignore_errors=True)
    for name in EXCLUDED_FILES:
        try:
            os.remove(os.path.join(dest, *name.split("/")))
        except FileNotFoundError:
            pass


def _files(root: str) -> Iterable[str]:
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in files:
            yield os.path.join(base, name)


def forbidden_files(root: str) -> List[str]:
    found = []
    for path in _files(root):
        rel = os.path.relpath(path, root)
        if any(fnmatch.fnmatch(part, pattern) for part in rel.split(os.sep) for pattern in FORBIDDEN_PATTERNS):
            found.append(rel)
    return sorted(found)


def private_strings(repo: str, deny_file: str = DENY_FILE) -> List[str]:
    """This machine's private strings: home directory, git e-mail, and the deny file."""
    strings = [os.path.expanduser("~")]
    email = subprocess.run(["git", "-C", repo, "config", "user.email"], capture_output=True, text=True)
    if email.stdout.strip():
        strings.append(email.stdout.strip())
    if os.path.isfile(deny_file):
        with open(deny_file, "r", encoding="utf-8") as handle:
            strings += [line.strip() for line in handle if line.strip() and not line.startswith("#")]
    # A bare "/" home (some service accounts) would match every file.
    return [s for s in strings if len(s) >= 4]


def files_containing(root: str, strings: List[str]) -> List[str]:
    """Which files contain any of ``strings``. Never says which string, so it is safe to print."""
    needles = [s.encode() for s in strings]
    found = []
    for path in _files(root):
        with open(path, "rb") as handle:
            data = handle.read()
        if any(n in data for n in needles):
            found.append(os.path.relpath(path, root))
    return sorted(found)


def gitleaks_findings(root: str, gitleaks: str) -> List[str]:
    """gitleaks over the snapshot, minus KNOWN_PUBLIC. Returns 'file:line rule', never the secret."""
    with tempfile.TemporaryDirectory() as tmp:
        report = os.path.join(tmp, "report.json")
        result = subprocess.run(
            [gitleaks, "dir", root, "--no-banner", "--report-format", "json", "--report-path", report],
            capture_output=True, text=True)
        # 0 = clean, 1 = findings; anything else means the scan itself did not run.
        if result.returncode not in (0, 1) or not os.path.isfile(report):
            raise PublishError(f"gitleaks did not run: {result.stderr.strip()[:300]}")
        with open(report, "r", encoding="utf-8") as handle:
            findings = json.load(handle) or []
    return sorted(
        f"{os.path.relpath(f['File'], root) if os.path.isabs(f['File']) else f['File']}:{f['StartLine']} {f['RuleID']}"
        for f in findings if f.get("Secret") not in KNOWN_PUBLIC)


def check_snapshot(root: str, repo: str, gitleaks: str, deny_file: str = DENY_FILE) -> None:
    problems = []
    for rel in forbidden_files(root):
        problems.append(f"never-public file: {rel}")
    for rel in files_containing(root, private_strings(repo, deny_file)):
        problems.append(f"contains a private string: {rel}")
    for finding in gitleaks_findings(root, gitleaks):
        problems.append(f"gitleaks: {finding}")
    if problems:
        raise PublishError("The snapshot is not safe to publish:\n  " + "\n  ".join(problems))


# --------------------------------------------------------------------------------------
# Committing on top of the public history
# --------------------------------------------------------------------------------------

def commit_snapshot(snapshot: str, clone: str, message: str) -> bool:
    """Replace the clone's tree with the snapshot and commit. False when nothing changed."""
    for entry in os.listdir(clone):
        if entry == ".git":
            continue
        path = os.path.join(clone, entry)
        shutil.rmtree(path) if os.path.isdir(path) and not os.path.islink(path) else os.remove(path)
    shutil.copytree(snapshot, clone, dirs_exist_ok=True, symlinks=True)
    _run(["git", "add", "-A"], cwd=clone)
    if not _run(["git", "status", "--porcelain"], cwd=clone):
        return False
    env = dict(os.environ, GIT_AUTHOR_NAME=AUTHOR_NAME, GIT_AUTHOR_EMAIL=AUTHOR_EMAIL,
               GIT_COMMITTER_NAME=AUTHOR_NAME, GIT_COMMITTER_EMAIL=AUTHOR_EMAIL)
    _run(["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", message], cwd=clone, env=env)
    return True


def publish(repo: str, ref: str, message: str, push: bool, gitleaks: str,
            public_url: str = PUBLIC_URL, deny_file: str = DENY_FILE, out=print) -> bool:
    commit = _run(["git", "-C", repo, "rev-parse", "--short", ref])
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = os.path.join(tmp, "snapshot")
        clone = os.path.join(tmp, "public")
        make_snapshot(repo, ref, snapshot)
        check_snapshot(snapshot, repo, gitleaks, deny_file)
        out(f"Snapshot of {commit} passed gitleaks and the private-string checks.")

        _run(["git", "clone", "-q", "--branch", PUBLIC_BRANCH, public_url, clone])
        if not commit_snapshot(snapshot, clone, message):
            out("The public repository already matches this snapshot; nothing to publish.")
            return False
        out(_run(["git", "show", "--stat", "--format=%h %an <%ae>%n%n%s", "HEAD"], cwd=clone))

        if not push:
            out("Dry run: nothing was pushed. Run again with --push to publish this commit.")
            return False
        _run(["git", "push", "-q", "origin", f"HEAD:{PUBLIC_BRANCH}"], cwd=clone)
        out(f"Published to {public_url} ({PUBLIC_BRANCH}).")
        return True


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ref", default="HEAD", help="commit to snapshot (default HEAD)")
    parser.add_argument("-m", "--message", help="public commit message (default names the version)")
    parser.add_argument("--push", action="store_true", help="push the commit (default: dry run)")
    parser.add_argument("--public-url", default=PUBLIC_URL)
    args = parser.parse_args(argv)

    gitleaks = os.environ.get("GITLEAKS") or shutil.which("gitleaks")
    if not gitleaks:
        print("gitleaks is required (put it on PATH or set GITLEAKS). Nothing is published unscanned.",
              file=sys.stderr)
        return 2
    message = args.message
    if not message:
        sys.path.insert(0, REPO_ROOT)
        from engine.version import VERSION
        message = f"DLNE server {VERSION}"
    try:
        publish(REPO_ROOT, args.ref, message, args.push, gitleaks, args.public_url)
    except PublishError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
