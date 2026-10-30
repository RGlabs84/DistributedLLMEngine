"""
2.7.0 engine flavours in the tray: the worker's DLNE_CUDA / DLNE_ENGINE lines (parsed like the extension's), the CUDA question
and its consent (3 days, never), how the flavour is resolved, and the settings that carry it.
"""

import json
from dataclasses import replace

import pytest

from dlne_donate import engine_flavour as ef, markers, paths, settings as settings_store, tiers, trayicon, worker
from dlne_donate.tiers import Hardware, Settings

HW = Hardware(gpu_name="RTX 4070", vram_gb=8, gpu_count=1, total_ram_gb=32, physical_cores=8, logical_threads=16)
APP = tiers.app_tiers_from_status(None)
CAPABLE = markers.Cuda(capable=True, gpu="NVIDIA GeForce RTX 4070 Laptop GPU", compute_cap="8.9", driver="615.78")
OLD_GPU = markers.Cuda(capable=False, reason="this GPU is older than Turing (compute capability 6.1)")
DAY = 24 * 3600


# ---- the marker lines (the same cases as the extension's parser) -------------------------------------------------------

def test_the_cuda_line_is_read_the_way_the_contract_prints_it():
    line = ('DLNE_CUDA: {"capable": true, "reason": "", "gpu": "NVIDIA GeForce RTX 4070 Laptop GPU", '
            '"compute_cap": "8.9", "driver": "615.78"}\n')
    assert markers.parse_cuda(line) == CAPABLE
    no = markers.parse_cuda('x\nDLNE_CUDA: {"capable": false, "reason": "no NVIDIA GPU", "gpu": "", "compute_cap": "", "driver": ""}\ny')
    assert no == markers.Cuda(capable=False, reason="no NVIDIA GPU")


def test_a_cuda_line_that_cannot_be_trusted_is_ignored_and_the_last_good_one_wins():
    assert markers.parse_cuda("DLNE_CUDA: junk\n") is None
    assert markers.parse_cuda('DLNE_CUDA: {"capable": "yes"}\n') is None, "capable must be true or false"
    assert markers.parse_cuda('DLNE_CUDA: {"reason": "x"}\n') is None
    assert markers.parse_cuda('DLNE_CUDA: [1]\n') is None
    both = 'DLNE_CUDA: {"capable": false, "reason": "a"}\nDLNE_CUDA: {"capable": true}\nDLNE_CUDA: nonsense\n'
    assert markers.parse_cuda(both) == markers.Cuda(capable=True)
    odd = markers.parse_cuda('DLNE_CUDA: {"capable": false, "gpu": 5, "driver": ["x"], "reason": "r' + "z" * 400 + '"}\n')
    assert odd.gpu == "" and odd.driver == "" and len(odd.reason) == 200, "wrong types dropped, long text cut"


def test_the_parsers_keep_what_the_extensions_parsers_keep():
    """Parity with vscode-extension/src/worker/contribution.ts parseCudaProbe / parseEngineReport (builder C, wip/2.7-C)."""
    # text is trimmed; the reason is kept only when CUDA is not possible; limits 120 / 16 / 32 / 200
    c = markers.parse_cuda('DLNE_CUDA: {"capable": true, "reason": "stale", "gpu": "  RTX  ", "compute_cap": " 8.9 ", "driver": " 615.78 "}')
    assert c == markers.Cuda(capable=True, reason="", gpu="RTX", compute_cap="8.9", driver="615.78")
    c = markers.parse_cuda('DLNE_CUDA: {"capable": false, "reason": "  no NVIDIA GPU  ", "gpu": "' + "g" * 300 + '", "compute_cap": "' + "1" * 40 + '"}')
    assert c.reason == "no NVIDIA GPU" and len(c.gpu) == 120 and len(c.compute_cap) == 16
    assert len(markers.parse_cuda('DLNE_CUDA: {"capable": true, "driver": "' + "d" * 90 + '"}').driver) == 32
    # a CUDA engine has no fall-back reason; a Vulkan one keeps it, trimmed, up to 300
    assert markers.parse_engine('DLNE_ENGINE: {"flavour": "cuda", "fallback": "left over"}') == markers.EngineRun("cuda", "")
    v = markers.parse_engine('DLNE_ENGINE: {"flavour": "vulkan", "fallback": " ' + "w" * 400 + ' "}')
    assert v.flavour == "vulkan" and len(v.fallback) == 300
    # the marker may sit after a log prefix; lines are cut at \n or \r\n only (str.splitlines would also cut at U+2028)
    assert markers.parse_engine('12:00:01 INFO DLNE_ENGINE: {"flavour": "cuda"}\r\n').flavour == "cuda"
    assert markers.parse_cuda('DLNE_CUDA: {"capable": true, "gpu": "RTX Laptop"}').gpu == "RTX Laptop"
    assert markers.parse_cuda('junk\nDLNE_CUDA: {"capable": true}') == markers.Cuda(capable=True)


def test_the_engine_line_names_the_flavour_and_why_cuda_did_not_start():
    assert markers.parse_engine('DLNE_ENGINE: {"flavour": "cuda", "fallback": ""}\n') == markers.EngineRun("cuda", "")
    fell = markers.parse_engine('DLNE_ENGINE: {"flavour": "vulkan", "fallback": "no CUDA device"}\n')
    assert fell == markers.EngineRun("vulkan", "no CUDA device")
    assert markers.parse_engine('DLNE_ENGINE: {"flavour": "metal", "fallback": ""}\n') is None
    assert markers.parse_engine('DLNE_ENGINE: {"fallback": "x"}\n') is None
    assert markers.parse_engine("DLNE_ENGINE: junk\n") is None
    assert markers.parse_engine('DLNE_ENGINE: {"flavour": "cuda"}\n') == markers.EngineRun("cuda", "")


def test_the_reader_reports_a_cuda_or_engine_line_once_and_again_on_change():
    reader = markers.OutputReader()
    line = 'DLNE_CUDA: {"capable": true, "reason": ""}\n'
    assert reader.feed(line)["cuda"].capable is True and reader.feed(line) == {}
    assert reader.feed('DLNE_CUDA: {"capable": false, "reason": "driver 572.16 is older than CUDA 13 needs (580 or later)"}\n')["cuda"].capable is False
    assert reader.feed('DLNE_ENGINE: {"flavour": "cuda", "fallback": ""}\n')["engine"] == markers.EngineRun("cuda")
    assert reader.feed('DLNE_ENGINE: {"flavour": "cuda", "fallback": ""}\n') == {}
    assert reader.feed('DLNE_ENGINE: {"flavour": "vulkan", "fallback": "x"}\n')["engine"].fallback == "x"
    cut = markers.OutputReader()
    assert cut.feed('DLNE_ENGINE: {"flavour": "cu') == {}, "a line cut by the pipe waits for its end"
    assert cut.feed('da", "fallback": ""}\r\n')["engine"].flavour == "cuda"


def test_the_running_on_line():
    assert ef.engine_line(None) == ""
    assert ef.engine_line(markers.EngineRun("cuda")) == "Running on CUDA"
    assert ef.engine_line(markers.EngineRun("vulkan")) == "Running on Vulkan"
    assert ef.engine_line(markers.EngineRun("vulkan", "no CUDA device")) == "CUDA could not start: no CUDA device; running on Vulkan"


def test_the_version_carries_the_flavour_as_display_only_metadata():
    assert ef.flavour_label("2.7.0", "cuda") == "2.7.0+cuda"
    assert ef.flavour_label("2.7.0", "vulkan") == "2.7.0+vulkan"
    assert ef.flavour_label("2.7.0", "") == "2.7.0" and ef.flavour_label("", "cuda") == ""
    status = worker.Status(engine_version="2.7.0", engine=markers.EngineRun("cuda"))
    assert status.version_label == "2.7.0+cuda"
    assert trayicon.version_line(status).endswith("engine 2.7.0+cuda")
    assert "engine" not in trayicon.version_line(worker.Status()), "no engine yet: only the app's own version"
    assert worker.Status(engine_version="2.7.0").version_label == "2.7.0", "flavour not yet known"


def test_the_tooltip_names_a_cuda_engine():
    status = worker.Status(state=worker.DONATING, engine=markers.EngineRun("cuda"), role=markers.Role("coding", model="Qwen3.6.gguf"))
    assert status.tooltip.startswith("DLNE Donate: donating (CUDA)\nCoding member")
    assert worker.Status(state=worker.DONATING).tooltip.startswith("DLNE Donate: donating\n")


# ---- resolving the flavour ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("engine,consent,cuda,failed,expected", [
    ("auto", "unasked", CAPABLE, False, "vulkan"),         # Vulkan until the owner agrees
    ("auto", "later", CAPABLE, False, "vulkan"),
    ("auto", "never", CAPABLE, False, "vulkan"),
    ("auto", "yes", CAPABLE, False, "cuda"),                # consent and a computer that can
    ("auto", "yes", OLD_GPU, False, "vulkan"),              # agreed, but this computer cannot
    ("auto", "yes", None, False, "vulkan"),                 # agreed, but the worker has not said yet this time: Vulkan, then CUDA
    ("auto", "yes", CAPABLE, True, "vulkan"),               # CUDA failed its start-up check: stay on Vulkan
    ("vulkan", "yes", CAPABLE, False, "vulkan"),
    ("cuda", "yes", CAPABLE, False, "cuda"),
    ("cuda", "yes", None, False, "vulkan"),
    ("cuda", "unasked", CAPABLE, False, "vulkan"),          # the setting alone is not consent (a hand-edited file)
    ("cuda", "never", CAPABLE, False, "vulkan"),
    ("cuda", "yes", OLD_GPU, False, "vulkan"),
    ("cuda", "yes", CAPABLE, True, "vulkan"),
    ("auto", "unasked", None, False, "vulkan"),             # the very first start of any machine
])
def test_the_flavour_is_resolved_like_the_extension_does_it(engine, consent, cuda, failed, expected):
    assert ef.resolve_flavour(engine, consent, cuda, failed) == expected
    assert ef.resolve_flavour(engine, consent, cuda, failed, lends_gpu=False) == "vulkan", "a CPU-only donor runs no GPU engine"


def test_the_worker_is_started_with_its_flavour_in_its_environment():
    assert worker.worker_environment("", "cuda")["DLNE_ENGINE_FLAVOR"] == "cuda"
    assert worker.worker_environment("", "vulkan")["DLNE_ENGINE_FLAVOR"] == "vulkan"
    assert worker.worker_environment()["DLNE_ENGINE_FLAVOR"] == "vulkan"
    assert worker.worker_environment("", "rocm")["DLNE_ENGINE_FLAVOR"] == "vulkan", "anything else is vulkan"


def test_the_choice_window_greys_cuda_out_with_the_reason():
    assert ef.cuda_choice(CAPABLE) == (True, "")
    assert ef.cuda_choice(OLD_GPU) == (False, "this GPU is older than Turing (compute capability 6.1)")
    assert ef.cuda_choice(markers.Cuda(capable=False)) == (False, "this computer cannot run the CUDA engine")
    ok, why = ef.cuda_choice(None)
    assert not ok and "first time you donate" in why


# ---- the question and its consent --------------------------------------------------------------------------------------

def test_the_question_has_the_contracts_words_and_three_answers():
    assert ef.QUESTION_TEXT == ("Your NVIDIA GPU can run DLNE about 30-50% faster with the CUDA engine. It is a one-time "
                                "download of about 600 MB. Use CUDA?")
    assert [ef.ANSWER_LABELS[a] for a in (ef.ANSWER_USE, ef.ANSWER_LATER, ef.ANSWER_NEVER)] == ["Use CUDA", "Not now", "Keep Vulkan"]


def test_the_question_is_asked_only_when_capable_and_auto_and_unanswered():
    now = 1_000_000.0
    assert ef.question_due("auto", "unasked", 0, CAPABLE, now)
    assert not ef.question_due("auto", "unasked", 0, None, now), "the worker has not said yet"
    assert not ef.question_due("auto", "unasked", 0, OLD_GPU, now)
    assert not ef.question_due("vulkan", "unasked", 0, CAPABLE, now) and not ef.question_due("cuda", "unasked", 0, CAPABLE, now)
    assert not ef.question_due("auto", "yes", 0, CAPABLE, now) and not ef.question_due("auto", "never", 0, CAPABLE, now)


def test_not_now_asks_again_after_three_days_and_not_before():
    now = 1_700_000_000.0
    later = ef.answer_changes(ef.ANSWER_LATER, now)
    assert later["cuda_consent"] == "later" and later["cuda_later_ms"] == int((now + 3 * DAY) * 1000)
    ask = lambda t: ef.question_due("auto", later["cuda_consent"], later["cuda_later_ms"], CAPABLE, t)
    assert not ask(now) and not ask(now + 3 * DAY - 1)
    assert ask(now + 3 * DAY) and ask(now + 30 * DAY)
    assert ef.effective_consent("later", later["cuda_later_ms"], now + DAY) == "later"
    assert ef.effective_consent("later", later["cuda_later_ms"], now + 4 * DAY) == "unasked"


def test_keep_vulkan_is_never_asked_again_and_use_cuda_is_consent():
    never = ef.answer_changes(ef.ANSWER_NEVER, 5.0)
    assert never == {"cuda_consent": "never", "cuda_later_ms": 0}
    assert not ef.question_due("auto", "never", 0, CAPABLE, 10 ** 12), "not even years later"
    use = ef.answer_changes(ef.ANSWER_USE, 5.0, engine="auto")
    assert use == {"cuda_consent": "yes", "cuda_later_ms": 0, "engine": "auto"}, "the question keeps the setting on auto"
    assert ef.answer_changes(ef.ANSWER_USE, 5.0, choosing_cuda=True)["engine"] == "cuda", "choosing CUDA in Settings sets it"
    assert ef.answer_changes(ef.ANSWER_NEVER, 5.0, choosing_cuda=True, engine="vulkan").get("engine") is None
    assert ef.answer_changes("close", 5.0) == {}


# ---- settings: the engine and the consent, kept in the settings file -------------------------------------------------

def test_the_engine_and_consent_round_trip_through_the_settings_file(app_home):
    path = paths.settings_path()
    for consent, later_ms in (("unasked", 0), ("yes", 0), ("never", 0), ("later", 1_800_000_000_000)):
        for engine in ef.ENGINE_SETTINGS:
            saved = replace(tiers.default_settings(HW, APP), engine=engine, cuda_consent=consent, cuda_later_ms=later_ms)
            settings_store.save(saved, path)
            assert settings_store.load(HW, APP, path) == saved
    settings_store.save(replace(tiers.default_settings(HW, APP), engine="cuda", cuda_consent="later", cuda_later_ms=1_800_000_000_000), path)
    on_disk = json.loads(open(path, encoding="utf-8").read())
    assert on_disk["engine"] == "cuda" and on_disk["cuda_consent"] == {"later": 1_800_000_000_000}, "the extension's shapes"
    settings_store.save(replace(tiers.default_settings(HW, APP), cuda_consent="yes"), path)
    assert json.loads(open(path, encoding="utf-8").read())["cuda_consent"] == "yes"


def test_an_old_settings_file_and_a_damaged_one_fall_back_to_auto_unasked(app_home):
    path = paths.settings_path()
    old = tiers.settings_to_json(tiers.default_settings(HW, APP))
    del old["engine"], old["cuda_consent"]
    open(path, "w", encoding="utf-8").write(json.dumps(old))
    loaded = settings_store.load(HW, APP, path)
    assert (loaded.engine, loaded.cuda_consent, loaded.cuda_later_ms) == ("auto", "unasked", 0)
    for engine, consent in (("rocm", "maybe"), (5, 7), (None, {"later": "soon"}), ("CUDA", {"later": -1}), ("", {"later": True})):
        old.update(engine=engine, cuda_consent=consent)
        open(path, "w", encoding="utf-8").write(json.dumps(old))
        loaded = settings_store.load(HW, APP, path)
        assert (loaded.engine, loaded.cuda_consent, loaded.cuda_later_ms) == ("auto", "unasked", 0), (engine, consent)


def test_settings_changed_in_memory_are_cleaned_too():
    odd = replace(tiers.default_settings(HW, APP), engine="rocm", cuda_consent="later", cuda_later_ms=0)
    clean = tiers.clamp_settings(odd, HW, APP)
    assert (clean.engine, clean.cuda_consent, clean.cuda_later_ms) == ("auto", "unasked", 0)


# ---- the model: the question, the choice in Settings -----------------------------------------------------------------

class FakeController:
    """Just enough of the controller for the model; ``status`` carries what the worker said about CUDA."""

    def __init__(self, home, get_settings, get_hardware, **kwargs):
        self.on_change = kwargs["on_change"]
        self.s = worker.Status()
        self.applied = 0

    def apply_settings(self):
        self.applied += 1

    def status(self):
        return self.s

    def set_paused(self, paused):
        self.s = replace(self.s, paused=paused)


def make_model(app_home, clock, hardware=HW):
    from dlne_donate.model import Model

    return Model(str(app_home), hardware=hardware, controller_factory=FakeController, fetch_info=False,
                 editor_present=lambda: False, now=lambda: clock["now"])


def test_the_model_asks_once_and_remembers_each_answer(app_home):
    clock = {"now": 1_700_000_000.0}
    model = make_model(app_home, clock)
    assert model.settings.gpu_enabled
    assert not model.cuda_question_due(), "the worker has not said it can run CUDA"
    model.controller.s = replace(model.controller.s, cuda=CAPABLE)
    assert model.cuda_question_due()

    model.answer_cuda(ef.ANSWER_LATER)
    assert not model.cuda_question_due()
    clock["now"] += 3 * DAY - 5
    assert not model.cuda_question_due()
    clock["now"] += 10
    assert model.cuda_question_due(), "three days after Not now"
    saved = json.loads(open(paths.settings_path(str(app_home)), encoding="utf-8").read())
    assert "later" in saved["cuda_consent"]

    model.answer_cuda(ef.ANSWER_NEVER)
    clock["now"] += 365 * DAY
    assert not model.cuda_question_due() and model.settings.cuda_consent == "never"
    assert model.controller.applied >= 2, "a running worker is told so it can switch"


def test_use_cuda_is_consent_and_the_worker_is_told(app_home):
    model = make_model(app_home, {"now": 1.0})
    model.controller.s = replace(model.controller.s, cuda=CAPABLE)
    before = model.controller.applied
    model.answer_cuda(ef.ANSWER_USE)
    assert (model.settings.cuda_consent, model.settings.engine) == ("yes", "auto")
    assert model.controller.applied == before + 1
    assert ef.resolve_flavour(model.settings.engine, model.settings.cuda_consent, CAPABLE) == "cuda"
    assert not model.cuda_question_due()


def test_no_question_for_a_computer_that_does_not_lend_its_gpu(app_home):
    model = make_model(app_home, {"now": 1.0})
    model.controller.s = replace(model.controller.s, cuda=CAPABLE)
    model.update(gpu_enabled=False)
    assert not model.cuda_question_due()


def test_choosing_cuda_in_settings_asks_first_and_only_use_cuda_turns_it_on(app_home):
    model = make_model(app_home, {"now": 1_700_000_000.0})
    asked = []
    for answer, engine, consent in ((ef.ANSWER_LATER, "auto", "later"), (ef.ANSWER_NEVER, "auto", "never"), (ef.ANSWER_USE, "cuda", "yes")):
        model.choose_engine("cuda", lambda a=answer: asked.append(a) or a)
        assert (model.settings.engine, model.settings.cuda_consent) == (engine, consent)
    assert asked == [ef.ANSWER_LATER, ef.ANSWER_NEVER, ef.ANSWER_USE]
    model.choose_engine("vulkan", lambda: pytest.fail("Vulkan needs no question"))
    assert model.settings.engine == "vulkan"
    model.choose_engine("cuda", lambda: pytest.fail("already agreed: no second question"))
    assert model.settings.engine == "cuda"
    model.choose_engine("auto", lambda: pytest.fail("Auto needs no question"))
    assert model.settings.engine == "auto"


def test_the_state_file_says_which_engine_runs(app_home):
    model = make_model(app_home, {"now": 1.0})
    model._write_state(worker.Status(state=worker.DONATING, engine=markers.EngineRun("cuda")))
    state = json.loads(open(paths.state_path(str(app_home)), encoding="utf-8").read())
    assert state["engine_flavour"] == "cuda" and state["paused"] is False


# ---- the app asks the question once, in its window -----------------------------------------------------------------------

def test_the_app_shows_the_window_and_records_the_answer(tmp_path):
    from dlne_donate.app import App

    class Window:
        def __init__(self):
            self.events = []

        def show(self, near_pointer=False):
            self.events.append("show")

        def ask_cuda(self):
            self.events.append("ask")
            return ef.ANSWER_USE

    class Model:
        answers = []

        def answer_cuda(self, answer):
            self.answers.append(answer)

    app = App(home=str(tmp_path))
    app.model, app.window = Model(), Window()
    app._asking_cuda = True
    app._ask_cuda()
    assert app.window.events == ["show", "ask"] and Model.answers == [ef.ANSWER_USE]
    assert app._asking_cuda is False, "free to ask again (when it is due again)"
