from engine.config import get_base_dir
import logging
import os
import sys
from rich.console import Console
from rich.logging import RichHandler
from rich.markup import escape as _escape_markup, render as _render_markup
from rich.theme import Theme

# Setup a beautiful terminal theme for different roles
custom_theme = Theme({
    "info": "dim cyan",
    "warning": "magenta",
    "danger": "bold red",
    "success": "bold green",
    "worker": "bold blue",
    "coordinator": "bold yellow"
})


class _StdoutThatSurvives:
    """
    A worker's stdout is a pipe to the editor that started it, and the editor can go away (a window reload) while
    the node runs on, by design. A write to that broken pipe raised BrokenPipeError in the heartbeat loop, and Rich
    answers one with SystemExit, which ended the loop (2.5.3). From the first failed write on, output goes nowhere.
    """

    def __init__(self, stream):
        self._stream = stream

    def _gone(self):
        self._stream = open(os.devnull, "w")

    def write(self, text):
        try:
            return self._stream.write(text)
        except (OSError, ValueError):     # ValueError: the stream was closed
            self._gone()
            return len(text)

    def flush(self):
        try:
            self._stream.flush()
        except (OSError, ValueError):
            self._gone()

    def __getattr__(self, name):
        return getattr(self._stream, name)


if sys.stdout is not None and not isinstance(sys.stdout, _StdoutThatSurvives):
    sys.stdout = _StdoutThatSurvives(sys.stdout)

console = Console(theme=custom_theme)

def setup_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """
    Sets up a Rich-powered logger for beautiful console output,
    and a FileHandler for persistent lightweight logging.
    """
    logger = logging.getLogger(name)
    
    # Avoid adding multiple handlers if already setup
    if not logger.handlers:
        logger.setLevel(level)
        
        # We use RichHandler to format our output beautifully
        handler = RichHandler(
            console=console,
            show_time=True,
            show_path=False,
            rich_tracebacks=True,
            markup=True
        )
        
        formatter = logging.Formatter("%(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        
        # File Handler for persistent logging
        import os
        from logging.handlers import RotatingFileHandler
        
        log_dir = os.path.join(get_base_dir(), "logs")
        try:
            os.makedirs(log_dir, exist_ok=True)
            log_file = os.path.join(log_dir, "dlne_node.log")
            file_handler = RotatingFileHandler(log_file, mode="a", encoding="utf-8", maxBytes=5*1024*1024, backupCount=3)
        except Exception:
            # Fallback if logs directory is not writable
            log_file = "dlne_node.log"
            file_handler = RotatingFileHandler(log_file, mode="a", encoding="utf-8", maxBytes=5*1024*1024, backupCount=3)
            
        file_formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)
        
        # Prevent propagation to root logger to avoid double printing
        logger.propagate = False
        
    return logger

# Create standard loggers for the system
sys_logger = setup_logger("system")
worker_logger = setup_logger("worker")
coord_logger = setup_logger("coordinator")

def _markup_safe(msg) -> str:
    """
    Returns ``msg`` unchanged if Rich can parse its markup, and escaped if it cannot.

    Rich reads square brackets in a log message as style tags, and an unbalanced tag
    raises MarkupError *inside the logging call* — so a cosmetic mistake becomes a
    crash in whatever was doing the logging. It killed the update-check background
    task, and then killed the handler reporting that failure, because the report
    quoted the offending text back.

    Messages carrying deliberate markup still render in colour. Anything Rich rejects
    is shown literally, which beats losing the message and its caller. Nothing that
    happens in here is worth raising over: a logger that can throw is a logger that
    cannot be trusted in an error path.
    """
    text = str(msg)
    try:
        _render_markup(text)
    except Exception:
        return _escape_markup(text)
    return text

def log_system(msg: str):
    sys_logger.info(f"[info]{_markup_safe(msg)}[/info]")

def log_worker(msg: str):
    worker_logger.info(f"[worker]{_markup_safe(msg)}[/worker]")

def log_coordinator(msg: str):
    coord_logger.info(f"[coordinator]{_markup_safe(msg)}[/coordinator]")

def log_success(msg: str):
    sys_logger.info(f"[success]{_markup_safe(msg)}[/success]")

def log_error(msg: str):
    sys_logger.error(f"[danger]{_markup_safe(msg)}[/danger]")

def log_warning(msg: str):
    sys_logger.warning(f"[warning]{_markup_safe(msg)}[/warning]")
