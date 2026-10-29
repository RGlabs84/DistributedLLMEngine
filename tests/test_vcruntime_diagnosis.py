"""
A llama.cpp program that exits 0xC0000135 on Windows (STATUS_DLL_NOT_FOUND) is missing the
Visual C++ Runtime, not a CPU feature. engine/downloader.py must say so, link the installer,
and never blame AVX2 for it.
"""

import subprocess

import pytest

from engine import downloader

CODE = 3221225781  # 0xC0000135


class _Result:
    def __init__(self, code, err=""):
        self.returncode = code
        self.stderr = err
        self.stdout = ""


@pytest.fixture
def logs(monkeypatch):
    lines = []
    monkeypatch.setattr(downloader, "log_error", lines.append)
    monkeypatch.setattr(downloader, "log_system", lines.append)
    return lines


def _windows(monkeypatch, runtime_present):
    monkeypatch.setattr(downloader.platform, "system", lambda: "Windows")
    monkeypatch.setattr(downloader, "vcruntime_present", lambda bin_dir="": runtime_present)
    # SetErrorMode lives in kernel32, which a Linux test machine does not have.
    import ctypes
    monkeypatch.setattr(ctypes, "windll", type("W", (), {"kernel32": type("K", (), {"SetErrorMode": staticmethod(lambda m: 0)})})(), raising=False)


def test_both_spellings_of_the_code_are_recognised():
    assert downloader.is_dll_not_found(0xC0000135)
    assert downloader.is_dll_not_found(-1073741515)
    assert not downloader.is_dll_not_found(0)
    assert not downloader.is_dll_not_found(3221225501)  # illegal instruction is another thing
    assert not downloader.is_dll_not_found(None)


def test_the_message_names_the_runtime_and_the_official_link():
    assert downloader.VCREDIST_URL == "https://aka.ms/vs/17/release/vc_redist.x64.exe"
    assert downloader.VCREDIST_URL in downloader.VCREDIST_TEXT
    assert "Visual C++" in downloader.VCREDIST_TEXT
    assert "AVX" not in downloader.VCREDIST_TEXT


@pytest.mark.parametrize("code", [CODE, -1073741515])
def test_a_missing_runtime_on_windows_is_diagnosed_and_marked(monkeypatch, logs, capsys, code):
    _windows(monkeypatch, runtime_present=False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Result(code))
    assert downloader.test_binary_compatibility(r"C:\dlne\bin\llama-server.exe") is False
    text = "\n".join(logs)
    assert "Visual C++" in text and downloader.VCREDIST_URL in text
    assert "AVX" not in text
    assert downloader.VCREDIST_MARKER in capsys.readouterr().out


def test_a_dll_code_with_the_runtime_present_is_not_blamed_on_it_or_on_the_cpu(monkeypatch, logs, capsys):
    _windows(monkeypatch, runtime_present=True)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Result(CODE))
    assert downloader.test_binary_compatibility(r"C:\dlne\bin\ggml-rpc-server.exe") is False
    text = "\n".join(logs)
    assert "DLL" in text and "AVX" not in text
    assert downloader.VCREDIST_MARKER not in capsys.readouterr().out


def test_the_final_error_does_not_mention_avx2_for_a_missing_runtime(monkeypatch, tmp_path, logs):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ggml-rpc-server.exe").write_bytes(b"MZ")
    _windows(monkeypatch, runtime_present=False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Result(CODE))
    import engine.config as config
    monkeypatch.setattr(config, "get_base_dir", lambda: str(tmp_path))
    assert downloader.download_and_extract_engine("worker") is None
    final = [line for line in logs if line.startswith("FATAL")]
    assert len(final) == 1
    assert "Visual C++" in final[0] and downloader.VCREDIST_URL in final[0]
    assert "supports AVX2" not in final[0]


def test_a_real_illegal_instruction_still_says_avx(monkeypatch, tmp_path, logs):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ggml-rpc-server").write_bytes(b"x")
    monkeypatch.setattr(downloader.platform, "system", lambda: "Linux")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Result(132))
    import engine.config as config
    monkeypatch.setattr(config, "get_base_dir", lambda: str(tmp_path))
    assert downloader.download_and_extract_engine("worker") is None
    assert any("AVX" in line for line in logs)


def test_runtime_probe_is_true_off_windows(monkeypatch):
    monkeypatch.setattr(downloader.platform, "system", lambda: "Linux")
    assert downloader.vcruntime_present() is True


def test_app_local_runtime_counts_as_present(monkeypatch, tmp_path):
    monkeypatch.setattr(downloader.platform, "system", lambda: "Windows")
    (tmp_path / "vcruntime140.dll").write_bytes(b"x")
    (tmp_path / "msvcp140.dll").write_bytes(b"x")
    assert downloader.vcruntime_present(str(tmp_path)) is True
