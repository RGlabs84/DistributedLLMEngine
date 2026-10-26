"""
Tests for scripts/publish_public.py: a snapshot goes to the public repository as one commit
on top of ITS history, and nothing private gets through.

Everything runs against throwaway git repositories and a stand-in for gitleaks.
"""

import importlib.util
import json
import os
import subprocess
import sys

import pytest

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "publish_public.py")
_spec = importlib.util.spec_from_file_location("publish_public", SCRIPT)
pub = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pub)

SECRET = "ghp_" + "x" * 36


def git(cwd, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="Dev", GIT_AUTHOR_EMAIL="dev@private.example",
               GIT_COMMITTER_NAME="Dev", GIT_COMMITTER_EMAIL="dev@private.example")
    return subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=cwd, env=env,
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repos(tmp_path):
    """A private source repo (with a secret in its history) and a bare 'public' repo."""
    src = tmp_path / "src"
    src.mkdir()
    git(src, "init", "-q", "-b", "work")
    git(src, "config", "user.email", "dev@private.example")
    (src / "app.py").write_text(f"TOKEN = '{SECRET}'\n")
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "old commit with a secret")
    (src / "app.py").write_text("print('hello')\n")
    (src / "builder").mkdir()
    (src / "builder" / "obfuscate.py").write_text("x = 1\n")
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "secret removed")

    public = tmp_path / "public.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(public))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", "-q", str(public), str(seed))
    (seed / "README.md").write_text("public\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "initial public commit")
    git(seed, "push", "-q", "origin", "HEAD:main")
    return src, public, tmp_path


@pytest.fixture
def fake_gitleaks(tmp_path):
    """A gitleaks stand-in that reports any file containing SECRET."""
    path = tmp_path / "gitleaks"
    path.write_text(f"""#!{sys.executable}
import json, os, sys
root = sys.argv[2]
report = sys.argv[sys.argv.index("--report-path") + 1]
found = []
for base, _dirs, files in os.walk(root):
    for name in files:
        p = os.path.join(base, name)
        if {SECRET!r} in open(p, errors="ignore").read():
            found.append({{"File": p, "StartLine": 1, "RuleID": "github-pat", "Secret": {SECRET!r}}})
json.dump(found, open(report, "w"))
sys.exit(1 if found else 0)
""")
    path.chmod(0o755)
    return str(path)


def _publish(src, public, gitleaks, deny, push=True):
    lines = []
    result = pub.publish(str(src), "HEAD", "DLNE server 9.9.9", push, gitleaks,
                         public_url=str(public), deny_file=str(deny), out=lines.append)
    return result, "\n".join(lines)


def test_publishes_one_noreply_commit_on_top_of_the_public_history(repos, fake_gitleaks):
    src, public, tmp = repos
    ok, _ = _publish(src, public, fake_gitleaks, tmp / "none.txt")
    assert ok

    log = git(public, "log", "--format=%an <%ae>|%s", "main").splitlines()
    assert log == [f"Ross <{pub.AUTHOR_EMAIL}>|DLNE server 9.9.9", "Dev <dev@private.example>|initial public commit"]
    # The private history, and the secret in it, never reached the public repository.
    assert "secret removed" not in git(public, "log", "--all", "--format=%s")
    files = git(public, "ls-tree", "-r", "--name-only", "main").splitlines()
    assert files == ["app.py"]          # README replaced by the snapshot, builder/ left out


def test_dry_run_pushes_nothing(repos, fake_gitleaks):
    src, public, tmp = repos
    ok, output = _publish(src, public, fake_gitleaks, tmp / "none.txt", push=False)
    assert not ok
    assert "Dry run" in output
    assert len(git(public, "log", "--format=%h", "main").splitlines()) == 1


def test_nothing_to_publish_when_already_identical(repos, fake_gitleaks):
    src, public, tmp = repos
    assert _publish(src, public, fake_gitleaks, tmp / "none.txt")[0]
    ok, output = _publish(src, public, fake_gitleaks, tmp / "none.txt")
    assert not ok
    assert "already matches" in output


def test_a_secret_in_the_snapshot_stops_it(repos, fake_gitleaks):
    src, public, tmp = repos
    (src / "app.py").write_text(f"TOKEN = '{SECRET}'\n")
    git(src, "commit", "-q", "-am", "oops")

    with pytest.raises(pub.PublishError, match=r"gitleaks: app.py:1 github-pat") as err:
        _publish(src, public, fake_gitleaks, tmp / "none.txt")
    assert SECRET not in str(err.value)
    assert len(git(public, "log", "--format=%h", "main").splitlines()) == 1


def test_known_public_values_are_not_findings(repos, tmp_path, monkeypatch):
    src, public, tmp = repos
    monkeypatch.setattr(pub, "KNOWN_PUBLIC", frozenset({SECRET}))
    (src / "app.py").write_text(f"SWARM_KEY = '{SECRET}'\n")
    git(src, "commit", "-q", "-am", "public key")
    gitleaks = tmp_path / "gl"
    gitleaks.write_text(f"""#!{sys.executable}
import json, sys
json.dump([{{"File": "app.py", "StartLine": 1, "RuleID": "generic", "Secret": {SECRET!r}}}],
          open(sys.argv[sys.argv.index("--report-path") + 1], "w"))
sys.exit(1)
""")
    gitleaks.chmod(0o755)
    assert _publish(src, public, str(gitleaks), tmp / "none.txt")[0]


def test_private_strings_stop_it_without_being_printed(repos, fake_gitleaks):
    src, public, tmp = repos
    deny = tmp / "deny.txt"
    deny.write_text("# comment\n198.51.100.77\n")
    (src / "notes.py").write_text("HOST = '198.51.100.77'\nEMAIL = 'dev@private.example'\n")
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "leak")

    with pytest.raises(pub.PublishError) as err:
        _publish(src, public, fake_gitleaks, deny)
    assert "contains a private string: notes.py" in str(err.value)
    assert "198.51.100.77" not in str(err.value)


def test_the_git_email_alone_stops_it(repos, fake_gitleaks):
    src, public, tmp = repos
    (src / "notes.py").write_text("EMAIL = 'dev@private.example'\n")
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "leak")
    with pytest.raises(pub.PublishError, match="private string: notes.py"):
        _publish(src, public, fake_gitleaks, tmp / "none.txt")


@pytest.mark.parametrize("name", [".env", "vscode-extension/.env", "keys/release.pem", "config/api_keys.json",
                                  "RESUME_HANDOFF.md", "To-Do.md", "x_backup_2026.zip"])
def test_never_public_files_stop_it(repos, fake_gitleaks, name):
    src, public, tmp = repos
    path = src / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x\n")
    git(src, "add", "-f", "-A")
    git(src, "commit", "-q", "-m", "add file")
    with pytest.raises(pub.PublishError, match="never-public file"):
        _publish(src, public, fake_gitleaks, tmp / "none.txt")


def test_the_snapshot_is_the_commit_not_the_working_tree(repos, fake_gitleaks):
    src, public, tmp = repos
    (src / "app.py").write_text(f"TOKEN = '{SECRET}'\n")     # uncommitted
    assert _publish(src, public, fake_gitleaks, tmp / "none.txt")[0]
    assert git(public, "show", "main:app.py") == "print('hello')"


def test_a_scan_that_did_not_run_is_an_error(repos, tmp_path):
    src, public, tmp = repos
    broken = tmp_path / "broken"
    broken.write_text("#!/bin/sh\nexit 3\n")
    broken.chmod(0o755)
    with pytest.raises(pub.PublishError, match="gitleaks did not run"):
        _publish(src, public, str(broken), tmp / "none.txt")


def test_without_gitleaks_nothing_is_published(monkeypatch, capsys):
    monkeypatch.delenv("GITLEAKS", raising=False)
    monkeypatch.setattr(pub.shutil, "which", lambda name: None)
    assert pub.main([]) == 2
    assert "gitleaks is required" in capsys.readouterr().err


def test_retired_and_misleading_files_stay_in_the_repository_but_not_on_github(repos, fake_gitleaks):
    src, public, tmp = repos
    leftovers = [
        "ABOUT-DLNE/index.html",                  # placeholder page
        "discord/channels/faq.md",                # the retired v1.0 product's texts and scripts
        "discord/adverts/announcement.md",
        "discord/scripts/setup_server.py",
        "download_dlne_models.sh",                # legacy downloader
        "wallpapers/dlne_background_1440p_16_9_with_logos.png",   # shows another app's icon
        "wallpapers/dlne_background_1440p_16_9.png",              # the DLNE one stays
        "scripts/dlne_discord_tile.py",           # lives in scripts/, so it is not discord/
    ]
    for name in leftovers:
        path = src / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n")
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "add files")

    assert _publish(src, public, fake_gitleaks, tmp / "none.txt")[0]
    assert git(public, "ls-tree", "-r", "--name-only", "main").splitlines() == [
        "app.py", "scripts/dlne_discord_tile.py", "wallpapers/dlne_background_1440p_16_9.png"]
    # Nothing was deleted from the private repository.
    assert set(leftovers) <= set(git(src, "ls-files").splitlines())


def test_the_exclusions_are_what_the_maintainer_asked_for():
    assert set(pub.EXCLUDED_DIRS) == {"builder", "ABOUT-DLNE", "discord"}
    assert set(pub.EXCLUDED_FILES) == {"download_dlne_models.sh",
                                       "wallpapers/dlne_background_1440p_16_9_with_logos.png"}
