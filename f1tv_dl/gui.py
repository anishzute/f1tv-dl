"""Local desktop UI. All network and media work runs outside Tk's event thread."""

import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import __version__
from .config import Settings
from .desktop import DesktopService, DownloadOptions, install_browser_support
from .errors import F1TVError, OperationCancelled
from .http import network_summary
from .text import display_text

try:
    import tkinter as tk
    from tkinter import filedialog, font, messagebox, ttk
except ImportError:
    tk = None

METHODS = {"Browser sign-in": "browser", "Email and password": "password", "Session token": "token"}
LANGUAGES = {
    "English": "eng",
    "Dutch": "nld",
    "German": "deu",
    "French": "fra",
    "Portuguese": "por",
    "Spanish": "spa",
    "Japanese": "jpn",
    "Track effects": "fx",
    "Team radio": "tea",
}


def clock_time(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def friendly_error(error):
    text = str(error)
    if "rejected password login" in text:
        return (
            "F1 TV rejected this sign-in. Check your email and password, or choose "
            "Browser sign-in on the Account tab to complete verification."
        )
    if text.startswith("Browser login requires:"):
        return "Click Install browser support on the Account tab, then try signing in again."
    if text.startswith("Browser login failed."):
        return (
            "Browser sign-in failed. Check browser setup and network access, "
            "or choose another sign-in method on the Account tab."
        )
    return text.replace("--login-method browser", "Browser sign-in on the Account tab").replace(
        "F1TV_TOKEN", "the session token"
    )


class EventLogHandler(logging.Handler):
    def __init__(self, events):
        super().__init__(logging.WARNING)
        self.events = events

    def emit(self, record):
        self.events.put(("log", record.getMessage()))


class DownloaderApp:
    def __init__(self, root, service=None):
        self.root = root
        self.service = service or DesktopService()
        self.content = None
        self.last_output = None
        self.busy = False
        self.closing = False
        self.expected_seconds = 0
        self.queue_index = 0
        self.queue_total = 1
        self.batch_completed = []
        self.queue_rows = {}
        self.events = queue.Queue()
        self.cancel_event = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="f1tv-desktop")
        self.saved_states = []
        self.page_canvases = {}
        self.log_handler = EventLogHandler(self.events)
        logging.getLogger("f1tv_dl").addHandler(self.log_handler)
        self._variables()
        self._style()
        self._layout()
        self._refresh_actions()
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.poll_id = root.after(80, self._drain_events)
        if not self.service.signed_in:
            self.tabs.select(self.account_tab)

    def _variables(self):
        self.url = tk.StringVar()
        self.method = tk.StringVar(value="Browser sign-in")
        self.email = tk.StringVar(value=os.environ.get("F1TV_USER", ""))
        self.password = tk.StringVar()
        self.token = tk.StringVar()
        self.remember = tk.BooleanVar(value=True)
        self.resolution = tk.StringVar(value="Best available")
        self.audio = tk.StringVar(value="English")
        self.international = tk.StringVar(value="None")
        self.offset = tk.StringVar(value="00:00:00.000")
        self.format = tk.StringVar(value="mp4")
        self.directory = tk.StringVar(
            value=os.environ.get("F1TV_OUTDIR", str(Path.cwd() / "download"))
        )
        self.overwrite = tk.BooleanVar(value=False)
        self.sample = tk.StringVar()
        self.status = tk.StringVar(value="Ready. Sign in or paste a video link to begin.")
        self.account_status = tk.StringVar()
        self.video_title = tk.StringVar(value="Load a video to see its available feeds.")
        self.progress_text = tk.StringVar(value="No download running")
        self.selection_text = tk.StringVar(value="No feeds selected")
        self.country = tk.StringVar(value=self.service.settings.country)
        self.entitlement = tk.StringVar(value=self.service.settings.entitlement)
        self.timeout = tk.StringVar(value=str(self.service.settings.timeout))
        self.cache_path = tk.StringVar(value=str(self.service.store.path or ""))
        self.browser_path = tk.StringVar()
        self.tool_status = tk.StringVar()

    def _style(self):
        self.root.title("F1 TV Downloader")
        self.root.geometry("960x850")
        self.root.minsize(820, 640)
        self.root.configure(background="#f5f5f7")
        default_font = font.nametofont("TkDefaultFont")
        families = set(font.families(self.root))
        family = next(
            (
                name
                for name in ("Segoe UI", "DejaVu Sans", "Noto Sans", "Liberation Sans")
                if name in families
            ),
            default_font.actual("family"),
        )
        default_font.configure(family=family, size=10)
        for name in ("TkTextFont", "TkHeadingFont", "TkSmallCaptionFont"):
            font.nametofont(name).configure(family=family)
        self.style = ttk.Style(self.root)
        self.style.theme_use("clam")
        self.style.configure(".", font=default_font)
        self.style.configure("TFrame", background="#f5f5f7")
        self.style.configure("TLabel", background="#f5f5f7", foreground="#20232b")
        self.style.configure("Muted.TLabel", foreground="#646975")
        self.style.configure("Title.TLabel", font=(default_font.actual("family"), 23, "bold"))
        self.style.configure("Section.TLabel", font=(default_font.actual("family"), 12, "bold"))
        self.style.configure(
            "TButton",
            padding=(13, 8),
            background="white",
            bordercolor="#cfd3dc",
            lightcolor="white",
            darkcolor="white",
        )
        self.style.map("TButton", background=[("active", "#e9ecf2")])
        for widget in ("TCheckbutton", "TRadiobutton"):
            self.style.configure(widget, background="#f5f5f7")
        self.style.configure("TCombobox", fieldbackground="white", background="#e9ecf2")
        self.style.configure("Primary.TButton", foreground="white", background="#d92727")
        self.style.map(
            "Primary.TButton", background=[("disabled", "#bfc1c7"), ("active", "#b91f1f")]
        )
        self.style.configure("TNotebook", background="#f5f5f7", borderwidth=0)
        self.style.configure("TNotebook.Tab", padding=(22, 10))
        self.style.map(
            "TNotebook.Tab", background=[("selected", "#f5f5f7"), ("!selected", "#e5e8ee")]
        )
        self.style.configure("TLabelframe", background="#f5f5f7", padding=14)
        self.style.configure("TLabelframe.Label", background="#f5f5f7", foreground="#20232b")
        self.style.configure("Treeview", rowheight=27, background="white", fieldbackground="white")
        self.style.configure("Treeview.Heading", background="#e9ecf2", foreground="#404653")
        self.style.map(
            "Treeview", background=[("selected", "#fbe3e3")], foreground=[("selected", "#20232b")]
        )
        self.style.configure("Horizontal.TProgressbar", background="#d92727", troughcolor="#e5e6ea")

    def _layout(self):
        outer = ttk.Frame(self.root, padding=22)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)
        ttk.Label(outer, text="F1 TV Downloader", style="Title.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            outer, text="Choose an F1 TV feed and save it to your computer.", style="Muted.TLabel"
        ).grid(row=1, column=0, sticky="w", pady=(4, 16))
        self.tabs = ttk.Notebook(outer)
        self.tabs.grid(row=2, column=0, sticky="nsew")
        self.download_tab = ttk.Frame(self.tabs)
        self.account_tab = ttk.Frame(self.tabs)
        self.settings_tab = ttk.Frame(self.tabs)
        self.tabs.add(self.download_tab, text="Download")
        self.tabs.add(self.account_tab, text="Account")
        self.tabs.add(self.settings_tab, text="Settings")
        self._download_panel()
        self._account_panel()
        self._settings_panel()
        footer = ttk.Frame(outer)
        footer.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        footer.columnconfigure(0, weight=1)
        status_label = ttk.Label(footer, textvariable=self.status, wraplength=640)
        status_label.grid(row=0, column=0, sticky="ew")
        self.cancel_button = ttk.Button(
            footer, text="Cancel", command=self.cancel, state="disabled"
        )
        self.cancel_button.grid(row=0, column=1, sticky="e", padx=(12, 0))
        footer.bind(
            "<Configure>",
            lambda event: status_label.configure(
                wraplength=max(200, event.width - self.cancel_button.winfo_reqwidth() - 16)
            ),
        )
        for event in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.root.bind_all(event, self._scroll_page, add="+")

    def _scrollable_body(self, page, padding):
        canvas = tk.Canvas(page, highlightthickness=0, background="#f5f5f7", yscrollincrement=22)
        scrollbar = ttk.Scrollbar(page, orient="vertical", command=canvas.yview)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        canvas.configure(yscrollcommand=scrollbar.set)
        body = ttk.Frame(canvas, padding=padding)
        window = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        self.page_canvases[str(page)] = canvas
        return body

    def _scroll_page(self, event):
        if event.widget.winfo_class() in {"Treeview", "TCombobox", "Text", "Listbox"}:
            return
        canvas = self.page_canvases.get(self.tabs.select())
        if canvas is not None:
            if event.num in (4, 5):
                direction = -1 if event.num == 4 else 1
            else:
                direction = int(-event.delta / 120) or (-1 if event.delta > 0 else 1)
            canvas.yview_scroll(direction, "units")

    def _download_panel(self):
        panel = self._scrollable_body(self.download_tab, 16)
        ttk.Label(panel, text="1. Choose a video", style="Section.TLabel").pack(anchor="w")
        row = ttk.Frame(panel)
        row.pack(fill="x", pady=(8, 6))
        self.url_entry = ttk.Entry(row, textvariable=self.url)
        self.url_entry.pack(side="left", fill="x", expand=True)
        self.url_entry.bind("<Return>", lambda event: self.load_video())
        ttk.Button(row, text="Paste", command=self.paste_url).pack(side="left", padx=6)
        self.load_button = ttk.Button(row, text="Load video", command=self.load_video)
        self.load_button.pack(side="left")
        ttk.Label(panel, textvariable=self.video_title, wraplength=800).pack(
            anchor="w", pady=(4, 8)
        )
        tree_row = ttk.Frame(panel)
        tree_row.pack(fill="x")
        self.feeds = ttk.Treeview(
            tree_row,
            columns=("feed", "driver", "number", "status"),
            show="headings",
            height=4,
            selectmode="extended",
        )
        for column, title, width in (
            ("feed", "Feed", 230),
            ("driver", "Driver", 370),
            ("number", "Number", 90),
            ("status", "Status", 120),
        ):
            self.feeds.heading(column, text=title)
            self.feeds.column(column, width=width, minwidth=70)
        self.feeds.pack(side="left", fill="x", expand=True)
        scroll = ttk.Scrollbar(tree_row, orient="vertical", command=self.feeds.yview)
        scroll.pack(side="right", fill="y")
        self.feeds.configure(yscrollcommand=scroll.set)
        self.feeds.bind("<<TreeviewSelect>>", self._feed_changed)
        selection_row = ttk.Frame(panel)
        selection_row.pack(fill="x", pady=(8, 0))
        ttk.Button(selection_row, text="Select all", command=self.select_all_feeds).pack(
            side="left"
        )
        ttk.Button(selection_row, text="Clear", command=self.clear_feeds).pack(side="left", padx=8)
        ttk.Label(selection_row, textvariable=self.selection_text).pack(side="left", padx=8)
        ttk.Label(
            panel,
            text="Use Ctrl-click to select individual feeds or Shift-click for a range. "
            "Each feed is saved as a separate file.",
            style="Muted.TLabel",
            wraplength=750,
        ).pack(anchor="w", pady=(4, 0))
        actions = ttk.Frame(panel)
        actions.pack(fill="x", pady=(16, 8))
        ttk.Label(actions, text="2. Quality and audio", style="Section.TLabel").pack(side="left")
        self.link_button = ttk.Button(actions, text="Copy playback link", command=self.copy_link)
        self.link_button.pack(side="right")
        self.inspect_button = ttk.Button(actions, text="Check streams", command=self.inspect)
        self.inspect_button.pack(side="right", padx=8)
        options = ttk.Frame(panel)
        options.pack(fill="x")
        options.columnconfigure((0, 1, 2), weight=1)
        ttk.Label(options, text="Video quality").grid(row=0, column=0, sticky="w")
        ttk.Label(options, text="Main audio").grid(row=0, column=1, sticky="w", padx=10)
        ttk.Label(options, text="Extra international audio").grid(row=0, column=2, sticky="w")
        self.quality_combo = ttk.Combobox(
            options, textvariable=self.resolution, values=["Best available"], width=19
        )
        self.quality_combo.grid(row=1, column=0, sticky="ew", pady=5)
        self.audio_combo = ttk.Combobox(
            options, textvariable=self.audio, values=list(LANGUAGES), width=19, state="readonly"
        )
        self.audio_combo.grid(row=1, column=1, sticky="ew", padx=10, pady=5)
        self.extra_combo = ttk.Combobox(
            options,
            textvariable=self.international,
            values=["None", *[n for n in LANGUAGES if n != "Team radio"]],
            state="readonly",
            width=23,
        )
        self.extra_combo.grid(row=1, column=2, sticky="ew", pady=5)
        ttk.Label(panel, text="3. Save your video", style="Section.TLabel").pack(
            anchor="w", pady=(14, 0)
        )
        folder = ttk.Frame(panel)
        folder.pack(fill="x", pady=8)
        ttk.Entry(folder, textvariable=self.directory).pack(side="left", fill="x", expand=True)
        ttk.Button(folder, text="Choose folder...", command=self.choose_folder).pack(
            side="left", padx=(8, 0)
        )
        file_row = ttk.Frame(panel)
        file_row.pack(fill="x")
        ttk.Label(file_row, text="Format").pack(side="left", padx=(0, 10))
        for value in ("mp4", "ts"):
            ttk.Radiobutton(file_row, text=value.upper(), value=value, variable=self.format).pack(
                side="left", padx=(0, 12)
            )
        ttk.Checkbutton(file_row, text="Replace existing file", variable=self.overwrite).pack(
            side="left", padx=10
        )
        self.download_button = ttk.Button(
            file_row, text="Download video", style="Primary.TButton", command=self.start_download
        )
        self.download_button.pack(side="right")
        self.progress = ttk.Progressbar(panel, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(14, 6))
        progress_row = ttk.Frame(panel)
        progress_row.pack(fill="x")
        ttk.Label(progress_row, textvariable=self.progress_text, style="Muted.TLabel").pack(
            side="left"
        )
        self.open_button = ttk.Button(progress_row, text="Open folder", command=self.open_folder)
        self.open_button.pack(side="right")
        self.log = tk.Text(
            panel,
            height=2,
            wrap="word",
            state="disabled",
            background="#f5f5f7",
            foreground="#646975",
            relief="flat",
            font="TkSmallCaptionFont",
        )
        self.log.pack(fill="both", expand=True, pady=(8, 0))

    def _account_panel(self):
        panel = self._scrollable_body(self.account_tab, 24)
        ttk.Label(panel, text="Sign in to F1 TV", style="Section.TLabel").pack(anchor="w")
        ttk.Label(panel, textvariable=self.account_status, style="Muted.TLabel").pack(
            anchor="w", pady=(8, 22)
        )
        form = ttk.Frame(panel)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        fields = [
            ("Sign-in method", self.method),
            ("Email", self.email),
            ("Password", self.password),
            ("Session token", self.token),
        ]
        self.account_entries = {}
        for row, (label, variable) in enumerate(fields):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", padx=(0, 18), pady=9)
            if row == 0:
                entry = ttk.Combobox(
                    form, textvariable=variable, values=list(METHODS), state="readonly"
                )
                entry.bind("<<ComboboxSelected>>", lambda event: self._account_fields())
            else:
                entry = ttk.Entry(form, textvariable=variable, show="*" if row in (2, 3) else "")
                self.account_entries[label] = entry
            entry.grid(row=row, column=1, sticky="ew", pady=9)
        self.remember_check = ttk.Checkbutton(
            panel, text="Remember sign-in on this computer", variable=self.remember
        )
        self.remember_check.pack(anchor="w", pady=(16, 12))
        ttk.Label(
            panel,
            text="Browser sign-in opens the official F1 sign-in page in Chrome/Chromium. "
            "Complete any verification there.\nEmail and password are optional "
            "when using the browser. The browser closes when authorization is received. "
            "Passwords are never saved.",
            wraplength=750,
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(0, 20))
        buttons = ttk.Frame(panel)
        buttons.pack(fill="x")
        self.signin_button = ttk.Button(
            buttons, text="Sign in", style="Primary.TButton", command=self.sign_in
        )
        self.signin_button.pack(side="left")
        self.signout_button = ttk.Button(buttons, text="Sign out", command=self.sign_out)
        self.signout_button.pack(side="left", padx=10)
        ttk.Separator(panel).pack(fill="x", pady=28)
        ttk.Label(panel, text="First time using browser sign-in?", style="Section.TLabel").pack(
            anchor="w"
        )
        ttk.Label(
            panel,
            text="Install the sign-in browser once. "
            "This requires internet access and may take a few minutes.",
            wraplength=750,
            style="Muted.TLabel",
        ).pack(anchor="w", pady=10)
        ttk.Button(panel, text="Install browser support", command=self.install_browser).pack(
            anchor="w"
        )

    def _settings_panel(self):
        panel = self._scrollable_body(self.settings_tab, 24)
        ttk.Label(panel, text="Download settings", style="Section.TLabel").pack(anchor="w")
        ttk.Label(
            panel,
            text="These settings are optional. The defaults work for most downloads.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(8, 18))
        form = ttk.Frame(panel)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        for row, (label, variable) in enumerate(
            [
                ("International audio offset", self.offset),
                ("Sample duration (seconds)", self.sample),
                ("Network timeout (seconds)", self.timeout),
                ("Country", self.country),
                ("Metadata entitlement", self.entitlement),
                ("Session cache file", self.cache_path),
                ("Chromium executable (optional)", self.browser_path),
            ]
        ):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", padx=(0, 14), pady=8)
            ttk.Entry(form, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=8)
        ttk.Label(
            panel,
            text="Leave sample duration blank to download the full video. Audio offset uses\n"
            "HH:MM:SS.mmm, for example -00:00:04.750. "
            "Country settings do not change account access.",
            wraplength=750,
            style="Muted.TLabel",
        ).pack(anchor="w", pady=14)
        ttk.Button(panel, text="Apply connection settings", command=self.apply_settings).pack(
            anchor="w"
        )
        ttk.Separator(panel).pack(fill="x", pady=22)
        ttk.Label(panel, text="Media tools", style="Section.TLabel").pack(anchor="w")
        ttk.Label(panel, textvariable=self.tool_status, wraplength=750, style="Muted.TLabel").pack(
            anchor="w", pady=10
        )
        ttk.Button(panel, text="Check again", command=self.check_tools).pack(anchor="w")
        ttk.Label(
            panel,
            text=f"F1 TV Downloader {__version__} | Runs locally on your computer",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(20, 0))
        self.check_tools()

    def check_tools(self):
        missing = [name for name in ("ffmpeg", "ffprobe") if not shutil.which(name)]
        tools = (
            "FFmpeg and FFprobe are ready."
            if not missing
            else "Missing: "
            + ", ".join(missing)
            + ". Install FFmpeg, then restart the app or check again."
        )
        self.tool_status.set(tools + "\n" + network_summary())

    def _account_fields(self):
        method = METHODS[self.method.get()]
        for name, entry in self.account_entries.items():
            enabled = (name == "Session token") == (method == "token")
            entry.configure(state="normal" if enabled and not self.busy else "disabled")
        self.remember_check.state(["disabled"] if method == "token" or self.busy else ["!disabled"])

    def _refresh_actions(self):
        if self.busy:
            return
        signed_in = self.service.signed_in
        self.account_status.set(
            "A session is ready. You can choose a video and download."
            if signed_in
            else "You are signed out. Choose a sign-in method below."
        )
        self.signout_button.state(["!disabled"] if signed_in else ["disabled"])
        count = len(self._channels())
        self.selection_text.set(f"{count} feed{'s' if count != 1 else ''} selected")
        ready = self.content is not None and signed_in and count > 0
        self.download_button.state(["!disabled"] if ready else ["disabled"])
        self.download_button.configure(
            text=f"Download selected ({count})" if count > 1 else "Download video"
        )
        for button in (self.inspect_button, self.link_button):
            button.state(["!disabled"] if ready and count == 1 else ["disabled"])
        self.open_button.state(["!disabled"] if self.last_output else ["disabled"])
        self._account_fields()

    def _walk_controls(self, widget):
        for child in widget.winfo_children():
            if (
                isinstance(
                    child,
                    (
                        ttk.Button,
                        ttk.Entry,
                        ttk.Combobox,
                        ttk.Checkbutton,
                        ttk.Radiobutton,
                        ttk.Treeview,
                    ),
                )
                and child is not self.cancel_button
            ):
                yield child
            yield from self._walk_controls(child)

    def _set_busy(self, busy):
        self.busy = busy
        if busy:
            self.saved_states = [
                (widget, widget.state()) for widget in self._walk_controls(self.root)
            ]
            for widget, _ in self.saved_states:
                widget.state(["disabled"])
            self.cancel_button.state(["!disabled"])
        else:
            for widget, states in self.saved_states:
                widget.state(["!disabled", "!readonly", *states])
            self.saved_states.clear()
            self.cancel_button.state(["disabled"])
            self._refresh_actions()

    def _run(self, message, operation, success=None):
        if self.busy:
            return
        self.batch_completed = []
        self.cancel_event = threading.Event()
        self._set_busy(True)
        self.status.set(message)

        def worker():
            try:
                result = operation(self.cancel_event)
                self.events.put(("done", (success, result, None)))
            except Exception as error:
                self.events.put(("done", (success, None, error)))

        self.executor.submit(worker)

    def _drain_events(self):
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "status":
                self.status.set(payload)
            elif kind == "log":
                self._append_log(payload)
            elif kind == "progress":
                self._show_progress(payload)
            elif kind == "queue":
                self._show_queue_update(payload)
            elif kind == "done":
                success, result, error = payload
                self.progress.stop()
                self._set_busy(False)
                if self.closing:
                    self._destroy()
                    return
                if isinstance(error, OperationCancelled):
                    self.status.set(
                        f"Cancelled. Kept {len(self.batch_completed)} completed download(s)."
                        if self.batch_completed
                        else "Cancelled. No incomplete video was saved."
                    )
                    self.progress_text.set("Cancelled")
                elif error is not None:
                    text = (
                        friendly_error(error)
                        if isinstance(error, (F1TVError, OSError))
                        else "An unexpected error occurred. Please try again."
                    )
                    self.status.set(text)
                    self._append_log(text)
                    self.progress_text.set("Operation failed")
                    messagebox.showerror("F1 TV Downloader", text, parent=self.root)
                elif success:
                    success(result)
        self.poll_id = self.root.after(80, self._drain_events)

    def _append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _show_progress(self, progress):
        parts = [
            f"Downloaded {clock_time(progress.seconds)}",
            f"{progress.total_bytes / 1048576:.1f} MB",
        ]
        if self.expected_seconds:
            percent = min(99.9, progress.seconds / self.expected_seconds * 100)
            overall = ((max(1, self.queue_index) - 1) * 100 + percent) / self.queue_total
            self.progress.configure(mode="determinate", value=overall)
            parts.insert(0, f"{percent:.0f}%")
        if self.queue_total > 1:
            parts.insert(0, f"Feed {self.queue_index}/{self.queue_total}")
        if progress.speed and progress.speed != "N/A":
            parts.append(progress.speed)
        self.progress_text.set(" | ".join(parts))

    def paste_url(self):
        try:
            self.url.set(self.root.clipboard_get().strip())
        except tk.TclError:
            self.status.set("The clipboard does not contain a video link.")

    def load_video(self):
        if self.busy:
            return
        url = self.url.get().strip()
        self.content = None
        self.feeds.delete(*self.feeds.get_children())
        self.queue_rows.clear()
        self.video_title.set("Loading video feeds...")
        self._run(
            "Loading video feeds...",
            lambda cancel: self.service.load_video(url),
            self._video_loaded,
        )

    def _video_loaded(self, content):
        self.content = content
        self.video_title.set(
            display_text(content.title)
            + (f" | {clock_time(content.duration)}" if content.duration else "")
        )
        self.feeds.delete(*self.feeds.get_children())
        self.queue_rows.clear()
        for index, channel in enumerate(content.channels):
            self.feeds.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    display_text(channel.title),
                    display_text(channel.driver_name),
                    channel.racing_number,
                    "",
                ),
            )
            self.queue_rows[channel.title] = str(index)
        if content.channels:
            default = content.channels.index(content.select_channel())
            self.feeds.selection_set(str(default))
            self.feeds.see(str(default))
        else:
            self.feeds.insert("", "end", iid="main", values=("Main feed", "", "", ""))
            self.queue_rows[None] = "main"
            self.feeds.selection_set("main")
        self.status.set(
            "Video loaded. Select one or more feeds to download."
            if self.service.signed_in
            else "Video loaded. Sign in on the Account tab to download."
        )
        self._refresh_actions()

    def _feed_changed(self, event=None):
        if self.busy:
            return
        count = len(self._channels())
        self.selection_text.set(f"{count} feed{'s' if count != 1 else ''} selected")
        self.resolution.set("Best available")
        self.quality_combo.configure(values=["Best available"])

        self._refresh_actions()

    def _channel(self):
        channels = self._channels()
        return channels[0] if channels else None

    def _channels(self):
        if self.content is None:
            return ()
        selection = set(self.feeds.selection())
        return tuple(
            self.content.channels[int(row)].title if row != "main" else None
            for row in self.feeds.get_children()
            if row in selection
        )

    def select_all_feeds(self):
        self.feeds.selection_set(self.feeds.get_children())
        self._feed_changed()

    def clear_feeds(self):
        self.feeds.selection_remove(self.feeds.selection())
        self._feed_changed()

    def sign_in(self):
        method = METHODS[self.method.get()]
        username, password, token = self.email.get(), self.password.get(), self.token.get()
        if (
            not password
            and username.strip()
            and username.strip() == os.environ.get("F1TV_USER", "").strip()
        ):
            password = os.environ.get("F1TV_PASSWORD") or os.environ.get("F1TV_PASS", "")
        remember, executable = self.remember.get(), self.browser_path.get().strip()
        self._run(
            "Signing in...",
            lambda cancel: self.service.sign_in(
                method,
                username=username,
                password=password,
                token=token,
                remember=remember,
                executable=executable,
                cancel=cancel,
                status=lambda text: self.events.put(("status", text)),
            ),
            self._signed_in,
        )

    def _signed_in(self, result):
        self.password.set("")
        self.token.set("")
        self.status.set("Sign-in complete. Choose a video to download.")
        self.tabs.select(self.download_tab)
        self._refresh_actions()

    def sign_out(self):
        self._run("Signing out...", lambda cancel: self.service.sign_out(), self._signed_out)

    def _signed_out(self, result):
        self.password.set("")
        self.token.set("")
        self.status.set("Signed out. The saved session was removed.")
        self._refresh_actions()

    def install_browser(self):
        self._run(
            "Setting up browser sign-in...",
            lambda cancel: install_browser_support(
                cancel=cancel,
                status=lambda text: self.events.put(("status", text)),
            ),
            lambda result: self.status.set("Browser sign-in is ready. Click Sign in to continue."),
        )

    def inspect(self):
        if len(self._channels()) != 1:
            self.status.set("Select one feed to check its quality and audio.")
            return
        channel = self._channel()
        self._run(
            "Checking available streams...",
            lambda cancel: self.service.inspect(self.content, channel, cancel),
            self._streams_loaded,
        )

    def _streams_loaded(self, info):
        self.quality_combo.configure(values=["Best available", *info.resolutions])
        names = {code: name for name, code in LANGUAGES.items()}
        available = [names.get(code, code) for code in info.languages]
        self.audio_combo.configure(values=available or list(LANGUAGES))
        if available and self.audio.get() not in available:
            self.audio.set(available[0])
        self.status.set("Available quality and audio updated for the selected feed.")

    def copy_link(self):
        if len(self._channels()) != 1:
            self.status.set("Select one feed to copy its playback link.")
            return
        channel = self._channel()
        self._run(
            "Getting playback link...",
            lambda cancel: self.service.stream_url(self.content, channel),
            self._link_loaded,
        )

    def _link_loaded(self, url):
        self.root.clipboard_clear()
        self.root.clipboard_append(url)
        self.status.set("Playback link copied to the clipboard. The link may expire.")

    def choose_folder(self):
        folder = filedialog.askdirectory(
            parent=self.root,
            title="Save videos to",
            initialdir=self.directory.get()
            if Path(self.directory.get()).is_dir()
            else str(Path.cwd()),
        )
        if folder:
            self.directory.set(folder)

    def _options(self):
        directory = self.directory.get().strip()
        if not directory:
            raise F1TVError("Choose a folder for your download.")
        try:
            duration = float(self.sample.get()) if self.sample.get().strip() else None
        except ValueError:
            raise F1TVError("Sample duration must be a number of seconds, or blank.") from None
        extra = self.international.get()
        options = DownloadOptions(
            directory=Path(directory).expanduser(),
            channel=self._channel(),
            resolution="best"
            if self.resolution.get() == "Best available"
            else self.resolution.get().strip(),
            audio=LANGUAGES.get(self.audio.get(), self.audio.get()),
            international=None if extra == "None" else LANGUAGES.get(extra, extra),
            offset=self.offset.get().strip(),
            format=self.format.get(),
            duration=duration,
            overwrite=self.overwrite.get(),
        )
        options.validate()
        return options

    def start_download(self):
        if self.busy or self.content is None:
            return
        channels = self._channels()
        if not channels:
            self.status.set("Select at least one feed to download.")
            return
        try:
            options = self._options()
        except F1TVError as error:
            messagebox.showerror("Check download settings", str(error), parent=self.root)
            return
        self.expected_seconds = options.duration or self.content.duration
        self.queue_total = len(channels)
        self.queue_index = 1
        self.progress.configure(
            value=0, mode="determinate" if self.expected_seconds else "indeterminate"
        )
        if not self.expected_seconds:
            self.progress.start(15)
        self.progress_text.set("Preparing download...")
        self._run(
            "Preparing download...",
            lambda cancel: self.service.download_many(
                self.content,
                options,
                channels,
                cancel=cancel,
                progress=lambda value: self.events.put(("progress", value)),
                status=lambda text: self.events.put(("status", text)),
                item_status=lambda update: self.events.put(("queue", update)),
            ),
            self._downloads_done,
        )

    def _show_queue_update(self, update):
        row = self.queue_rows.get(update.channel)
        if row is not None and self.feeds.exists(row):
            self.feeds.set(row, "status", update.state)
        self.queue_total = update.total
        if update.state == "Downloading":
            self.queue_index = update.index
            self.progress_text.set(
                f"Feed {update.index}/{update.total}: {update.channel or 'Main feed'}"
            )
            if self.expected_seconds:
                self.progress.configure(value=(update.index - 1) / update.total * 100)
        elif update.output is not None:
            self.last_output = update.output
            self.batch_completed.append(update.output)
            self._append_log(f"Saved to {update.output}")
        elif update.error:
            self._append_log(f"{update.channel or 'Main feed'}: {update.error}")

    def _downloads_done(self, result):
        self.progress.configure(mode="determinate", value=100)
        completed, failed = len(result.completed), len(result.failed)
        if failed:
            summary = f"{completed} downloaded, {failed} failed. Completed files were kept."
            self.progress_text.set(summary)
            details = "\n".join(
                f"{name or 'Main feed'}: {error}" for name, error in result.failed[:5]
            )
            self.status.set(summary)
            if self.queue_total == 1:
                messagebox.showerror("Download failed", details, parent=self.root)
            else:
                messagebox.showwarning(
                    "Downloads finished", summary + "\n\n" + details, parent=self.root
                )
        else:
            self.progress_text.set(
                "Download complete" if completed == 1 else f"{completed} downloads complete"
            )
            self.status.set(
                f"Saved {result.completed[0].name}"
                if completed == 1
                else f"Saved {completed} files."
            )
        self._refresh_actions()

    def cancel(self):
        self.cancel_event.set()
        self.cancel_button.state(["disabled"])
        self.status.set("Cancelling... Waiting for the current step to stop.")

    def apply_settings(self):
        try:
            country, entitlement = self.country.get().strip(), self.entitlement.get().strip()
            timeout = float(self.timeout.get())
            if not re.fullmatch(r"[A-Za-z]{2,3}", country):
                raise ValueError("Country must be a two- or three-letter code.")
            if not re.fullmatch(r"[A-Za-z0-9_]+", entitlement):
                raise ValueError("Metadata entitlement must use letters, digits, and underscores.")
            if not 0 < timeout <= 600:
                raise ValueError("Network timeout must be between 0 and 600 seconds.")
            path = (
                Path(self.cache_path.get()).expanduser() if self.cache_path.get().strip() else None
            )
            replacement = DesktopService(Settings(country.upper(), entitlement, timeout), path)
            if self.service.signed_in:
                replacement.sessions = self.service.sessions
            self.service = replacement
            self.content = None
            self.feeds.delete(*self.feeds.get_children())
            self.video_title.set("Load the video again to use the updated settings.")
            self.status.set("Connection settings updated.")
            self._refresh_actions()
        except (ValueError, RuntimeError) as error:
            messagebox.showerror("Check settings", str(error), parent=self.root)

    def open_folder(self):
        if not self.last_output:
            return
        folder = str(self.last_output.parent)
        try:
            if sys.platform == "win32":
                os.startfile(folder)
            else:
                subprocess.Popen(
                    ["open" if sys.platform == "darwin" else "xdg-open", folder],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except OSError:
            messagebox.showinfo("Download folder", folder, parent=self.root)

    def close(self):
        if self.busy:
            if not messagebox.askyesno(
                "Close downloader?", "Cancel the current operation and close?", parent=self.root
            ):
                return
            # Tk keeps processing events while the confirmation dialog is open.
            if self.busy:
                self.closing = True
                self.cancel()
            else:
                self._destroy()
        else:
            self._destroy()

    def _destroy(self):
        logging.getLogger("f1tv_dl").removeHandler(self.log_handler)
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.root.after_cancel(self.poll_id)
        self.root.destroy()


def main():
    if tk is None:
        print(
            "Tk is required for the desktop app. On Ubuntu/Debian install python3-tk. "
            "On Windows/macOS use a Python installation that includes Tk.",
            file=sys.stderr,
        )
        return 1
    try:
        root = tk.Tk()
    except tk.TclError:
        print(
            "The desktop app needs a graphical display. Run it on your desktop computer.",
            file=sys.stderr,
        )
        return 1
    DownloaderApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
