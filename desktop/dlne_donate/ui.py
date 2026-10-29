"""
The Settings window: small, native-looking Tk (ttk), the "smart sliders" panel.

    GPU    on/off, the VRAM to lend, the RAM that goes with it
    CPU    on/off, the RAM (snapping to the swarm's model stops, each labelled), the threads
    a live line saying what the setting powers, or that it is below what the swarm can use
    one sentence per section, and a big Donate / Stop button

It reads and changes a :class:`model.Model` and nothing else, and it only ever runs on the thread that made it.
"""

import base64
import io
import sys
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox, ttk
from typing import Any, Callable, List, Optional

from . import WEBSITE_URL, bundled, markers, tiers
from . import worker as worker_mod
from .model import Model

STATE_COLORS = {worker_mod.IDLE: "#8a8f98", worker_mod.PREPARING: "#e0a100", worker_mod.STARTING: "#e0a100",
                worker_mod.DONATING: "#2e9e4f", worker_mod.STOPPING: "#e0a100", worker_mod.ERROR: "#d13438"}
WRAP = 372

GPU_SENTENCE = ("A GPU with 8 GB of VRAM or more, plus 16 to 24 GB of RAM beside it, runs a whole coding model for "
                "other people. A smaller GPU joins the shared GPU pool.")
CPU_SENTENCE = ("A CPU with 4 or more physical cores, AVX2 and 8 GB of RAM or more powers the DLNE phone app. "
                "The more RAM you lend, the smarter its model.")
THREAD_SENTENCE = "Threads decide how much of your computer stays free for you."


def icon_photo(size: int = 48) -> Optional[tk.PhotoImage]:
    """The DLNE icon as a Tk image (Tk reads PNG itself), scaled with Pillow."""
    try:
        from PIL import Image
        image = Image.open(bundled.asset_path("icon.png")).convert("RGBA").resize((size, size), Image.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return tk.PhotoImage(data=base64.b64encode(buffer.getvalue()))
    except Exception:  # noqa: BLE001 - a window without its icon is still a window
        return None


class SettingsWindow:
    def __init__(self, root: tk.Tk, model: Model, on_quit: Callable[[], None], window_only: bool = False):
        self.root = root
        self.model = model
        self.on_quit = on_quit
        self.window_only = window_only
        self._commit_job: Optional[str] = None
        self._building = True
        self.win = tk.Toplevel(root)
        self.win.title("DLNE Donate")
        self.win.resizable(False, True)        # the height follows the screen (the content scrolls); the width is fixed
        self._pointer: Optional[tuple] = None
        self._fitted_content_h = 0
        self._icon_small = icon_photo(48)
        self._icon_window = icon_photo(64)
        if self._icon_window is not None:
            try:
                self.win.iconphoto(True, self._icon_window)
            except tk.TclError:
                pass
        self.win.protocol("WM_DELETE_WINDOW", self.hide)
        self._style()
        self._build()
        self._building = False
        self.refresh()
        self.win.withdraw()

    # ---- look --------------------------------------------------------------------------------------

    def _style(self) -> None:
        style = ttk.Style(self.win)
        for name in (("vista", "winnative") if sys.platform == "win32" else ("clam",)):
            if name in style.theme_names():
                style.theme_use(name)
                break
        # Fonts are derived from the platform's own default font (a family name made up here would fall back to a bitmap font).
        base_font = tkfont.nametofont("TkDefaultFont")
        size = int(base_font.cget("size"))
        self._fonts = []

        def derived(delta: int, bold: bool) -> tkfont.Font:
            font = base_font.copy()
            font.configure(size=size + delta if size > 0 else size - delta, weight="bold" if bold else "normal")
            self._fonts.append(font)      # Tk drops a font whose Python object is collected
            return font

        style.configure("Title.TLabel", font=derived(4, True))
        style.configure("Section.TLabel", font=derived(1, True))
        style.configure("Section.TCheckbutton", font=derived(1, True))
        style.configure("Hint.TLabel", foreground="#5b6068")
        style.configure("Live.TLabel", foreground="#1a5fb4")
        style.configure("Warn.TLabel", foreground="#9a6700")
        style.configure("Bad.TLabel", foreground="#d13438")
        style.configure("Big.TButton", font=derived(3, True), padding=(10, 9))

    def _build(self) -> None:
        win = self.win
        # The settings scroll (a small screen cannot show them all); the Donate button and the two start-up options
        # stay below, in `bottom`, so they are always reachable.
        background = ttk.Style(win).lookup("TFrame", "background") or win.cget("background")
        win.configure(background=background)
        win.columnconfigure(0, weight=1)
        win.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(win, highlightthickness=0, bd=0, background=background, yscrollincrement=20)
        self.scrollbar = ttk.Scrollbar(win, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.scrollbar.grid_remove()
        outer = ttk.Frame(self.canvas, padding=(14, 12, 14, 4))
        self.outer = outer
        self._outer_id = self.canvas.create_window(0, 0, window=outer, anchor="nw")
        self.bottom = ttk.Frame(win, padding=(14, 0, 14, 12))
        self.bottom.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        outer.bind("<Configure>", lambda _e: self._update_scroll())
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            win.bind(sequence, self._on_wheel)
        hw = self.model.hardware

        head = ttk.Frame(outer)
        head.grid(row=0, column=0, sticky="ew")
        if self._icon_small is not None:
            ttk.Label(head, image=self._icon_small).grid(row=0, column=0, rowspan=2, padx=(0, 10))
        ttk.Label(head, text="DLNE Donate", style="Title.TLabel").grid(row=0, column=1, sticky="w")
        ttk.Label(head, text="Lend this computer to the swarm", style="Hint.TLabel").grid(row=1, column=1, sticky="w")

        status_row = ttk.Frame(outer)
        status_row.grid(row=1, column=0, sticky="ew", pady=(10, 4))
        self.dot = tk.Canvas(status_row, width=14, height=14, highlightthickness=0, bd=0)
        self.dot.grid(row=0, column=0, padx=(0, 6))
        self._dot_item = self.dot.create_oval(2, 2, 12, 12, fill=STATE_COLORS[worker_mod.IDLE], outline="")
        self.status_text = ttk.Label(status_row, text="Not donating", wraplength=WRAP - 24, justify="left")
        self.status_text.grid(row=0, column=1, sticky="w")

        ttk.Separator(outer).grid(row=2, column=0, sticky="ew", pady=6)

        # ---- GPU ----
        gpu = ttk.Frame(outer)
        gpu.grid(row=3, column=0, sticky="ew")
        self.gpu_var = tk.BooleanVar()
        self.gpu_check = ttk.Checkbutton(gpu, text="Lend my GPU", style="Section.TCheckbutton", variable=self.gpu_var,
                                         command=self._on_gpu_toggle)
        self.gpu_check.grid(row=0, column=0, sticky="w")
        name = (f"{hw.gpu_name} ({hw.vram_gb:g} GB)" if hw.has_gpu else "no GPU the swarm can use")
        ttk.Label(gpu, text=name, style="Hint.TLabel", wraplength=WRAP).grid(row=1, column=0, sticky="w", padx=(22, 0))
        ttk.Label(gpu, text=GPU_SENTENCE, style="Hint.TLabel", wraplength=WRAP, justify="left").grid(row=2, column=0, sticky="w", pady=(2, 4))
        self.vram_row = self._slider_row(gpu, 3, "VRAM to lend")
        self.gpu_ram_row = self._slider_row(gpu, 4, "RAM that goes with it")
        gpu.columnconfigure(0, weight=1)

        ttk.Separator(outer).grid(row=4, column=0, sticky="ew", pady=8)

        # ---- CPU ----
        cpu = ttk.Frame(outer)
        cpu.grid(row=5, column=0, sticky="ew")
        self.cpu_var = tk.BooleanVar()
        self.cpu_check = ttk.Checkbutton(cpu, text="Lend my CPU", style="Section.TCheckbutton", variable=self.cpu_var,
                                         command=self._on_cpu_toggle)
        self.cpu_check.grid(row=0, column=0, sticky="w")
        ttk.Label(cpu, text=f"{hw.physical_cores} cores, {hw.logical_threads} threads, {hw.total_ram_gb:g} GB RAM",
                  style="Hint.TLabel").grid(row=1, column=0, sticky="w", padx=(22, 0))
        ttk.Label(cpu, text=CPU_SENTENCE, style="Hint.TLabel", wraplength=WRAP, justify="left").grid(row=2, column=0, sticky="w", pady=(2, 4))
        self.cpu_scale_frame = ttk.Frame(cpu)
        self.cpu_scale_frame.grid(row=3, column=0, sticky="ew")
        self.cpu_stops: List[tiers.RamStop] = []
        self._cpu_widgets: List[Any] = []
        cpu.columnconfigure(0, weight=1)

        ttk.Separator(outer).grid(row=6, column=0, sticky="ew", pady=8)

        # ---- threads ----
        thr = ttk.Frame(outer)
        thr.grid(row=7, column=0, sticky="ew")
        ttk.Label(thr, text="Threads", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        self.threads_row = self._slider_row(thr, 1, "")
        self.threads_line = ttk.Label(thr, text="", style="Hint.TLabel", wraplength=WRAP, justify="left")
        self.threads_line.grid(row=2, column=0, sticky="w")
        thr.columnconfigure(0, weight=1)

        ttk.Separator(outer).grid(row=8, column=0, sticky="ew", pady=8)

        # ---- the live line ----
        self.live = ttk.Label(outer, text="", style="Live.TLabel", wraplength=WRAP, justify="left")
        self.live.grid(row=9, column=0, sticky="w")
        self.warn = ttk.Label(outer, text="", style="Warn.TLabel", wraplength=WRAP, justify="left")
        self.warn.grid(row=10, column=0, sticky="w", pady=(4, 0))
        self.detail = ttk.Label(outer, text="", style="Hint.TLabel", wraplength=WRAP, justify="left")
        self.detail.grid(row=11, column=0, sticky="w", pady=(4, 0))

        bottom = self.bottom
        self.button = ttk.Button(bottom, text="Donate", style="Big.TButton", command=self._on_button)
        self.button.grid(row=0, column=0, sticky="ew", pady=(10, 6))
        self.progress = ttk.Progressbar(bottom, mode="determinate", maximum=100)
        self.progress.grid(row=1, column=0, sticky="ew")
        self.progress.grid_remove()

        foot = ttk.Frame(bottom)
        foot.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.autostart_var = tk.BooleanVar()
        ttk.Checkbutton(foot, text="Start with my computer", variable=self.autostart_var, command=self._on_autostart).grid(row=0, column=0, sticky="w")
        self.resume_var = tk.BooleanVar()
        ttk.Checkbutton(foot, text="Start donating when the app starts", variable=self.resume_var, command=self._on_resume).grid(row=1, column=0, sticky="w")
        self.quit_button = ttk.Button(foot, text="Quit", command=self.on_quit)
        if self.window_only:
            self.quit_button.grid(row=0, column=1, rowspan=2, padx=(30, 0))
        foot.columnconfigure(0, weight=1)
        bottom.columnconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)

    def _slider_row(self, parent: Any, row: int, label: str) -> dict:
        frame = ttk.Frame(parent)
        frame.grid(row=row, column=0, sticky="ew", pady=1)
        title = ttk.Label(frame, text=label, width=20)
        title.grid(row=0, column=0, sticky="w")
        scale = ttk.Scale(frame, from_=0, to=1, orient="horizontal", length=170)
        scale.grid(row=0, column=1, sticky="ew")
        value = ttk.Label(frame, text="", width=9, anchor="e")
        value.grid(row=0, column=2, sticky="e")
        frame.columnconfigure(1, weight=1)
        row_info = {"frame": frame, "title": title, "scale": scale, "value": value}
        scale.configure(command=lambda v, info=row_info: self._dragging(info, float(v)))
        scale.bind("<ButtonRelease-1>", lambda _e: self._schedule_commit())
        scale.bind("<KeyRelease>", lambda _e: self._schedule_commit())
        return row_info

    # ---- reading the model into the widgets --------------------------------------------------------

    def refresh(self) -> None:
        """Redraw everything from the model (cheap; called on every change and twice a second)."""
        if not self.win.winfo_exists():
            return
        self._building = True
        model = self.model
        hw, s = model.hardware, model.settings
        desc = model.describe()
        status = model.status()
        running = status.running

        self.gpu_var.set(bool(s.gpu_enabled and hw.has_gpu))
        self.gpu_check.state(["!disabled"] if hw.has_gpu else ["disabled"])
        vram_low, vram_high = tiers.gpu_vram_bounds(hw)
        self._set_scale(self.vram_row, vram_low, max(vram_high, vram_low + 0.1), s.gpu_vram_gb, f"{s.gpu_vram_gb:g} GB",
                        enabled=bool(s.gpu_enabled and hw.has_gpu))
        low, soft, hard = tiers.gpu_ram_bounds(hw)
        self._set_scale(self.gpu_ram_row, low, max(hard, low + 1), s.gpu_ram_gb, f"{s.gpu_ram_gb:g} GB",
                        enabled=bool(s.gpu_enabled and hw.has_gpu))

        self.cpu_var.set(bool(s.cpu_enabled))
        stops = tiers.cpu_stops(hw, model.swarm.app_tiers)
        self._rebuild_cpu_stops(stops, s.cpu_ram_gb, enabled=bool(s.cpu_enabled))
        self.cpu_check.state(["!disabled"] if hw.cpu_meets_minimum and stops else ["disabled"])

        lo_t, hi_t = tiers.thread_bounds(hw)
        self._set_scale(self.threads_row, lo_t, max(hi_t, lo_t + 1), s.threads, f"{s.threads} of {hi_t}", enabled=True)
        self.threads_line.configure(text=f"{THREAD_SENTENCE} {desc.threads_line}")

        lines = [text for text in (desc.gpu_line if s.gpu_enabled or not hw.has_gpu else "", desc.cpu_line if s.cpu_enabled or not desc.cpu_usable else "") if text]
        if not lines:
            lines = [desc.blocker or "Turn on the GPU or the CPU to donate."]
        self.live.configure(text="\n\n".join(lines))
        self._show_text(self.warn, "\n".join(desc.warnings))
        extra = []
        if desc.disk_gb and not running:
            extra.append(f"The first time, DLNE downloads the model ({desc.disk_gb:g} GB) and keeps it on your disk.")
        if running and model.controller.devices_changed():
            extra.append("Switching the GPU or the CPU on or off applies the next time you press Donate. "
                         "The amounts are saved now, and the worker picks them up as it runs.")
        if status.clock_notice:
            extra.append(status.clock_notice)
        self._show_text(self.detail, "\n".join(extra))

        self._show_status(status, desc.machine_blocker)
        if status.running:
            self.button.configure(text="Stop donating", state="disabled" if status.state == worker_mod.STOPPING else "normal")
        else:
            self.button.configure(text="Donate", state="normal" if desc.can_donate else "disabled")
        self.autostart_var.set(model.autostart_enabled())
        self.resume_var.set(s.resume_donating)
        self._building = False
        self._refit_if_content_changed()

    def _show_status(self, status: worker_mod.Status, machine_blocker: str = "") -> None:
        self.dot.itemconfigure(self._dot_item, fill=STATE_COLORS.get(status.state, STATE_COLORS[worker_mod.IDLE]))
        if status.state == worker_mod.DONATING:
            text = "Donating: " + (markers.role_title(status.role) if status.role else "joined the swarm")
            if status.also_cpu:
                text += " + phone app"
            if status.role:
                text += "\n" + markers.role_meaning(status.role)
            for line in (markers.dock_line(status.dock), markers.yield_line(status.yielding)):
                if line:
                    text += "\n" + line
            if status.activity:
                text += "\n" + status.activity
        elif status.state == worker_mod.ERROR:
            text = status.error or status.message
        elif status.state == worker_mod.IDLE:
            text = machine_blocker or "Not donating"          # a computer that can lend nothing says why, up here
        else:
            text = status.message
        bad = status.state == worker_mod.ERROR or (status.state == worker_mod.IDLE and bool(machine_blocker))
        self.status_text.configure(text=text, style="Bad.TLabel" if bad else "TLabel")
        if status.progress is not None and status.state in (worker_mod.PREPARING, worker_mod.DONATING):
            self.progress.grid()
            self.progress.configure(value=status.progress * 100)
        else:
            self.progress.grid_remove()

    @staticmethod
    def _show_text(label: Any, text: str) -> None:
        """A line that takes no room while it has nothing to say."""
        label.configure(text=text)
        if text:
            label.grid()
        else:
            label.grid_remove()

    @staticmethod
    def _set_scale(row: dict, low: float, high: float, value: float, text: str, enabled: bool) -> None:
        scale = row["scale"]
        scale.configure(from_=low, to=high)
        scale.set(min(max(value, low), high))
        row["value"].configure(text=text)
        scale.state(["!disabled"] if enabled else ["disabled"])

    def _rebuild_cpu_stops(self, stops: List[tiers.RamStop], value: float, enabled: bool) -> None:
        key = [(s.gb, s.runs, s.beyond_half) for s in stops]
        if getattr(self, "_cpu_key", None) != key:
            for widget in self._cpu_widgets:
                widget.destroy()
            self._cpu_widgets = []
            self.cpu_stops = stops
            self._cpu_key = key
            frame = self.cpu_scale_frame
            if len(stops) >= 2:
                self.cpu_scale = ttk.Scale(frame, from_=0, to=len(stops) - 1, orient="horizontal")
                self.cpu_scale.grid(row=0, column=0, columnspan=len(stops), sticky="ew", padx=14)
                self.cpu_scale.bind("<ButtonRelease-1>", lambda _e: self._cpu_release())
                self.cpu_scale.bind("<KeyRelease>", lambda _e: self._cpu_release())
                self._cpu_widgets.append(self.cpu_scale)
            else:
                self.cpu_scale = None
            for i, stop in enumerate(stops):
                label = ttk.Label(frame, text=f"{stop.title}\n{stop.runs}" + ("\n(over half)" if stop.beyond_half else ""),
                                  justify="center", style="Hint.TLabel", anchor="center")
                label.grid(row=1, column=i, sticky="n")
                frame.columnconfigure(i, weight=1, uniform="stops")
                self._cpu_widgets.append(label)
            if not stops:
                note = ttk.Label(frame, text="This computer has too little RAM to lend 8 GB and keep 4 GB for itself.",
                                 style="Hint.TLabel", wraplength=WRAP)
                note.grid(row=0, column=0, sticky="w")
                self._cpu_widgets.append(note)
        index = min(range(len(stops)), key=lambda i: abs(stops[i].gb - value)) if stops else 0
        if self.cpu_scale is not None:
            self.cpu_scale.set(index)
            self.cpu_scale.state(["!disabled"] if enabled else ["disabled"])

    # ---- changing the model ------------------------------------------------------------------------

    def _dragging(self, row: dict, raw: float) -> None:
        """While a slider is dragged: show the value it would take, and snap to whole units (nothing is saved until release)."""
        if self._building:
            return
        text = None
        if row is self.vram_row:
            value = self._snap_vram(raw)
            text = f"{value:g} GB"
        elif row is self.gpu_ram_row:
            text = f"{int(round(raw))} GB"
        elif row is self.threads_row:
            text = f"{int(round(raw))} of {self.model.hardware.logical_threads}"
        if text:
            row["value"].configure(text=text)
        self._schedule_commit()

    def _snap_vram(self, raw: float) -> float:
        high = self.model.hardware.vram_gb
        value = float(round(raw))
        return high if abs(high - raw) < 0.5 or value > high else max(value, 1.0)

    def _cpu_release(self) -> None:
        if self._building or not self.cpu_stops or self.cpu_scale is None:
            return
        index = min(max(int(round(float(self.cpu_scale.get()))), 0), len(self.cpu_stops) - 1)
        self.cpu_scale.set(index)                       # snap to the stop
        self.model.update(cpu_ram_gb=float(self.cpu_stops[index].gb))

    def _schedule_commit(self) -> None:
        if self._building:
            return
        if self._commit_job is not None:
            self.win.after_cancel(self._commit_job)
        self._commit_job = self.win.after(250, self._commit)

    def _commit(self) -> None:
        self._commit_job = None
        self.model.update(gpu_vram_gb=self._snap_vram(float(self.vram_row["scale"].get())),
                          gpu_ram_gb=float(int(round(float(self.gpu_ram_row["scale"].get())))),
                          threads=int(round(float(self.threads_row["scale"].get()))))

    def _on_gpu_toggle(self) -> None:
        if not self._building:
            self.model.update(gpu_enabled=bool(self.gpu_var.get()))

    def _on_cpu_toggle(self) -> None:
        if not self._building:
            self.model.update(cpu_enabled=bool(self.cpu_var.get()))

    def _on_resume(self) -> None:
        if not self._building:
            self.model.update(resume_donating=bool(self.resume_var.get()))

    def _on_autostart(self) -> None:
        if self._building:
            return
        result = self.model.set_autostart(bool(self.autostart_var.get()))
        if not result.get("ok"):
            messagebox.showwarning("DLNE Donate", str(result.get("detail")), parent=self.win)
        self.autostart_var.set(self.model.autostart_enabled())

    def _on_button(self) -> None:
        model = self.model
        if model.status().running:
            model.stop()
            return
        if not self._confirm_lending():
            return
        model.donate()

    def _confirm_lending(self) -> bool:
        """The first time a GPU is lent: what the swarm may do with it, and what protects the owner (as the extension says it)."""
        model = self.model
        s = model.settings
        if not (s.gpu_enabled and model.hardware.has_gpu) or s.lend_notice_seen:
            return True
        desc = model.describe()
        facts = "\n".join("- " + fact for fact in MEMBER_PRIVACY_FACTS)
        disk = f"\n\nIt downloads a model of {desc.disk_gb:g} GB the first time and keeps it on your disk." if desc.disk_gb else ""
        ok = messagebox.askokcancel(
            "Lend your GPU to the swarm?",
            "The swarm decides what your GPU does, and this window shows you which: a share of one big model run together "
            "with other volunteers' GPUs, or (with enough VRAM and RAM) a whole coding model that answers other people's "
            "coding requests.\n\nIf it becomes a coding member, you should know:\n" + facts + disk +
            "\n\nYou can stop at any time. Lend your GPU?", parent=self.win)
        if ok:
            model.update(lend_notice_seen=True)
        return bool(ok)

    # ---- showing and hiding ------------------------------------------------------------------------

    def show(self, near_pointer: bool = False) -> None:
        self.model.refresh_swarm()
        self.refresh()
        self._pointer = self.win.winfo_pointerxy() if near_pointer else None
        self.win.deiconify()
        self.win.update_idletasks()
        self._place()
        # Windows gives a window that has just been mapped its own default position, and its frame (title bar, borders)
        # only has a measurable size once it is on screen: place it again when both have settled.
        for delay in (60, 300, 900):
            self.win.after(delay, self._settle)
        self.win.lift()
        try:
            self.win.attributes("-topmost", True)
            self.win.after(400, lambda: self.win.attributes("-topmost", False))
        except tk.TclError:
            pass
        self.win.focus_force()

    def set_window_only(self, value: bool) -> None:
        """No tray to return to (none docked): closing the window only minimises it, and the window carries a Quit button."""
        self.window_only = value
        if value:
            self.quit_button.grid(row=0, column=1, rowspan=2, padx=(30, 0))
        else:
            self.quit_button.grid_remove()

    def hide(self) -> None:
        if self.window_only:
            self.win.iconify()
        else:
            self.win.withdraw()

    def visible(self) -> bool:
        return bool(self.win.winfo_exists() and self.win.state() == "normal")

    def _work_area(self) -> tuple:
        """(left, top, right, bottom) of the screen minus the taskbar."""
        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes
                rect = wintypes.RECT()
                if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):    # SPI_GETWORKAREA
                    return rect.left, rect.top, rect.right, rect.bottom
            except Exception:  # noqa: BLE001
                pass
        return 0, 0, self.win.winfo_screenwidth(), self.win.winfo_screenheight()

    MARGIN = 10

    def _fit(self) -> tuple:
        """
        Size the window to its content, or to the work area when that is smaller (the content then scrolls).
        Returns ``(client_width, client_height, frame_width, frame_height)``: the frame is the client plus the title bar
        and borders, which can only be measured once the window is on screen.
        """
        win = self.win
        win.update_idletasks()
        _left, top, _right, bottom = self._work_area()
        title = max(win.winfo_rooty() - win.winfo_y(), 0)
        border = max(win.winfo_rootx() - win.winfo_x(), 0)
        extra_h, extra_w = title + border, 2 * border
        content_h, content_w = self.outer.winfo_reqheight(), self.outer.winfo_reqwidth()
        bottom_h = self.bottom.winfo_reqheight()
        available = (bottom - top) - 2 * self.MARGIN - extra_h - bottom_h
        canvas_h = max(min(content_h, available), 120)
        scrolls = content_h > canvas_h
        if scrolls:
            self.scrollbar.grid()
        else:
            self.scrollbar.grid_remove()
        width = max(content_w, self.bottom.winfo_reqwidth()) + (self.scrollbar.winfo_reqwidth() if scrolls else 0)
        self.canvas.configure(width=content_w, height=canvas_h)
        self._fitted_content_h = content_h
        self._update_scroll()
        client_h = canvas_h + bottom_h
        return width, client_h, width + extra_w, client_h + extra_h

    def _target(self) -> tuple:
        """Where the window goes: the corner of the work area that holds the tray, or next to the pointer; always fully inside it."""
        client_w, client_h, width, height = self._fit()
        left, top, right, bottom = self._work_area()
        margin = self.MARGIN
        if self._pointer is not None:
            px, py = self._pointer
            x = px - width if px > (left + right) / 2 else px
            y = py - height if py > (top + bottom) / 2 else py
        else:
            x, y = right - width - margin, bottom - height - margin
        x = min(max(x, left + margin), max(left + margin, right - width - margin))
        y = min(max(y, top + margin), max(top + margin, bottom - height - margin))
        return client_w, client_h, int(x), int(y)

    def _place(self) -> None:
        client_w, client_h, x, y = self._target()
        self.win.geometry(f"{client_w}x{client_h}+{x}+{y}")

    def _settle(self) -> None:
        """Run shortly after the window was shown: put it back where it belongs if the system moved it or its frame turned out larger."""
        try:
            if self.win.winfo_exists() and self.win.winfo_viewable():
                self._place()
        except tk.TclError:
            pass

    def _refit_if_content_changed(self) -> None:
        """A line appeared or went away while the window is open: fit the height again, keeping the window where the owner put it."""
        try:
            if not (self.win.winfo_exists() and self.win.winfo_viewable()):
                return
            if self.outer.winfo_reqheight() == self._fitted_content_h:
                return
            x, y = self.win.winfo_x(), self.win.winfo_y()
            client_w, client_h, _w, height = self._fit()
            _l, top, _r, bottom = self._work_area()
            y = max(top + self.MARGIN, min(y, bottom - self.MARGIN - height))
            self.win.geometry(f"{client_w}x{client_h}+{x}+{y}")
        except tk.TclError:
            pass

    # ---- scrolling ---------------------------------------------------------------------------------

    def _on_canvas_configure(self, event: Any) -> None:
        self.canvas.itemconfigure(self._outer_id, width=event.width)
        self._update_scroll()

    def _update_scroll(self) -> None:
        content_h = self.outer.winfo_reqheight()
        self.canvas.configure(scrollregion=(0, 0, max(self.outer.winfo_reqwidth(), 1), max(content_h, 1)))
        if content_h <= self.canvas.winfo_height():
            self.canvas.yview_moveto(0)

    def _on_wheel(self, event: Any) -> None:
        if not self.scrollbar.winfo_ismapped():
            return
        if getattr(event, "num", 0) in (4, 5):
            step = -1 if event.num == 4 else 1
        else:
            step = -1 if event.delta > 0 else 1
        self.canvas.yview_scroll(step * 3, "units")


# The facts a coding member is shown, plainly (vscode-extension/src/worker/contribution.ts MEMBER_PRIVACY_FACTS).
MEMBER_PRIVACY_FACTS = (
    "You run other people's coding requests. Your GPU has to read their prompts and code to answer, so the machine itself sees them while it works.",
    "The requests are anonymous. No name, account or IP address of the person asking reaches your machine.",
    "They are end-to-end encrypted in transit: each request is sealed for your machine and opened only by the DLNE worker on it, so the network and DLNE's server cannot read it.",
    "Your machine keeps no trace: the worker writes no prompts or answers to disk or to its logs.",
    "Code from a private repository goes only to machines the operator trusts, unless its owner allows any member. Public repositories can reach any member.",
    "The swarm sometimes sends test requests holding decoy credentials; a member that ever uses one is removed.",
)
