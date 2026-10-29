"""The worker process mode (--run-worker), including the frozen-app case that engine/config.py would otherwise get wrong."""

import os
import shutil
import subprocess
import sys

from conftest import DESKTOP, REPO


def make_engine(tmp_path, body):
    root = tmp_path / "engine-root"
    (root / "engine").mkdir(parents=True)
    shutil.copy(REPO / "engine" / "config.py", root / "engine" / "config.py")
    (root / "engine" / "__init__.py").write_text("")
    (root / "dlne_server.py").write_text(body, encoding="utf-8")
    return root


def run(tmp_path, root, args, frozen):
    runner = tmp_path / "runner.py"
    runner.write_text(
        "import sys\n"
        + ("sys.frozen = True\n" if frozen else "")
        + "from dlne_donate.__main__ import main\n"
        + f"sys.exit(main({['--run-worker', str(root), '--'] + args!r}))\n", encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(DESKTOP), PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, str(runner)], capture_output=True, text=True, env=env, timeout=60, cwd=str(tmp_path))


SERVER = '''
import sys
from engine import config
print("BASE", config.get_base_dir())
print("CONFIG", config.CONFIG_FILE)
print("ARGS", sys.argv[1:])
if sys.argv[1:2] == ["exit75"]:
    sys.exit(75)
'''


def test_a_frozen_engine_still_uses_the_verified_folder_as_its_root(tmp_path):
    root = make_engine(tmp_path, SERVER)
    done = run(tmp_path, root, ["worker", "--coordinator", "https://x"], frozen=True)
    assert done.returncode == 0, done.stderr
    lines = dict(line.split(" ", 1) for line in done.stdout.splitlines() if " " in line)
    assert os.path.abspath(lines["BASE"]) == os.path.abspath(root)
    assert os.path.abspath(lines["CONFIG"]) == os.path.abspath(root / "config" / "config.txt")
    assert "['worker', '--coordinator', 'https://x']" in lines["ARGS"]


def test_an_exit_code_of_the_engine_reaches_the_controller(tmp_path):
    root = make_engine(tmp_path, SERVER)
    assert run(tmp_path, root, ["exit75"], frozen=False).returncode == 75


def test_a_worker_whose_app_died_ends_itself(tmp_path):
    """The app was killed without stopping its worker: the worker must not go on lending the computer."""
    import time

    import psutil

    root = make_engine(tmp_path, "import time\nprint('up', flush=True)\ntime.sleep(120)\n")
    app = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    runner = tmp_path / "runner.py"
    runner.write_text("import sys\nfrom dlne_donate import worker\nworker.PARENT_CHECK_S = 0.2\n"
                      "from dlne_donate.__main__ import main\n"
                      f"sys.exit(main(['--run-worker', {str(root)!r}, '--', 'worker']))\n", encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(DESKTOP), DLNE_DONATE_PARENT=str(app.pid))
    child = subprocess.Popen([sys.executable, str(runner)], stdout=subprocess.PIPE, text=True, env=env, cwd=str(tmp_path))
    try:
        assert child.stdout.readline().strip() == "up"
        assert child.poll() is None                        # the app is alive: the worker stays
        time.sleep(1.0)
        assert child.poll() is None
        app.kill()
        app.wait()
        child.wait(timeout=15)                             # the app is gone: the worker ends itself
        assert child.returncode is not None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        if app.poll() is None:
            app.kill()
    assert not psutil.pid_exists(child.pid) or psutil.Process(child.pid).status() == psutil.STATUS_ZOMBIE


def test_a_missing_engine_is_an_error_not_a_crash(tmp_path):
    done = run(tmp_path, tmp_path / "nope", ["worker"], frozen=False)
    assert done.returncode == 2 and "is missing" in done.stderr


def test_the_run_worker_mode_needs_the_separator(tmp_path):
    runner = tmp_path / "r.py"
    runner.write_text("import sys\nfrom dlne_donate.__main__ import main\nsys.exit(main(['--run-worker', 'x', 'worker']))\n")
    done = subprocess.run([sys.executable, str(runner)], capture_output=True, text=True, env=dict(os.environ, PYTHONPATH=str(DESKTOP)))
    assert done.returncode == 2
