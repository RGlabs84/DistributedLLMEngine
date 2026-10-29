"""
The phone chats' salt (2.6, engine/chat_privacy.py): a line only one chat has, so the engine's prompt cache
is shared inside a chat and never between two.

Nothing here may write into the real install: the secret is created under ``<base dir>/config``, and
``chat_privacy`` bound ``get_base_dir`` by name, so the sandbox patches it in that module too.
"""

import os
import re
import stat
import threading

import pytest

from engine import chat_privacy

UUID_A = "3f2b9c1e-6a47-4d52-9a0e-1b7c5d8e2f41"
UUID_B = "9d8c7b6a-5f4e-4d3c-8b2a-1f0e9d8c7b6a"


@pytest.fixture
def sandbox(isolated_config, dlne_root, monkeypatch):
    monkeypatch.setattr(chat_privacy, "get_base_dir", lambda: str(dlne_root))
    monkeypatch.setattr(chat_privacy, "_secret", None)
    return dlne_root


@pytest.mark.parametrize("value", [UUID_A, UUID_B, "00000000-0000-4000-8000-000000000001"])
def test_a_canonical_uuid_is_kept(value):
    assert chat_privacy.clean_chat_id(value) == value


def test_an_upper_case_uuid_is_the_same_chat_in_lower_case():
    assert chat_privacy.clean_chat_id(UUID_A.upper()) == UUID_A


@pytest.mark.parametrize("value", [
    None, 5, ["x" * 10], {"id": "x" * 10}, "", "short", "a" * 65, "has space in it", "new\nline-id-1",
    "semi;colon-id-1", "ünïcödé-chat-id", UUID_A + "\n",
    # what 2.6.0 accepted (8 to 64 token characters) is no chat id any more: it is guessable or constant
    "abcdefgh", "A" * 64, "a_b-c_d-e", "00000000", "alice-the-user",
    "00000000-0000-0000-0000-000000000000", UUID_A.replace("-", ""), "{" + UUID_A + "}", UUID_A + "-1",
    UUID_A[:-1] + "g", " " + UUID_A,
])
def test_anything_else_is_not_a_chat_id(value):
    assert chat_privacy.clean_chat_id(value) is None


def test_the_line_is_stable_for_a_chat_and_different_between_chats(sandbox):
    line = chat_privacy.salt_line(UUID_A)

    assert re.fullmatch(r"\[internal reference [0-9a-f]{16} - ignore it, never mention or repeat it\]", line)
    assert chat_privacy.salt_line(UUID_A) == line
    assert chat_privacy.salt_line(UUID_B) != line


def test_the_secret_is_made_once_private_and_kept_across_restarts(sandbox, monkeypatch):
    first = chat_privacy.salt_line(UUID_A)
    path = sandbox / "config" / "chat_salt.key"

    assert re.fullmatch(r"[0-9a-f]{64}", path.read_text().strip())
    assert stat.S_IMODE(os.stat(path).st_mode) & 0o077 == 0
    monkeypatch.setattr(chat_privacy, "_secret", None)       # a restart reads the same file
    assert chat_privacy.salt_line(UUID_A) == first


def test_another_install_has_another_secret(sandbox, monkeypatch):
    first = chat_privacy.salt_line(UUID_A)
    (sandbox / "config" / "chat_salt.key").unlink()
    monkeypatch.setattr(chat_privacy, "_secret", None)

    assert chat_privacy.salt_line(UUID_A) != first


def test_a_secret_that_cannot_be_stored_still_separates_chats_and_says_so(sandbox, monkeypatch):
    warnings = []
    monkeypatch.setattr(chat_privacy, "log_warning", warnings.append)
    (sandbox / "config").rmdir()
    (sandbox / "config").write_text("a file where the folder should be")      # makedirs fails

    first = chat_privacy.salt_line(UUID_A)

    assert chat_privacy.salt_line(UUID_A) == first and chat_privacy.salt_line(UUID_B) != first
    assert len(warnings) == 1


@pytest.mark.parametrize("damage", ["", "\n", "not hex at all\n", "abcd\n", "zz" * 32 + "\n", "é" * 70])
def test_an_empty_or_damaged_secret_file_is_replaced_once_and_then_kept(sandbox, monkeypatch, damage):
    warnings = []
    monkeypatch.setattr(chat_privacy, "log_warning", warnings.append)
    path = sandbox / "config" / "chat_salt.key"
    path.write_bytes(damage.encode("utf-8"))

    first = chat_privacy.salt_line(UUID_A)

    assert len(warnings) == 1
    assert re.fullmatch(r"[0-9a-f]{64}", path.read_text().strip())
    assert stat.S_IMODE(os.stat(path).st_mode) & 0o077 == 0
    assert [p.name for p in path.parent.iterdir() if p.name.startswith("chat_salt")] == ["chat_salt.key"], "no temp file left"
    monkeypatch.setattr(chat_privacy, "_secret", None)                  # the next start matches this one
    assert chat_privacy.salt_line(UUID_A) == first and len(warnings) == 1


def test_a_good_secret_file_is_never_touched(sandbox, monkeypatch):
    warnings = []
    monkeypatch.setattr(chat_privacy, "log_warning", warnings.append)
    path = sandbox / "config" / "chat_salt.key"
    path.write_text("ab" * 32 + "\n")

    chat_privacy.salt_line(UUID_A)

    assert path.read_text() == "ab" * 32 + "\n" and warnings == []


def test_the_secret_never_reaches_a_log(sandbox, caplog):
    caplog.set_level(0)
    chat_privacy.salt_line(UUID_A)
    secret = (sandbox / "config" / "chat_salt.key").read_text().strip()

    assert secret not in caplog.text


def test_loose_permissions_on_an_existing_secret_file_are_tightened(sandbox):
    path = sandbox / "config" / "chat_salt.key"
    path.write_text("cd" * 32 + "\n")
    os.chmod(path, 0o644)

    chat_privacy.salt_line(UUID_A)

    assert stat.S_IMODE(os.stat(path).st_mode) & 0o077 == 0
    assert path.read_text() == "cd" * 32 + "\n"


def test_a_secret_file_with_a_byte_order_mark_is_used_not_replaced(sandbox, monkeypatch):
    warnings = []
    monkeypatch.setattr(chat_privacy, "log_warning", warnings.append)
    path = sandbox / "config" / "chat_salt.key"
    path.write_bytes(b"\xef\xbb\xbf" + b"ef" * 32 + b"\r\n")        # what Notepad writes

    chat_privacy.salt_line(UUID_A)

    assert warnings == [] and path.read_bytes().startswith(b"\xef\xbb\xbf")


def test_processes_starting_together_agree_on_one_secret(sandbox, monkeypatch):
    results, barrier = [], threading.Barrier(8)

    def start():
        barrier.wait()
        results.append(chat_privacy._load_secret())

    threads = [threading.Thread(target=start) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    path = sandbox / "config" / "chat_salt.key"

    assert len(results) == 8 and len(set(results)) == 1
    assert bytes.fromhex(path.read_text().strip()) == results[0]
    assert [p.name for p in path.parent.iterdir()] == ["chat_salt.key"], "no temp file left"


def test_a_file_that_appears_first_is_used_not_overwritten(sandbox, monkeypatch):
    """The other process wins the race between 'there is no file' and our link: its secret stands."""
    path = sandbox / "config" / "chat_salt.key"
    real_link = os.link

    def lose_the_race(src, dst):
        path.write_text("12" * 32 + "\n")            # the other process got there first
        return real_link(src, dst)                  # now raises FileExistsError

    monkeypatch.setattr(chat_privacy.os, "link", lose_the_race)

    assert chat_privacy._load_secret() == bytes.fromhex("12" * 32)
    assert path.read_text() == "12" * 32 + "\n"


def test_the_chat_tag_is_the_hmac_the_salt_line_carries(sandbox):
    tag = chat_privacy.chat_tag(UUID_A)

    assert re.fullmatch(r"[0-9a-f]{16}", tag) and tag in chat_privacy.salt_line(UUID_A)
    assert chat_privacy.chat_tag(UUID_B) != tag
