"""
Logging must never be able to take down its caller.

Rich reads square brackets as style markup. A message holding an unbalanced tag —
whether hand-written, or quoted out of an exception, a model name, a file path — makes
the logging call itself raise. That is how the update-check background task died: it
logged a banner whose closing tag lived in a separate log record, and the handler that
tried to report the failure then quoted the same broken text and died too, so the
original error was never printed at all.
"""

import logging

import pytest

from engine import logger as dlne_logger


@pytest.fixture
def captured(monkeypatch):
    """Collects what each log helper hands to the stdlib logger, without printing."""
    records = []

    class _Recorder:
        def info(self, msg):
            records.append(("info", msg))

        def warning(self, msg):
            records.append(("warning", msg))

        def error(self, msg):
            records.append(("error", msg))

    recorder = _Recorder()
    monkeypatch.setattr(dlne_logger, "sys_logger", recorder)
    monkeypatch.setattr(dlne_logger, "worker_logger", recorder)
    monkeypatch.setattr(dlne_logger, "coord_logger", recorder)
    return records


# --------------------------------------------------------------- _markup_safe


def test_balanced_markup_is_left_alone():
    """Deliberate styling still reaches Rich, so log lines keep their colour."""
    text = "[bold yellow]*** NEW MODEL REQUEST ***[/bold yellow]"
    assert dlne_logger._markup_safe(text) == text


def test_an_orphan_closing_tag_is_escaped_rather_than_raising():
    escaped = dlne_logger._markup_safe("=" * 50 + "[/bold yellow]")
    assert escaped.endswith(r"\[/bold yellow]")
    # every occurrence is the escaped form, so Rich sees no tag at all
    assert escaped.count("[/bold yellow]") == escaped.count(r"\[/bold yellow]")


def test_an_unclosed_opening_tag_is_accepted():
    """Rich closes dangling tags itself; only orphan closers are malformed."""
    text = "[bold yellow]" + "=" * 50
    assert dlne_logger._markup_safe(text) == text


def test_square_brackets_in_untrusted_text_never_raise():
    """Worker names, model names and file paths all reach the log verbatim."""
    for hostile in (
        "model [q4_0] failed",
        "[/danger] injected",
        "C:\\models\\[phi3]\\weights.gguf",
        "[",
        "]",
        "[[",
        "[unclosed",
    ):
        assert isinstance(dlne_logger._markup_safe(hostile), str)


def test_non_string_messages_are_coerced():
    assert dlne_logger._markup_safe(ValueError("boom")) == "boom"
    assert dlne_logger._markup_safe(42) == "42"


def test_the_exact_failure_that_killed_the_update_check():
    """
    The regression, end to end: a task raises, and the reporting path quotes the
    exception text — which itself carries the orphan tag that caused the failure.
    """
    quoted = (
        "Background task 'update_check' failed: MarkupError: closing tag "
        "'[/bold yellow]' at position 73 doesn't match any open tag"
    )
    safe = dlne_logger._markup_safe(quoted)
    assert "position 73" in safe
    assert r"\[/bold yellow]" in safe


# ------------------------------------------------------------- the log helpers


@pytest.mark.parametrize(
    "helper, level, style",
    [
        ("log_system", "info", "info"),
        ("log_worker", "info", "worker"),
        ("log_coordinator", "info", "coordinator"),
        ("log_success", "info", "success"),
        ("log_error", "error", "danger"),
        ("log_warning", "warning", "warning"),
    ],
)
def test_every_helper_wraps_in_its_style_and_survives_broken_markup(
    captured, helper, level, style
):
    getattr(dlne_logger, helper)("boom [/bold yellow] boom")
    assert len(captured) == 1
    got_level, message = captured[0]
    assert got_level == level
    assert message.startswith(f"[{style}]")
    assert message.endswith(f"[/{style}]")
    # the payload was defanged, so the wrapper's own tags still pair up
    assert dlne_logger._markup_safe(message) == message


def test_error_path_still_reports_when_the_message_is_hostile(captured):
    """log_error is the last line of defence; it must not be the thing that fails."""
    dlne_logger.log_error("worker [/danger] dropped")
    _, message = captured[0]
    assert "dropped" in message
    # the payload's tag is defanged; the only live one is the wrapper's own closer
    assert r"\[/danger]" in message
    assert message.endswith("[/danger]")
    assert dlne_logger._markup_safe(message) == message


def test_a_real_handler_renders_the_hostile_message_without_raising():
    """
    Exercises the actual RichHandler rather than a stand-in, because the crash
    happened inside handler.emit, not in the helper.
    """
    logger = dlne_logger.setup_logger("markup-regression-test")
    record = logging.LogRecord(
        name="markup-regression-test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=dlne_logger._markup_safe("=" * 50 + "[/bold yellow]"),
        args=(),
        exc_info=None,
    )
    for handler in logger.handlers:
        handler.handle(record)
