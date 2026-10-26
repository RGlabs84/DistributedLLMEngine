"""
Verified, resumable model downloads (engine/downloader.py: download_verified / fetch_verified).

Everything talks to a real HTTPS server on 127.0.0.1 (tests/download_fakes.py), so the failures
the downloader has to survive are real ones: a connection cut mid-body, a server that ignores
Range, a 503, a stall. The disk is simulated only where a real full disk is not practical
(the free-space probe and a write that fails with ENOSPC).

The promises under test:
  * every part is fetched, its size and SHA-256 checked while it streams in;
  * the real file name never holds a partial or wrong file (atomic rename of a verified .part);
  * a download that stopped can be resumed (HTTP Range), by the same call or a later one;
  * a manifest cannot write outside the folder, or fetch over anything but https;
  * two callers asking for the same file never write the same .part;
  * a disk that is too small is refused up front, and a disk that fills mid-way fails cleanly.
"""

import errno
import os
import ssl
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from engine import app_pool, downloader
from tests.download_fakes import FakeModelServer, make_blob, sha256

REPO = str(Path(__file__).resolve().parent.parent)
PART = 300_000


@pytest.fixture
def server(tmp_path, monkeypatch):
    # A proxy in the environment must never sit between the test and 127.0.0.1.
    for variable in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    certs = tmp_path / "certs"
    certs.mkdir()
    with FakeModelServer(certs) as running:
        yield running


@pytest.fixture
def dest(tmp_path):
    return tmp_path / "models"


def fetch(server, files, dest, **options):
    settings = dict(ssl_context=server.client_context, timeout=5.0, backoff=0.01, max_backoff=0.05)
    settings.update(options)
    return downloader.fetch_verified(files, str(dest), **settings)


def three_parts(server, size=PART):
    blobs = [make_blob(size, seed) for seed in range(3)]
    files = [server.serve(f"model-0000{i + 1}-of-00003.gguf", blob) for i, blob in enumerate(blobs)]
    return files, blobs


def one_part(server, name="model.gguf", size=PART, seed=0):
    blob = make_blob(size, seed)
    return server.serve(name, blob), blob


def listing(folder):
    return sorted(os.listdir(folder)) if os.path.isdir(folder) else []


def bookkeeping(folder):
    """Our working files: partial downloads, locks and temporary markers (the markers proper are fine)."""
    return [n for n in listing(folder) if n.endswith((".part", ".lock", ".tmp"))]


# ======================================================================================
# The happy path
# ======================================================================================

def test_every_part_is_downloaded_and_only_verified_files_are_left(server, dest):
    files, blobs = three_parts(server)
    result = fetch(server, files, dest)
    assert result.ok and result.reason == "" and not result.cancelled
    assert result.bytes_fetched == 3 * PART
    assert result.paths == [str(dest / f["file"]) for f in files]
    for entry, blob in zip(files, blobs):
        assert (dest / entry["file"]).read_bytes() == blob
    assert bookkeeping(dest) == []
    # What is left besides the parts is the marker the CPU pool also uses.
    assert listing(dest) == sorted([f["file"] for f in files] + [f["file"] + ".dlne-sha256" for f in files])


def test_download_verified_answers_with_a_plain_bool(server, dest):
    files, _ = three_parts(server)
    assert downloader.download_verified(files, str(dest), ssl_context=server.client_context) is True
    assert downloader.download_verified([], str(dest)) is False


def test_the_markers_are_the_ones_the_cpu_pool_checks(server, dest):
    files, _ = three_parts(server)
    assert fetch(server, files, dest).ok
    for entry in files:
        assert app_pool.sha256_ok(str(dest / entry["file"]), entry["sha256"], entry["size"])


def test_a_marker_from_the_cpu_pool_is_trusted_and_a_marker_from_here_is_too(tmp_path):
    path = tmp_path / "m.gguf"
    path.write_bytes(make_blob(1000))
    digest = sha256(path.read_bytes())
    stamp = os.stat(path)
    assert app_pool.sha256_ok(str(path), digest, 1000)           # writes the marker
    path.write_bytes(b"\0" * 1000)                                # same size ...
    os.utime(path, (stamp.st_atime, stamp.st_mtime))              # ... and mtime: only the marker vouches
    assert downloader._file_verified(str(path), digest, 1000)
    os.remove(str(path) + ".dlne-sha256")
    path.write_bytes(make_blob(1000))
    os.utime(path, (stamp.st_atime, stamp.st_mtime))
    assert downloader._file_verified(str(path), digest, 1000)    # hashes, writes the marker
    path.write_bytes(b"\0" * 1000)
    os.utime(path, (stamp.st_atime, stamp.st_mtime))
    assert app_pool.sha256_ok(str(path), digest, 1000)


def test_verified_files_are_skipped_without_touching_the_network(server, dest):
    files, _ = three_parts(server)
    assert fetch(server, files, dest).ok
    server.requests.clear()
    again = fetch(server, files, dest)
    assert again.ok and again.bytes_fetched == 0
    assert server.requests == []


def test_a_file_placed_by_hand_is_accepted_and_given_a_marker(server, dest):
    entry, blob = one_part(server)
    dest.mkdir()
    (dest / entry["file"]).write_bytes(blob)
    result = fetch(server, [entry], dest)
    assert result.ok and server.requests == []
    assert (dest / (entry["file"] + ".dlne-sha256")).exists()


def test_a_wrong_file_of_the_right_size_is_replaced(server, dest):
    entry, blob = one_part(server)
    dest.mkdir()
    (dest / entry["file"]).write_bytes(b"x" * PART)
    assert fetch(server, [entry], dest).ok
    assert (dest / entry["file"]).read_bytes() == blob
    assert server.count(entry["file"]) == 1


def test_only_the_missing_parts_are_fetched(server, dest):
    files, blobs = three_parts(server)
    dest.mkdir()
    (dest / files[0]["file"]).write_bytes(blobs[0])
    result = fetch(server, files, dest)
    assert result.ok and result.bytes_fetched == 2 * PART
    assert server.count(files[0]["file"]) == 0 and server.count(files[2]["file"]) == 1


# ======================================================================================
# Resuming and interruptions
# ======================================================================================

def test_a_partial_file_from_an_earlier_run_is_resumed_with_a_range_request(server, dest):
    entry, blob = one_part(server)
    dest.mkdir()
    (dest / (entry["file"] + ".part")).write_bytes(blob[:120_000])
    result = fetch(server, [entry], dest)
    assert result.ok
    assert server.ranges(entry["file"]) == ["bytes=120000-"]
    assert result.bytes_fetched == PART - 120_000
    assert (dest / entry["file"]).read_bytes() == blob
    assert bookkeeping(dest) == []


def test_a_complete_part_left_before_the_rename_is_verified_and_renamed_without_a_download(server, dest):
    entry, blob = one_part(server)
    dest.mkdir()
    (dest / (entry["file"] + ".part")).write_bytes(blob)
    assert fetch(server, [entry], dest).ok
    assert server.requests == []
    assert (dest / entry["file"]).read_bytes() == blob


def test_a_complete_but_wrong_part_is_thrown_away_and_fetched_again(server, dest):
    entry, blob = one_part(server)
    dest.mkdir()
    (dest / (entry["file"] + ".part")).write_bytes(b"\1" * PART)
    assert fetch(server, [entry], dest).ok
    assert server.ranges(entry["file"]) == [None]
    assert (dest / entry["file"]).read_bytes() == blob


def test_a_part_longer_than_the_file_is_thrown_away(server, dest):
    entry, blob = one_part(server)
    dest.mkdir()
    (dest / (entry["file"] + ".part")).write_bytes(b"\1" * (PART + 5))
    assert fetch(server, [entry], dest).ok
    assert server.ranges(entry["file"]) == [None]
    assert (dest / entry["file"]).read_bytes() == blob


def test_a_connection_cut_mid_file_is_resumed_by_the_same_call(server, dest):
    entry, blob = one_part(server)
    server.rule(entry["file"], cut_after=100_000)
    result = fetch(server, [entry], dest)
    assert result.ok
    first, second = server.ranges(entry["file"])
    assert first is None
    offset = int(second.removeprefix("bytes=").removesuffix("-"))
    assert 0 < offset <= 100_000
    assert result.bytes_fetched == PART                           # every byte crossed the wire once
    assert (dest / entry["file"]).read_bytes() == blob
    assert bookkeeping(dest) == []


def test_an_interrupted_download_keeps_its_partial_file_and_a_later_call_resumes_it(server, dest):
    entry, blob = one_part(server)
    # The first connection brings 100 kB and is cut; the server then stays busy.
    server.rule(entry["file"], cut_after=100_000, statuses=[None] + [503] * 9)
    first = fetch(server, [entry], dest, retries=1)
    assert not first.ok and not first.lasting and not first.cancelled
    assert not (dest / entry["file"]).exists()                    # the real name never holds a partial file
    partial = dest / (entry["file"] + ".part")
    assert 0 < partial.stat().st_size <= 100_000
    assert not (dest / (entry["file"] + ".lock")).exists()        # and the lock is not left behind
    server.rule(entry["file"])                                    # the server recovers
    second = fetch(server, [entry], dest)
    assert second.ok and (dest / entry["file"]).read_bytes() == blob
    assert server.ranges(entry["file"])[-1].startswith("bytes=")


def test_keep_partial_false_leaves_nothing_after_a_failure(server, dest):
    entry, _ = one_part(server)
    server.rule(entry["file"], cut_after=100_000, statuses=[None] + [503] * 9)
    result = fetch(server, [entry], dest, retries=0, keep_partial=False)
    assert not result.ok and server.count(entry["file"]) == 2     # it did get as far as a partial file
    assert bookkeeping(dest) == [] and entry["file"] not in listing(dest)


def test_a_server_that_ignores_range_makes_the_part_start_over(server, dest):
    entry, blob = one_part(server)
    server.rule(entry["file"], ignore_range=True)
    dest.mkdir()
    (dest / (entry["file"] + ".part")).write_bytes(blob[:50_000])
    result = fetch(server, [entry], dest)
    assert result.ok and (dest / entry["file"]).read_bytes() == blob
    assert result.bytes_fetched == PART


def test_a_resume_point_the_server_refuses_starts_the_file_over(server, dest):
    entry, blob = one_part(server)
    dest.mkdir()
    (dest / (entry["file"] + ".part")).write_bytes(blob[:50_000])
    server.rule(entry["file"], statuses=[416])                    # "Range Not Satisfiable" to the first request
    result = fetch(server, [entry], dest)
    assert result.ok and (dest / entry["file"]).read_bytes() == blob
    assert server.ranges(entry["file"]) == ["bytes=50000-", None]


def test_leftover_bytes_that_are_not_a_prefix_of_the_file_cost_one_clean_restart(server, dest):
    entry, blob = one_part(server)
    dest.mkdir()
    (dest / (entry["file"] + ".part")).write_bytes(b"\0" * 50_000)
    result = fetch(server, [entry], dest)
    assert result.ok and (dest / entry["file"]).read_bytes() == blob
    assert server.ranges(entry["file"]) == ["bytes=50000-", None]


# ======================================================================================
# Wrong data
# ======================================================================================

def test_a_download_with_the_wrong_hash_fails_for_good_and_leaves_nothing(server, dest):
    entry, _ = one_part(server)
    entry = dict(entry, sha256=sha256(b"not this file"))
    result = fetch(server, [entry], dest)
    assert not result.ok and result.lasting
    assert "SHA-256" in result.reason and result.failed_file == entry["file"]
    assert server.count(entry["file"]) == 1                       # a wrong file is not retried
    assert listing(dest) == []


def test_a_wrong_part_stops_the_rest_and_keeps_the_earlier_good_parts(server, dest):
    files, blobs = three_parts(server)
    files[1] = dict(files[1], sha256=sha256(b"nope"))
    result = fetch(server, files, dest)
    assert not result.ok and result.failed_file == files[1]["file"]
    assert (dest / files[0]["file"]).read_bytes() == blobs[0]     # verified, so it stays
    assert not (dest / files[1]["file"]).exists() and not (dest / files[2]["file"]).exists()
    assert server.count(files[2]["file"]) == 0
    assert bookkeeping(dest) == []


def test_a_file_shorter_than_signed_is_refused(server, dest):
    entry, _ = one_part(server)
    entry = dict(entry, size=PART + 10_000)                       # the server's copy is 10 kB short
    result = fetch(server, [entry], dest)
    assert not result.ok and result.lasting and "bytes long" in result.reason
    assert server.count(entry["file"]) == 1
    assert listing(dest) == []


def test_a_server_that_promises_more_than_it_sends_never_yields_a_file(server, dest):
    entry, blob = one_part(server)
    # It claims the signed length but its body ends 5 kB early (the connection just closes).
    server.rule(entry["file"], body=blob[:-5_000], content_length=PART)
    result = fetch(server, [entry], dest, retries=1)
    assert not result.ok
    assert bookkeeping(dest) == [] and entry["file"] not in listing(dest)


def test_a_body_shorter_than_signed_without_a_length_is_not_accepted(server, dest):
    entry, blob = one_part(server)
    server.rule(entry["file"], no_length=True, body=blob[:-1])
    result = fetch(server, [entry], dest, retries=1)
    assert not result.ok and entry["file"] not in listing(dest)


def test_a_file_longer_than_signed_is_refused_and_deleted(server, dest):
    entry, blob = one_part(server)
    server.rule(entry["file"], no_length=True, extra=b"surplus")   # more than was signed arrives
    result = fetch(server, [entry], dest)
    assert not result.ok and result.lasting and "more than" in result.reason
    assert listing(dest) == []


def test_a_signed_size_smaller_than_the_servers_file_is_refused(server, dest):
    entry, _ = one_part(server)
    entry = dict(entry, size=PART - 1000)
    result = fetch(server, [entry], dest)
    assert not result.ok and result.lasting
    assert listing(dest) == []


def test_a_compressed_answer_is_refused(server, dest):
    entry, _ = one_part(server)
    server.rule(entry["file"], headers={"Content-Encoding": "gzip"})
    result = fetch(server, [entry], dest)
    assert not result.ok and result.lasting and "compressed" in result.reason
    assert listing(dest) == []


# ======================================================================================
# The manifest is untrusted: file names
# ======================================================================================

BAD_NAMES = [
    "../evil.gguf", "..\\evil.gguf", "/etc/cron.d/evil", "sub/evil.gguf", "sub\\evil.gguf",
    "C:evil.gguf", "C:\\evil.gguf", "..", ".", "", " ", ".hidden.gguf", "-rf.gguf", "~root",
    "evil.gguf.part", "evil.gguf.lock", "evil.gguf.dlne-sha256", "evil.gguf.tmp", "evil.gguf.dlne-download",
    "nul.gguf", "CON", "com1.txt", "Lpt9.gguf", "a..b.gguf", "evil.gguf.", "x\x00y.gguf",
    "x\ny.gguf", "x y.gguf", "[b]x.gguf", "a[b].gguf", "a" * 201, "caf\u00e9.gguf", None, 5, ["a.gguf"],
]


@pytest.mark.parametrize("name", BAD_NAMES)
def test_an_unsafe_file_name_refuses_the_whole_download_before_anything_happens(server, tmp_path, dest, name):
    good, _ = one_part(server, "good.gguf")
    server.files["/x.gguf"] = b"0123456789"
    evil = {"file": name, "url": server.url("x.gguf"), "size": 10, "sha256": "0" * 64}
    result = fetch(server, [good, evil], dest)
    assert not result.ok and result.lasting
    assert server.requests == []                                  # not even the good file was fetched
    assert not dest.exists()                                      # and no folder was made for it
    assert sorted(p.name for p in tmp_path.iterdir()) == ["certs"]


def test_ordinary_model_names_are_safe():
    for name in ("Qwen3.6-35B-A3B-UD-IQ3_S.gguf", "Qwen3.8-Flash-Next-UD-Q2_K_XL-00001-of-00003.gguf",
                 "gpt-oss-20b-MXFP4.gguf", "qwen2.5-coder-14b-instruct-q4_k_m.gguf", "a.gguf"):
        assert downloader.safe_file_name(name), name


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_symlink_in_the_way_cannot_redirect_a_write_out_of_the_folder(server, tmp_path, dest):
    entry, _ = one_part(server)
    outside = tmp_path / "outside.gguf"
    dest.mkdir()
    os.symlink(outside, dest / entry["file"])
    result = fetch(server, [entry], dest)
    assert not result.ok and "outside" in result.reason
    assert not outside.exists() and server.requests == []


def test_the_same_name_twice_is_refused_even_in_another_case(server, dest):
    first, _ = one_part(server, "Model.gguf")
    second = dict(first, file="model.GGUF")
    result = fetch(server, [first, second], dest)
    assert not result.ok and "twice" in result.reason and server.requests == []


# ======================================================================================
# The manifest is untrusted: sizes, hashes and URLs
# ======================================================================================

@pytest.mark.parametrize("size", [0, -5, True, 1.5, "100", None])
def test_a_size_that_is_not_a_positive_integer_is_refused(server, dest, size):
    entry, _ = one_part(server)
    result = fetch(server, [dict(entry, size=size)], dest)
    assert not result.ok and "size" in result.reason and server.requests == []


@pytest.mark.parametrize("digest", ["", "abc", "z" * 64, "0" * 63, "0" * 65, None, 12, "0" * 64 + "\n"])
def test_a_hash_that_is_not_sha256_hex_is_refused(server, dest, digest):
    entry, _ = one_part(server)
    result = fetch(server, [dict(entry, sha256=digest)], dest)
    assert not result.ok and "SHA-256" in result.reason and server.requests == []


def test_an_uppercase_hash_is_accepted(server, dest):
    entry, blob = one_part(server)
    assert fetch(server, [dict(entry, sha256=entry["sha256"].upper())], dest).ok


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:1/x.gguf", "ftp://example.com/x.gguf", "file:///etc/passwd", "//example.com/x.gguf",
    "https:///x.gguf", "https://user:pw@example.com/x.gguf", "https://user@example.com/x.gguf",
    "https://example.com/x y.gguf", "https://example.com:notaport/x.gguf", "example.com/x.gguf",
    "javascript:alert(1)", "data:text/plain,hi", "", None, 7, "https://example.com/caf\u00e9",
])
def test_only_plain_https_addresses_are_fetched(server, dest, url):
    entry, _ = one_part(server)
    result = fetch(server, [dict(entry, url=url)], dest)
    assert not result.ok and result.lasting and server.requests == []


@pytest.mark.parametrize("files", [None, [], (), "model.gguf", {"file": "a.gguf"}, [None], ["a.gguf"], [[]]])
def test_a_malformed_file_list_is_refused(server, dest, files):
    result = fetch(server, files, dest)
    assert not result.ok and result.lasting and server.requests == []


def test_a_redirect_to_another_https_address_is_followed_and_keeps_the_range(server, dest):
    entry, blob = one_part(server, "real.gguf")
    server.files["/pointer.gguf"] = b""
    server.rule("pointer.gguf", redirect=server.url("real.gguf"))
    pointing = dict(entry, file="pointer.gguf", url=server.url("pointer.gguf"))
    dest.mkdir()
    (dest / "pointer.gguf.part").write_bytes(blob[:70_000])
    result = fetch(server, [pointing], dest)
    assert result.ok and (dest / "pointer.gguf").read_bytes() == blob
    assert server.ranges("real.gguf") == ["bytes=70000-"]


def test_a_redirect_to_plain_http_is_refused(server, dest):
    entry, _ = one_part(server)
    server.rule(entry["file"], redirect="http://127.0.0.1:1/model.gguf")
    result = fetch(server, [entry], dest)
    assert not result.ok and result.lasting and "redirect" in result.reason
    assert listing(dest) == []


def test_a_server_certificate_that_is_not_trusted_is_refused(server, dest):
    entry, _ = one_part(server)
    result = fetch(server, [entry], dest, ssl_context=ssl.create_default_context())
    assert not result.ok and result.lasting and "certificate" in result.reason
    assert server.requests == []
    assert listing(dest) == []


# ======================================================================================
# Retries, backoff and timeouts
# ======================================================================================

def test_a_missing_file_fails_at_once_without_retries(server, dest):
    entry, _ = one_part(server)
    entry = dict(entry, url=server.url("gone.gguf"))
    result = fetch(server, [entry], dest)
    assert not result.ok and result.lasting and "404" in result.reason
    assert server.count("gone.gguf") == 1 and listing(dest) == []


def test_a_busy_server_is_retried(server, dest):
    entry, blob = one_part(server)
    server.rule(entry["file"], statuses=[503, 503, 429])
    result = fetch(server, [entry], dest)
    assert result.ok and server.count(entry["file"]) == 4
    assert (dest / entry["file"]).read_bytes() == blob


def test_a_server_that_stays_busy_is_given_up_on_after_the_retries(server, dest):
    entry, _ = one_part(server)
    server.rule(entry["file"], statuses=[503] * 50)
    result = fetch(server, [entry], dest, retries=2)
    assert not result.ok and not result.lasting and "503" in result.reason and "2 retries" in result.reason
    assert server.count(entry["file"]) == 3
    assert listing(dest) == []


class RecordingEvent(threading.Event):
    """A cancel flag that records how long the downloader asked to wait, and does not wait."""

    def __init__(self):
        super().__init__()
        self.waits = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return super().wait(0)


def test_the_wait_between_retries_doubles_up_to_a_cap(server, dest):
    entry, _ = one_part(server)
    server.rule(entry["file"], statuses=[503] * 50)
    cancel = RecordingEvent()
    result = fetch(server, [entry], dest, retries=5, backoff=1.0, max_backoff=4.0, cancel=cancel)
    assert not result.ok and server.count(entry["file"]) == 6
    assert len(cancel.waits) == 5
    for waited, nominal in zip(cancel.waits, [1, 2, 4, 4, 4]):
        assert 0.75 * nominal <= waited <= 1.25 * nominal


def test_a_servers_retry_after_is_honoured_up_to_a_limit(server, dest):
    entry, _ = one_part(server)
    server.rule(entry["file"], statuses=[429, 429], retry_after=7)
    cancel = RecordingEvent()
    assert fetch(server, [entry], dest, cancel=cancel).ok
    assert cancel.waits == [7.0, 7.0]
    assert downloader._retry_after({"Retry-After": "99999"}) == downloader._MAX_RETRY_AFTER
    assert downloader._retry_after({"Retry-After": "soon"}) is None
    assert downloader._retry_after({}) is None
    assert downloader._retry_after({"Retry-After": "nan"}) is None


def test_progress_keeps_a_flaky_link_going_but_a_dead_one_runs_out_of_retries(server, dest):
    entry, blob = one_part(server)
    # Three cuts in a row, each after some new data: 3 failures with retries=1 still finish.
    server.rule(entry["file"], cut_after=60_000, cut_times=3)
    result = fetch(server, [entry], dest, retries=1)
    assert result.ok and (dest / entry["file"]).read_bytes() == blob
    assert server.count(entry["file"]) == 4


def test_going_round_in_circles_is_not_progress(server, dest):
    entry, blob = one_part(server)
    # Every connection ignores Range and sends the first kilobyte again before it is cut.
    server.rule(entry["file"], ignore_range=True, cut_after=1_000, cut_times=99)
    dest.mkdir()
    (dest / (entry["file"] + ".part")).write_bytes(blob[:50_000])
    result = fetch(server, [entry], dest, retries=2)
    assert not result.ok and not result.lasting
    assert server.count(entry["file"]) == 4                       # one free attempt, then the retries
    assert not (dest / entry["file"]).exists()


def test_a_server_that_never_answers_times_out_and_is_retried(server, dest):
    entry, _ = one_part(server)
    server.rule(entry["file"], stall=1.5)
    started = time.monotonic()
    result = fetch(server, [entry], dest, timeout=0.3, retries=1)
    assert not result.ok and not result.lasting
    assert server.count(entry["file"]) == 2
    assert time.monotonic() - started < 5


# ======================================================================================
# Disk space
# ======================================================================================

def test_a_disk_that_is_too_small_is_refused_before_anything_is_fetched(server, dest, monkeypatch):
    files, _ = three_parts(server)
    monkeypatch.setattr(downloader, "_disk_free", lambda path: 500_000)
    result = fetch(server, files, dest)
    assert not result.ok and result.lasting and "not enough disk space" in result.reason
    assert server.requests == [] and bookkeeping(dest) == []


def test_the_space_needed_is_what_is_still_to_download_plus_the_margin(server, dest, monkeypatch):
    files, blobs = three_parts(server)
    dest.mkdir()
    (dest / files[0]["file"]).write_bytes(blobs[0])                         # done: needs nothing
    (dest / (files[1]["file"] + ".part")).write_bytes(blobs[1][:100_000])   # 200 kB to go
    need = PART + 200_000                                                    # plus the whole third part
    monkeypatch.setattr(downloader, "_disk_free", lambda path: need + 999)
    refused = fetch(server, files, dest, free_margin=1000)
    assert not refused.ok and "not enough disk space" in refused.reason and server.requests == []
    monkeypatch.setattr(downloader, "_disk_free", lambda path: need + 1000)
    assert fetch(server, files, dest, free_margin=1000).ok


def test_the_default_margin_is_a_gigabyte_or_two_percent(server, dest, monkeypatch):
    entry, _ = one_part(server)
    monkeypatch.setattr(downloader, "_disk_free", lambda path: PART + (1 << 30) - 1)
    assert not fetch(server, [entry], dest).ok
    monkeypatch.setattr(downloader, "_disk_free", lambda path: PART + (1 << 30))
    assert fetch(server, [entry], dest).ok
    # An unknown amount of free space does not block a download.
    other = server.serve("other.gguf", make_blob(1000, 5))
    monkeypatch.setattr(downloader, "_disk_free", lambda path: None)
    assert fetch(server, [other], dest).ok


def test_a_disk_that_fills_up_mid_download_fails_cleanly_and_can_be_resumed(server, dest, monkeypatch):
    entry, blob = one_part(server)
    real_write = downloader._write_block
    written = []

    def filling_up(handle, block):
        if sum(written) + len(block) > 150_000:
            raise OSError(errno.ENOSPC, "No space left on device")
        written.append(len(block))
        real_write(handle, block)

    monkeypatch.setattr(downloader, "_write_block", filling_up)
    result = fetch(server, [entry], dest)
    assert not result.ok and result.lasting and "disk is full" in result.reason
    assert not (dest / entry["file"]).exists()
    assert (dest / (entry["file"] + ".part")).stat().st_size == sum(written)
    assert not (dest / (entry["file"] + ".lock")).exists()

    monkeypatch.setattr(downloader, "_write_block", real_write)             # space was freed
    assert fetch(server, [entry], dest).ok
    assert (dest / entry["file"]).read_bytes() == blob
    assert server.ranges(entry["file"])[-1] == f"bytes={sum(written)}-"


def test_a_folder_that_cannot_be_made_is_reported(server, tmp_path):
    entry, _ = one_part(server)
    blocker = tmp_path / "file-not-folder"
    blocker.write_text("x")
    result = fetch(server, [entry], blocker / "models")
    assert not result.ok and result.lasting and server.requests == []


# ======================================================================================
# Callers at the same time
# ======================================================================================

def run_callers(server, dest, file_lists, **options):
    results = [None] * len(file_lists)

    def call(index):
        results[index] = fetch(server, file_lists[index], dest, **options)

    threads = [threading.Thread(target=call, args=(i,)) for i in range(len(file_lists))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert not any(thread.is_alive() for thread in threads)
    return results


def test_two_callers_for_the_same_file_download_it_once(server, dest):
    entry, blob = one_part(server, size=400_000)
    server.rule(entry["file"], delay=0.01)                                   # slow enough to overlap
    results = run_callers(server, dest, [[entry], [entry]])
    assert all(r.ok for r in results)
    assert server.count(entry["file"]) == 1
    assert sum(r.bytes_fetched for r in results) == 400_000
    assert (dest / entry["file"]).read_bytes() == blob
    assert bookkeeping(dest) == []


def test_callers_with_the_same_parts_in_other_orders_fetch_each_part_once(server, dest):
    files, blobs = three_parts(server, size=200_000)
    for entry in files:
        server.rule(entry["file"], delay=0.005)
    orders = [files, files[::-1], files[1:] + files[:1], files]
    results = run_callers(server, dest, orders)
    assert all(r.ok for r in results)
    for entry, blob in zip(files, blobs):
        assert server.count(entry["file"]) == 1
        assert (dest / entry["file"]).read_bytes() == blob
    assert bookkeeping(dest) == []


def test_a_caller_that_waits_reports_it(server, dest, monkeypatch):
    entry, _ = one_part(server, size=400_000)
    server.rule(entry["file"], delay=0.02)
    monkeypatch.setattr(downloader, "_PROGRESS_EVERY", 0)
    phases = []
    started = threading.Event()

    def streaming(update):
        if update["phase"] == "downloading":
            started.set()

    first = threading.Thread(target=lambda: fetch(server, [entry], dest, progress=streaming))
    first.start()
    assert started.wait(10)
    second = fetch(server, [entry], dest, progress=lambda u: phases.append(u["phase"]))
    first.join(30)
    assert second.ok and "waiting" in phases and server.count(entry["file"]) == 1


def test_the_lock_is_held_across_processes(tmp_path):
    lock = tmp_path / "x.gguf.lock"
    holder = textwrap.dedent(f"""
        import sys, threading
        sys.path.insert(0, {REPO!r})
        from engine import downloader
        with downloader._PartLock({str(lock)!r}, threading.Event(), 30):
            print("locked", flush=True)
            sys.stdin.readline()
    """)
    proc = subprocess.Popen([sys.executable, "-c", holder], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, cwd=str(tmp_path))
    try:
        assert proc.stdout.readline().strip() == "locked"
        started = time.monotonic()
        with pytest.raises(downloader.DownloadError, match="locked for too long"):
            with downloader._PartLock(str(lock), threading.Event(), 0.6):
                pytest.fail("took a lock another process holds")
        assert 0.5 <= time.monotonic() - started < 5
        proc.stdin.write("\n")
        proc.stdin.flush()
        assert proc.wait(15) == 0
        with downloader._PartLock(str(lock), threading.Event(), 5):          # free again
            pass
        assert not lock.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def test_a_lock_left_by_a_process_that_died_does_not_block(tmp_path):
    lock = tmp_path / "x.gguf.lock"
    crash = textwrap.dedent(f"""
        import os, sys, threading
        sys.path.insert(0, {REPO!r})
        from engine import downloader
        downloader._PartLock({str(lock)!r}, threading.Event(), 30).acquire()
        os._exit(1)                                   # no release, no cleanup
    """)
    subprocess.run([sys.executable, "-c", crash], check=False, cwd=str(tmp_path), timeout=60,
                   stderr=subprocess.DEVNULL)
    assert lock.exists()                              # the dead holder left the file ...
    started = time.monotonic()
    with downloader._PartLock(str(lock), threading.Event(), 5):               # ... but not the lock
        pass
    assert time.monotonic() - started < 2


def test_waiting_for_a_lock_can_be_cancelled(tmp_path):
    lock = str(tmp_path / "x.gguf.lock")
    cancel = threading.Event()
    outcome = []

    def waiter():
        try:
            with downloader._PartLock(lock, cancel, 30):
                outcome.append("got it")
        except downloader._Cancelled:
            outcome.append("cancelled")

    with downloader._PartLock(lock, threading.Event(), 5):
        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.4)
        cancel.set()
        thread.join(5)
    assert outcome == ["cancelled"]


# ======================================================================================
# Cancelling and progress
# ======================================================================================

def test_cancel_stops_the_download_and_keeps_the_partial_file(server, dest, monkeypatch):
    monkeypatch.setattr(downloader, "_BLOCK", 16 * 1024)
    monkeypatch.setattr(downloader, "_PROGRESS_EVERY", 0)
    entry, blob = one_part(server, size=600_000)
    server.rule(entry["file"], delay=0.01)
    cancel = threading.Event()

    def progress(update):
        if update["phase"] == "downloading" and update["file_done"] >= 100_000:
            cancel.set()

    result = fetch(server, [entry], dest, cancel=cancel, progress=progress)
    assert not result.ok and result.cancelled and not result.lasting
    partial = dest / (entry["file"] + ".part")
    assert 100_000 <= partial.stat().st_size < 600_000
    assert not (dest / entry["file"]).exists() and not (dest / (entry["file"] + ".lock")).exists()

    server.rule(entry["file"])
    assert fetch(server, [entry], dest).ok
    assert (dest / entry["file"]).read_bytes() == blob


def test_a_download_cancelled_before_it_starts_fetches_nothing(server, dest):
    entry, _ = one_part(server)
    cancel = threading.Event()
    cancel.set()
    result = fetch(server, [entry], dest, cancel=cancel)
    assert result.cancelled and server.requests == []


def test_progress_covers_every_part_and_ends_complete(server, dest, monkeypatch):
    monkeypatch.setattr(downloader, "_PROGRESS_EVERY", 0)
    files, _ = three_parts(server)
    updates = []
    assert fetch(server, files, dest, progress=updates.append).ok
    last = updates[-1]
    assert last["phase"] == "done" and last["done"] == last["total"] == 3 * PART
    assert all(u["count"] == 3 and u["total"] == 3 * PART for u in updates)
    flowing = [u["done"] for u in updates if u["phase"] in ("downloading", "done")]
    assert flowing == sorted(flowing)
    downloading = [u for u in updates if u["phase"] == "downloading"]
    assert {u["file"] for u in downloading} == {f["file"] for f in files}
    assert {u["index"] for u in downloading} == {1, 2, 3}
    assert all(0 <= u["file_done"] <= u["file_size"] == PART for u in downloading)


def test_progress_says_when_it_is_retrying_and_when_it_failed(server, dest, monkeypatch):
    monkeypatch.setattr(downloader, "_PROGRESS_EVERY", 0)
    entry, _ = one_part(server)
    server.rule(entry["file"], statuses=[503] * 9)
    updates = []
    result = fetch(server, [entry], dest, retries=1, progress=updates.append)
    assert not result.ok
    retrying = [u for u in updates if u["phase"] == "retrying"]
    assert retrying and "503" in retrying[0]["error"] and retrying[0]["retry_in"] > 0
    assert updates[-1]["phase"] == "failed" and updates[-1]["error"] == result.reason


def test_a_progress_callback_that_raises_does_not_stop_the_download(server, dest):
    files, _ = three_parts(server)
    calls = []

    def broken(update):
        calls.append(update)
        raise RuntimeError("the UI is gone")

    assert fetch(server, files, dest, progress=broken).ok
    assert calls


def test_a_download_that_is_already_complete_still_reports_done(server, dest):
    files, _ = three_parts(server)
    assert fetch(server, files, dest).ok
    updates = []
    assert fetch(server, files, dest, progress=updates.append).ok
    assert updates[-1]["phase"] == "done" and updates[-1]["done"] == updates[-1]["total"]


# ======================================================================================
# Windows: msvcrt file locks, files that are busy, deletes that wait
# ======================================================================================
#
# These run on any machine. `_is_windows()` is what the module asks, `fcntl` and `msvcrt` are names in
# its namespace, and a fake `msvcrt` below behaves the way the C runtime's `locking()` does:
# a byte range is locked per open file handle, a second handle gets EACCES, and the range is
# freed when its holder unlocks or closes. What no test here can show is the real Windows kernel
# (Defender holding a file, a delete that stays pending); that needs a real Windows PC (the
# cross-platform report lists the steps).

class FakeMsvcrt:
    LK_UNLCK, LK_LOCK, LK_NBLCK, LK_RLCK, LK_NBRLCK = 0, 1, 2, 3, 4

    def __init__(self, errno_for_lock_error=errno.EACCES):
        self.errno_for_lock_error = errno_for_lock_error
        self.held = {}          # (device, inode) -> the descriptor holding byte 0 .. nbytes
        self.calls = []         # (mode, nbytes, position)

    def _key(self, fd):
        info = os.fstat(fd)
        return info.st_dev, info.st_ino

    def locking(self, fd, mode, nbytes):
        self.calls.append((mode, nbytes, os.lseek(fd, 0, os.SEEK_CUR)))
        key = self._key(fd)
        if mode == self.LK_UNLCK:
            if self.held.get(key) != fd:
                raise OSError(errno.EACCES, "Permission denied")      # the range was not locked by this handle
            del self.held[key]
        elif mode == self.LK_NBLCK:
            if self.held.get(key, fd) != fd:
                raise OSError(self.errno_for_lock_error, "Permission denied")
            self.held[key] = fd
        else:
            raise AssertionError("the downloader only ever asks for a non-blocking lock")


@pytest.fixture
def windows(monkeypatch):
    """The downloader on a fake Windows: no fcntl, a fake msvcrt, and `_is_windows()` true."""
    fake = FakeMsvcrt()
    monkeypatch.setattr(downloader, "fcntl", None)
    monkeypatch.setattr(downloader, "msvcrt", fake)
    monkeypatch.setattr(downloader, "_is_windows", lambda: True)
    return fake


def test_a_lock_another_process_holds_is_waited_for_with_msvcrt_and_taken_when_freed(tmp_path, windows):
    lock = str(tmp_path / "x.gguf.lock")
    other = os.open(lock, os.O_RDWR | os.O_CREAT)          # "another process": a handle of its own
    windows.locking(other, windows.LK_NBLCK, 1)
    waited = []
    try:
        started = time.monotonic()
        with pytest.raises(downloader.DownloadError, match="locked for too long"):
            with downloader._PartLock(lock, threading.Event(), 0.6, on_wait=lambda: waited.append(1)):
                pytest.fail("took a lock another handle holds")
        assert waited and 0.5 <= time.monotonic() - started < 5
        windows.locking(other, windows.LK_UNLCK, 1)
        with downloader._PartLock(lock, threading.Event(), 5):
            assert len(windows.held) == 1, "and now this process holds it"
    finally:
        os.close(other)
    assert not os.path.exists(lock) and windows.held == {}, "released: unlocked, closed, deleted"


def test_the_msvcrt_lock_covers_the_first_byte_whatever_the_file_position_is(tmp_path, windows):
    path = str(tmp_path / "x.lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    try:
        os.write(fd, b"stale contents")                     # leaves the position at the end
        assert downloader._try_lock(fd) is True
        os.lseek(fd, 5, os.SEEK_SET)
        downloader._unlock(fd)
    finally:
        os.close(fd)
    assert windows.calls == [(windows.LK_NBLCK, 1, 0), (windows.LK_UNLCK, 1, 0)], "locked and unlocked at byte 0"


@pytest.mark.parametrize("code, expected", [
    (errno.EACCES, False),           # a locking violation: someone else holds it
    (errno.EDEADLK, False),
    (errno.EINVAL, None),            # this file system cannot lock (an odd network drive)
    (errno.ENOLCK, None),
])
def test_msvcrt_errors_mean_busy_or_cannot_lock(tmp_path, windows, code, expected):
    windows.errno_for_lock_error = code
    path = str(tmp_path / "x.lock")
    holder = os.open(path, os.O_RDWR | os.O_CREAT)
    second = os.open(path, os.O_RDWR)
    try:
        windows.locking(holder, windows.LK_NBLCK, 1)
        assert downloader._try_lock(second) is expected
    finally:
        os.close(second)
        os.close(holder)


def test_a_lock_file_a_scanner_still_has_open_is_left_for_the_next_holder(tmp_path, windows, monkeypatch):
    """Windows cannot delete a file that another handle has open: release() must shrug that off."""
    lock = str(tmp_path / "x.gguf.lock")
    real_remove = os.remove
    refused = []

    def busy_remove(path, *args, **kwargs):
        if str(path) == lock:
            refused.append(path)
            raise PermissionError(13, "The process cannot access the file because it is being used by another process")
        return real_remove(path, *args, **kwargs)

    monkeypatch.setattr(downloader.os, "remove", busy_remove)
    with downloader._PartLock(lock, threading.Event(), 5):
        pass
    assert refused and os.path.exists(lock), "the delete was refused and nothing broke"
    monkeypatch.setattr(downloader.os, "remove", real_remove)
    with downloader._PartLock(lock, threading.Event(), 5):          # the file that was left is simply locked again
        pass
    assert not os.path.exists(lock)


def test_the_lock_file_is_closed_before_it_is_deleted_on_windows(tmp_path, windows, monkeypatch):
    """The order matters there: an open file cannot be deleted, and a lock held at delete time would
    be released by the delete. (POSIX does the opposite on purpose, see _PartLock.release.)"""
    lock = str(tmp_path / "x.gguf.lock")
    opened, real_open, real_remove = [], os.open, os.remove

    def recording_open(path, *args, **kwargs):
        fd = real_open(path, *args, **kwargs)
        if str(path) == lock:
            info = os.fstat(fd)
            opened.append((fd, (info.st_dev, info.st_ino)))
        return fd

    def strict_remove(path, *args, **kwargs):
        if str(path) == lock:
            for fd, identity in opened:
                try:
                    info = os.fstat(fd)
                except OSError:
                    continue                         # closed
                if (info.st_dev, info.st_ino) == identity:      # (a reused descriptor number is another file)
                    raise PermissionError(13, "still open")
        return real_remove(path, *args, **kwargs)

    monkeypatch.setattr(downloader.os, "open", recording_open)
    monkeypatch.setattr(downloader.os, "remove", strict_remove)
    with downloader._PartLock(lock, threading.Event(), 5):
        assert os.path.exists(lock)
    assert not os.path.exists(lock), "deleted, after the descriptor was closed"


class Wait:
    """A clock and a sleep that move together, so a five-minute patience takes no time."""

    def __init__(self):
        self.now, self.pauses = 0.0, []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.pauses.append(seconds)
        self.now += seconds


def busy_error():
    return PermissionError(13, "The process cannot access the file because it is being used by another process")


def test_a_rename_that_windows_refuses_while_a_scanner_has_the_file_is_tried_again(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "_is_windows", lambda: True)
    source, target = tmp_path / "m.gguf.part", tmp_path / "m.gguf"
    source.write_bytes(b"x" * 10)
    refusals = [busy_error(), busy_error(), busy_error()]
    real_replace = os.replace

    def flaky(a, b):
        if refusals:
            raise refusals.pop()
        return real_replace(a, b)

    monkeypatch.setattr(downloader.os, "replace", flaky)
    wait = Wait()
    downloader._replace_file(str(source), str(target), sleep=wait.sleep, clock=wait.clock)
    assert target.read_bytes() == b"x" * 10 and not source.exists()
    assert wait.pauses == [0.05, 0.1, 0.2], "growing pauses"


def test_the_pauses_stop_growing_and_the_wait_ends_with_the_error(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "_is_windows", lambda: True)
    monkeypatch.setattr(downloader.os, "replace", lambda a, b: (_ for _ in ()).throw(busy_error()))
    wait = Wait()
    with pytest.raises(PermissionError):
        downloader._replace_file("a", "b", patience=30.0, sleep=wait.sleep, clock=wait.clock)
    assert max(wait.pauses) == downloader._BUSY_LAST_PAUSE and sum(wait.pauses) >= 30.0


def test_on_posix_a_refused_rename_is_an_error_at_once(monkeypatch):
    monkeypatch.setattr(downloader, "_is_windows", lambda: False)
    calls = []
    monkeypatch.setattr(downloader.os, "replace", lambda a, b: (calls.append(1), (_ for _ in ()).throw(busy_error()))[1])
    wait = Wait()
    with pytest.raises(PermissionError):
        downloader._replace_file("a", "b", sleep=wait.sleep, clock=wait.clock)
    assert calls == [1] and wait.pauses == []


def test_on_windows_an_error_that_is_not_about_a_busy_file_is_not_waited_for(monkeypatch):
    monkeypatch.setattr(downloader, "_is_windows", lambda: True)
    calls = []
    monkeypatch.setattr(downloader.os, "replace", lambda a, b: (calls.append(1), (_ for _ in ()).throw(FileNotFoundError(2, "gone")))[1])
    wait = Wait()
    with pytest.raises(FileNotFoundError):
        downloader._replace_file("a", "b", sleep=wait.sleep, clock=wait.clock)
    assert calls == [1] and wait.pauses == []


def test_waiting_for_a_busy_file_can_be_cancelled(monkeypatch):
    monkeypatch.setattr(downloader, "_is_windows", lambda: True)
    monkeypatch.setattr(downloader.os, "replace", lambda a, b: (_ for _ in ()).throw(busy_error()))
    cancel = threading.Event()
    wait = Wait()

    def cancelling_sleep(seconds):
        wait.sleep(seconds)
        cancel.set()

    with pytest.raises(downloader._Cancelled):
        downloader._replace_file("a", "b", cancel, sleep=cancelling_sleep, clock=wait.clock)
    assert len(wait.pauses) == 1


def test_a_whole_download_survives_a_scanner_holding_the_finished_part(server, dest, monkeypatch):
    """The last step of every download is the rename of the verified .part: on Windows that is exactly
    when Defender is scanning the 18 GB it just saw closed."""
    monkeypatch.setattr(downloader, "_is_windows", lambda: True)
    entry, blob = one_part(server)
    real_replace = os.replace
    refused = []

    def flaky(source, target):
        if str(source).endswith(".gguf.part") and len(refused) < 2:
            refused.append(source)
            raise busy_error()
        return real_replace(source, target)

    monkeypatch.setattr(downloader.os, "replace", flaky)
    result = fetch(server, [entry], dest)
    assert result.ok and len(refused) == 2
    assert (dest / entry["file"]).read_bytes() == blob
    assert bookkeeping(dest) == []


def test_a_lock_file_whose_delete_is_still_pending_can_be_made_again(tmp_path, monkeypatch):
    """Windows answers "access denied" to creating a file whose delete a scanner is holding up."""
    monkeypatch.setattr(downloader, "_is_windows", lambda: True)
    lock = str(tmp_path / "x.gguf.lock")
    real_open, refused = os.open, []

    def pending(path, *args, **kwargs):
        if str(path) == lock and len(refused) < 3:
            refused.append(path)
            raise PermissionError(13, "Access is denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(downloader.os, "open", pending)
    with downloader._PartLock(lock, threading.Event(), 5):
        pass
    assert len(refused) == 3


def test_on_posix_a_lock_file_that_cannot_be_opened_is_an_error_at_once(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "_is_windows", lambda: False)
    lock, calls = str(tmp_path / "x.gguf.lock"), []

    def refuse(path, *args, **kwargs):
        calls.append(path)
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(downloader.os, "open", refuse)
    with pytest.raises(PermissionError):
        downloader._PartLock(lock, threading.Event(), 5).acquire()
    assert len(calls) == 1


# ======================================================================================
# The existing helpers are untouched by this
# ======================================================================================

def test_the_engine_sync_helpers_are_still_there():
    for name in ("sync_rpc_bin", "fetch_verified_manifest", "download_and_extract_engine",
                 "test_binary_compatibility"):
        assert callable(getattr(downloader, name))
