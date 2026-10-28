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
PROFILES = {"Web (standard)": "web", "Android TV (4K / HDR)": "android-tv"}
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
COLORS = {
    "page": "#eef1f6",
    "card": "#ffffff",
    "text": "#172033",
    "muted": "#66758b",
    "accent": "#db303b",
    "accent_hover": "#ba2330",
    "border": "#dce2eb",
    "field": "#f7f9fc",
    "hero": "#172133",
}


def clock_time(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def friendly_error(error):
    text = str(error)
    if text.startswith("Android TV playback requires Browser sign-in."):
        return (
            "Android TV playback needs a browser session. Sign in using Browser sign-in "
            "on the Account tab, then try again."
        )
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
        self.manifest_report = None
        self.manifest_window = None
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
        self.playback_profile = tk.StringVar(value="Web (standard)")
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
        colors = COLORS
        self.root.title("F1 TV Downloader")
        width = min(1080, max(820, self.root.winfo_screenwidth() - 100))
        height = min(940, max(640, self.root.winfo_screenheight() - 100))
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(820, 640)
        self.root.configure(background=colors["page"])
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
        self.style.configure(
            ".", font=default_font, background=colors["card"], foreground=colors["text"]
        )
        self.style.configure("TFrame", background=colors["card"])
        self.style.configure("Page.TFrame", background=colors["page"])
        self.style.configure(
            "Card.TFrame",
            background=colors["card"],
            bordercolor=colors["border"],
            lightcolor=colors["border"],
            darkcolor=colors["border"],
            borderwidth=1,
            relief="solid",
        )
        self.style.configure("Hero.TFrame", background=colors["hero"])
        self.style.configure("TLabel", background=colors["card"], foreground=colors["text"])
        self.style.configure("Muted.TLabel", foreground=colors["muted"])
        self.style.configure(
            "PageMuted.TLabel", background=colors["page"], foreground=colors["muted"]
        )
        family = default_font.actual("family")
        self.style.configure(
            "Title.TLabel", background=colors["hero"], foreground="white", font=(family, 23, "bold")
        )
        self.style.configure("Subtitle.TLabel", background=colors["hero"], foreground="#b3bfd2")
        self.style.configure("Section.TLabel", font=(family, 13, "bold"))
        self.style.configure("Video.TLabel", font=(family, 11, "bold"))
        self.style.configure(
            "Step.TLabel",
            background="#fff0f1",
            foreground=colors["accent"],
            padding=(8, 5),
            font=(family, 9, "bold"),
        )
        self.style.configure(
            "Count.TLabel",
            background=colors["field"],
            foreground=colors["muted"],
            padding=(10, 6),
            font=(family, 9, "bold"),
        )
        self.style.configure(
            "SignedIn.TLabel",
            background="#194238",
            foreground="#b0ead3",
            padding=(12, 7),
            font=(family, 9, "bold"),
        )
        self.style.configure(
            "SignedOut.TLabel",
            background="#29364b",
            foreground="#d0d9e7",
            padding=(12, 7),
            font=(family, 9, "bold"),
        )
        self.style.configure(
            "TButton",
            padding=(14, 9),
            background=colors["card"],
            foreground=colors["text"],
            bordercolor=colors["border"],
            lightcolor=colors["card"],
            darkcolor=colors["card"],
            borderwidth=1,
            relief="flat",
            focuscolor=colors["accent"],
        )
        self.style.map(
            "TButton",
            background=[("active", colors["field"])],
            foreground=[("disabled", "#9aa6b6")],
        )
        self.style.configure("Compact.TButton", padding=(11, 6), font=(family, 9))
        for widget in ("TCheckbutton", "TRadiobutton"):
            self.style.configure(
                widget,
                background=colors["card"],
                foreground=colors["text"],
                indicatormargin=(0, 2, 6, 2),
            )
            self.style.map(
                widget,
                background=[("active", colors["card"])],
                foreground=[("disabled", "#9aa6b6")],
            )
        self.style.configure(
            "TEntry",
            padding=(9, 8),
            fieldbackground=colors["card"],
            bordercolor=colors["border"],
            lightcolor=colors["card"],
            darkcolor=colors["card"],
        )
        self.style.map(
            "TEntry",
            bordercolor=[("focus", colors["accent"])],
            fieldbackground=[("disabled", colors["field"])],
            foreground=[("disabled", "#9aa6b6")],
        )
        self.style.configure(
            "TCombobox",
            padding=(9, 7),
            fieldbackground=colors["card"],
            background=colors["field"],
            bordercolor=colors["border"],
            lightcolor=colors["card"],
            darkcolor=colors["card"],
            arrowcolor=colors["muted"],
        )
        self.style.map(
            "TCombobox",
            fieldbackground=[("readonly", colors["card"])],
            foreground=[("disabled", "#9aa6b6"), ("readonly", colors["text"])],
        )
        self.style.configure(
            "Primary.TButton",
            foreground="white",
            background=colors["accent"],
            bordercolor=colors["accent"],
            lightcolor=colors["accent"],
            darkcolor=colors["accent"],
            padding=(18, 10),
            font=(family, 10, "bold"),
        )
        self.style.map(
            "Primary.TButton",
            background=[("disabled", "#e2e7ef"), ("active", colors["accent_hover"])],
            foreground=[("disabled", "#95a0b1")],
            bordercolor=[("disabled", "#e2e7ef"), ("active", colors["accent_hover"])],
            lightcolor=[("disabled", "#e2e7ef")],
            darkcolor=[("disabled", "#e2e7ef")],
        )
        self.style.configure(
            "TNotebook",
            background=colors["page"],
            borderwidth=0,
            bordercolor=colors["page"],
            lightcolor=colors["page"],
            darkcolor=colors["page"],
        )
        self.style.configure(
            "TNotebook.Tab",
            padding=(25, 11),
            background=colors["page"],
            bordercolor=colors["page"],
            lightcolor=colors["page"],
            darkcolor=colors["page"],
            font=(family, 10, "bold"),
        )
        self.style.map(
            "TNotebook.Tab",
            padding=[("selected", (25, 11)), ("!selected", (25, 11))],
            expand=[("selected", (0, 0, 0, 0))],
            background=[("selected", colors["card"]), ("active", "#e5eaf2")],
            foreground=[("selected", colors["accent"]), ("!selected", colors["muted"])],
        )
        self.style.configure(
            "Treeview",
            rowheight=32,
            background=colors["card"],
            fieldbackground=colors["card"],
            bordercolor=colors["border"],
            lightcolor=colors["border"],
            darkcolor=colors["border"],
            borderwidth=1,
        )
        self.style.configure(
            "Treeview.Heading",
            background=colors["field"],
            foreground=colors["muted"],
            padding=(8, 9),
            relief="flat",
            font=(family, 9, "bold"),
        )
        self.style.map("Treeview.Heading", background=[("active", colors["field"])])
        self.style.map(
            "Treeview",
            background=[("selected", "#ffedf0")],
            foreground=[("selected", colors["text"])],
        )
        self.style.configure(
            "Horizontal.TProgressbar",
            background=colors["accent"],
            troughcolor="#e8ecf3",
            bordercolor="#e8ecf3",
            lightcolor=colors["accent"],
            darkcolor=colors["accent"],
            borderwidth=0,
            thickness=7,
        )
        self.style.configure(
            "Vertical.TScrollbar",
            background="#ccd4e0",
            troughcolor=colors["page"],
            bordercolor=colors["page"],
            lightcolor=colors["page"],
            darkcolor=colors["page"],
            arrowcolor=colors["muted"],
            arrowsize=11,
            width=12,
        )

    def _layout(self):
        outer = ttk.Frame(self.root, padding=22, style="Page.TFrame")
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)
        header = ttk.Frame(outer, style="Hero.TFrame", padding=(22, 20))
        header.grid(row=0, column=0, sticky="ew", pady=(0, 18))
        header.columnconfigure(1, weight=1)
        mark = tk.Canvas(
            header, width=42, height=42, background=COLORS["hero"], highlightthickness=0
        )
        mark.grid(row=0, column=0, rowspan=2, padx=(0, 16))
        mark.create_polygon(0, 6, 42, 6, 32, 16, 0, 16, fill=COLORS["accent"], outline="")
        mark.create_polygon(0, 21, 27, 21, 17, 31, 0, 31, fill=COLORS["accent"], outline="")
        mark.create_polygon(0, 36, 12, 36, 6, 42, 0, 42, fill=COLORS["accent"], outline="")
        ttk.Label(header, text="F1 TV Downloader", style="Title.TLabel").grid(
            row=0, column=1, sticky="w"
        )
        ttk.Label(
            header, text="Choose feeds, set quality, and save your race.", style="Subtitle.TLabel"
        ).grid(row=1, column=1, sticky="w", pady=(5, 0))
        self.session_badge = ttk.Label(header, text="Signed out", style="SignedOut.TLabel")
        self.session_badge.grid(row=0, column=2, rowspan=2, sticky="e", padx=(20, 0))
        self.tabs = ttk.Notebook(outer)
        self.tabs.grid(row=2, column=0, sticky="nsew")
        self.download_tab = ttk.Frame(self.tabs, style="Page.TFrame")
        self.account_tab = ttk.Frame(self.tabs, style="Page.TFrame")
        self.settings_tab = ttk.Frame(self.tabs, style="Page.TFrame")
        self.tabs.add(self.download_tab, text="Download")
        self.tabs.add(self.account_tab, text="Account")
        self.tabs.add(self.settings_tab, text="Settings")
        self._download_panel()
        self._account_panel()
        self._settings_panel()
        footer = ttk.Frame(outer, style="Card.TFrame", padding=(16, 12))
        footer.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        footer.columnconfigure(0, weight=1)
        self.footer_status = ttk.Label(footer, textvariable=self.status, wraplength=640)
        self.footer_status.grid(row=0, column=0, sticky="ew")
        self.download_button = ttk.Button(
            footer, text="Download video", style="Primary.TButton", command=self.start_download
        )
        self.download_button.grid(row=0, column=1, sticky="e", padx=(12, 0))
        self.cancel_button = ttk.Button(
            footer, text="Cancel", command=self.cancel, state="disabled"
        )
        self.cancel_button.grid(row=0, column=2, sticky="e", padx=(12, 0))
        footer.bind("<Configure>", self._footer_wrap)
        self.tabs.bind("<<NotebookTabChanged>>", self._tab_changed)
        for event in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.root.bind_all(event, self._scroll_page, add="+")

    def _tab_changed(self, event=None):
        if self.tabs.select() == str(self.download_tab):
            self.download_button.grid()
        else:
            self.download_button.grid_remove()
        self._footer_wrap()

    def _footer_wrap(self, event=None):
        width = self.footer_status.master.winfo_width()
        buttons = self.cancel_button.winfo_reqwidth()
        if self.download_button.winfo_manager():
            buttons += self.download_button.winfo_reqwidth() + 12
        self.footer_status.configure(wraplength=max(180, width - buttons - 48))

    def _scrollable_body(self, page, padding):
        canvas = tk.Canvas(
            page, highlightthickness=0, background=COLORS["page"], yscrollincrement=22
        )
        scrollbar = ttk.Scrollbar(page, orient="vertical", command=canvas.yview)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        canvas.configure(yscrollcommand=scrollbar.set)
        body = ttk.Frame(canvas, padding=padding, style="Page.TFrame")
        window = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        self.page_canvases[str(page)] = canvas
        return body

    def _card(self, parent, title, step=None):
        card = ttk.Frame(parent, style="Card.TFrame", padding=(18, 16))
        card.pack(fill="x", pady=(0, 14))
        heading = ttk.Frame(card)
        heading.pack(fill="x", pady=(0, 14))
        if step is not None:
            ttk.Label(heading, text=step, style="Step.TLabel").pack(side="left", padx=(0, 10))
        ttk.Label(heading, text=title, style="Section.TLabel").pack(side="left")
        return card, heading

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
        video, _ = self._card(panel, "Video & feeds", "01")
        row = ttk.Frame(video)
        row.pack(fill="x", pady=(8, 6))
        self.url_entry = ttk.Entry(row, textvariable=self.url)
        self.url_entry.pack(side="left", fill="x", expand=True)
        self.url_entry.bind("<Return>", lambda event: self.load_video())
        ttk.Button(row, text="Paste", command=self.paste_url).pack(side="left", padx=6)
        self.load_button = ttk.Button(
            row, text="Load video", command=self.load_video, style="Primary.TButton"
        )
        self.load_button.pack(side="left")
        ttk.Label(video, textvariable=self.video_title, wraplength=800, style="Video.TLabel").pack(
            anchor="w", pady=(10, 12)
        )
        tree_row = ttk.Frame(video)
        tree_row.pack(fill="x")
        self.feed_check_images = {}
        for checked in (False, True):
            for disabled in (False, True):
                icon = tk.PhotoImage(master=self.root, width=18, height=18)
                border = "#b8c1cf" if disabled else COLORS["accent"] if checked else "#95a2b6"
                icon.put(border, to=(2, 2, 16, 16))
                icon.put(border if checked else "#ffffff", to=(3, 3, 15, 15))
                if checked:
                    for x, y in ((5, 8), (6, 9), (7, 10), (8, 9), (9, 8), (10, 7), (11, 6)):
                        icon.put("#ffffff", to=(x, y, x + 2, y + 2))
                self.feed_check_images[checked, disabled] = icon
        self.feeds = ttk.Treeview(
            tree_row,
            columns=("feed", "driver", "number", "status"),
            show="tree headings",
            height=4,
            selectmode="none",
        )
        self.feeds.heading("#0", text="Select")
        self.feeds.column("#0", width=64, minwidth=64, stretch=False)
        for column, title, width in (
            ("feed", "Feed", 230),
            ("driver", "Driver", 370),
            ("number", "No.", 90),
            ("status", "Status", 120),
        ):
            self.feeds.heading(column, text=title)
            self.feeds.column(column, width=width, minwidth=70)
        scroll = ttk.Scrollbar(tree_row, orient="vertical", command=self.feeds.yview)
        scroll.pack(side="right", fill="y")
        self.feeds.pack(side="left", fill="x", expand=True)
        self.feeds.configure(yscrollcommand=scroll.set)
        self.feeds.bind("<<TreeviewSelect>>", self._feed_changed)
        self.feeds.bind("<Button-1>", self._feed_clicked)
        self.feeds.bind("<space>", self._feed_space)
        self.feeds.tag_configure("alternate", background="#f8fafd")
        selection_row = ttk.Frame(video)
        selection_row.pack(fill="x", pady=(12, 0))
        ttk.Button(
            selection_row, text="Select all", command=self.select_all_feeds, style="Compact.TButton"
        ).pack(side="left")
        ttk.Button(
            selection_row, text="Clear", command=self.clear_feeds, style="Compact.TButton"
        ).pack(side="left", padx=8)
        ttk.Label(selection_row, textvariable=self.selection_text, style="Count.TLabel").pack(
            side="right"
        )
        ttk.Label(
            video,
            text="One file per feed. Click a checkbox or press Space to select.",
            style="Muted.TLabel",
            wraplength=750,
        ).pack(anchor="w", pady=(8, 0))
        quality, actions = self._card(panel, "Quality & audio", "02")
        self.link_button = ttk.Button(
            actions, text="Copy playback link", command=self.copy_link, style="Compact.TButton"
        )
        self.link_button.pack(side="right")
        self.inspect_button = ttk.Button(
            actions, text="Check streams", command=self.inspect, style="Compact.TButton"
        )
        self.inspect_button.pack(side="right", padx=8)
        profile_row = ttk.Frame(quality)
        profile_row.pack(fill="x", pady=(0, 12))
        profile_row.columnconfigure(2, weight=1)
        ttk.Label(profile_row, text="Playback profile").grid(row=0, column=0, sticky="w")
        self.profile_combo = ttk.Combobox(
            profile_row,
            textvariable=self.playback_profile,
            values=list(PROFILES),
            state="readonly",
            width=25,
        )
        self.profile_combo.grid(row=0, column=1, sticky="w", padx=(12, 16))
        self.profile_combo.bind("<<ComboboxSelected>>", self._profile_changed)
        ttk.Label(
            profile_row,
            text="UHD / HDR when available. Requires browser sign-in.",
            style="Muted.TLabel",
            wraplength=220,
        ).grid(row=0, column=2, sticky="w")
        options = ttk.Frame(quality)
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
        manifest_row = ttk.Frame(quality)
        manifest_row.pack(fill="x", pady=(14, 0))
        self.manifest_button = ttk.Button(
            manifest_row,
            text="Check manifest / 4K",
            command=self.check_manifest,
            style="Compact.TButton",
        )
        self.manifest_button.pack(side="left", padx=(0, 12))
        ttk.Label(
            manifest_row,
            text="Compare resolutions and DRM for one selected feed.",
            style="Muted.TLabel",
            wraplength=460,
        ).pack(side="left", fill="x", expand=True)
        save, _ = self._card(panel, "Save your video", "03")
        folder = ttk.Frame(save)
        folder.pack(fill="x", pady=8)
        ttk.Entry(folder, textvariable=self.directory).pack(side="left", fill="x", expand=True)
        ttk.Button(folder, text="Choose folder...", command=self.choose_folder).pack(
            side="left", padx=(8, 0)
        )
        file_row = ttk.Frame(save)
        file_row.pack(fill="x", pady=(8, 0))
        ttk.Label(file_row, text="Format").pack(side="left", padx=(0, 10))
        for value in ("mp4", "ts"):
            ttk.Radiobutton(file_row, text=value.upper(), value=value, variable=self.format).pack(
                side="left", padx=(0, 12)
            )
        ttk.Checkbutton(file_row, text="Replace existing file", variable=self.overwrite).pack(
            side="left", padx=10
        )
        sample_row = ttk.Frame(save)
        sample_row.pack(fill="x", pady=(12, 0))
        ttk.Label(sample_row, text="Sample length (seconds)").pack(side="left")
        self.sample_entry = ttk.Entry(sample_row, textvariable=self.sample, width=8)
        self.sample_entry.pack(side="left", padx=12)
        ttk.Label(sample_row, text="Leave blank for the full video.", style="Muted.TLabel").pack(
            side="left",
        )
        self.progress = ttk.Progressbar(save, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(18, 10))
        progress_row = ttk.Frame(save)
        progress_row.pack(fill="x")
        ttk.Label(progress_row, textvariable=self.progress_text, style="Muted.TLabel").pack(
            side="left"
        )
        self.open_button = ttk.Button(
            progress_row, text="Open folder", command=self.open_folder, style="Compact.TButton"
        )
        self.open_button.pack(side="right")
        self.log = tk.Text(
            save,
            height=2,
            wrap="word",
            state="disabled",
            background=COLORS["field"],
            foreground=COLORS["muted"],
            relief="flat",
            font="TkSmallCaptionFont",
            padx=10,
            pady=8,
        )
        self.log.pack(fill="both", expand=True, pady=(8, 0))

    def _account_panel(self):
        page = self._scrollable_body(self.account_tab, 16)
        panel, _ = self._card(page, "Your F1 TV account")
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
            text="Browser sign-in opens Chrome/Chromium. Complete verification there; "
            "the window closes when authorization is received. "
            "Email and password are optional for browser sign-in. "
            "Passwords are never saved.",
            wraplength=650,
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
        setup, _ = self._card(page, "Browser setup")
        ttk.Label(
            setup,
            text="Install the sign-in browser once. "
            "This requires internet access and may take a few minutes.",
            wraplength=750,
            style="Muted.TLabel",
        ).pack(anchor="w", pady=10)
        ttk.Button(setup, text="Install browser support", command=self.install_browser).pack(
            anchor="w"
        )

    def _settings_panel(self):
        page = self._scrollable_body(self.settings_tab, 16)
        panel, _ = self._card(page, "Download settings")
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
        tools, _ = self._card(page, "Media tools")
        ttk.Label(tools, textvariable=self.tool_status, wraplength=750, style="Muted.TLabel").pack(
            anchor="w", pady=10
        )
        ttk.Button(tools, text="Check again", command=self.check_tools).pack(anchor="w")
        ttk.Label(
            page,
            text=f"F1 TV Downloader {__version__} | Runs locally on your computer",
            style="PageMuted.TLabel",
        ).pack(anchor="w", pady=(4, 0))
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
        self._sync_feed_checks()
        signed_in = self.service.signed_in
        self.session_badge.configure(
            text="Signed in" if signed_in else "Signed out",
            style="SignedIn.TLabel" if signed_in else "SignedOut.TLabel",
        )
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
        for button in (self.inspect_button, self.link_button, self.manifest_button):
            button.state(["!disabled"] if ready and count == 1 else ["disabled"])
        self.open_button.state(["!disabled"] if self.last_output else ["disabled"])
        self._account_fields()
        self._footer_wrap()

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
            self._sync_feed_checks()
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
                tags=("alternate",) if index % 2 else (),
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
            self.feeds.focus(str(default))
            self.feeds.see(str(default))
        else:
            self.feeds.insert("", "end", iid="main", values=("Main feed", "", "", ""))
            self.queue_rows[None] = "main"
            self.feeds.selection_set("main")
            self.feeds.focus("main")
        self.status.set(
            "Video loaded. Check one or more feeds to download."
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

    def _profile_changed(self, event=None):
        if self.busy:
            return
        self._feed_changed()
        self.status.set(
            "Playback profile updated. Click Check streams to refresh available quality."
        )

    def _sync_feed_checks(self):
        checked = set(self.feeds.selection())
        for row in self.feeds.get_children():
            self.feeds.item(row, image=self.feed_check_images[row in checked, self.busy])

    def _toggle_feed(self, row):
        if self.busy or self.feeds.instate(["disabled"]) or not row:
            return
        self.feeds.focus(row)
        if row in self.feeds.selection():
            self.feeds.selection_remove(row)
        else:
            self.feeds.selection_add(row)
        self._feed_changed()

    def _feed_clicked(self, event):
        if self.busy:
            return "break"
        row = self.feeds.identify_row(event.y)
        if row and self.feeds.identify_region(event.x, event.y) in {"tree", "cell"}:
            self.feeds.focus_set()
            self._toggle_feed(row)
            return "break"
        return None

    def _feed_space(self, event):
        self._toggle_feed(self.feeds.focus())
        return "break"

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
        if self.busy:
            return
        self.feeds.selection_set(self.feeds.get_children())
        self._feed_changed()

    def clear_feeds(self):
        if self.busy:
            return
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
        profile = PROFILES[self.playback_profile.get()]
        self._run(
            "Checking available streams...",
            lambda cancel: self.service.inspect(self.content, channel, cancel, profile=profile),
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

    def check_manifest(self):
        if self.busy:
            return
        if len(self._channels()) != 1 or not self.service.signed_in:
            self.status.set("Sign in and select one feed to check its manifest.")
            return
        content, channel = self.content, self._channel()
        self._run(
            "Checking manifests and 4K availability...",
            lambda cancel: self.service.check_manifest(
                content,
                channel,
                cancel=cancel,
                status=lambda text: self.events.put(("status", text)),
            ),
            self._manifest_checked,
        )

    def _manifest_checked(self, report):
        self.manifest_report = report
        self.status.set(
            "Manifest check complete. Copy or save the report to share the results."
            if report.inspected
            else "No manifest could be inspected. The report contains the access errors."
        )
        if self.manifest_window is not None:
            self.manifest_window.destroy()
        window = self.manifest_window = tk.Toplevel(self.root)
        window.title("Manifest check / 4K")
        window.geometry("800x620")
        window.minsize(620, 400)
        window.transient(self.root)
        window.protocol("WM_DELETE_WINDOW", window.withdraw)
        body = ttk.Frame(window, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Manifest check", style="Section.TLabel").pack(anchor="w")
        ttk.Label(
            body,
            text="Resolutions, DRM markers, and access errors. No media was downloaded.",
            style="Muted.TLabel",
            wraplength=700,
        ).pack(anchor="w", pady=(8, 14))
        row = ttk.Frame(body)
        row.pack(fill="both", expand=True)
        self.manifest_text = tk.Text(
            row,
            wrap="word",
            font="TkTextFont",
            padx=12,
            pady=12,
            background=COLORS["field"],
            foreground=COLORS["text"],
            relief="flat",
        )
        scroll = ttk.Scrollbar(row, orient="vertical", command=self.manifest_text.yview)
        scroll.pack(side="right", fill="y")
        self.manifest_text.pack(side="left", fill="both", expand=True)
        self.manifest_text.configure(yscrollcommand=scroll.set)
        self.manifest_text.insert("1.0", report.to_text())
        self.manifest_text.configure(state="disabled")
        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(14, 0))
        self.copy_report_button = ttk.Button(
            actions,
            text="Copy report",
            command=self.copy_manifest_report,
        )
        self.copy_report_button.pack(side="left")
        self.save_report_button = ttk.Button(
            actions,
            text="Save report...",
            command=self.save_manifest_report,
        )
        self.save_report_button.pack(side="left", padx=8)
        ttk.Button(actions, text="Close", command=window.withdraw).pack(side="right")

    def copy_manifest_report(self):
        if self.manifest_report is not None:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.manifest_report.to_json())
            self.status.set("Manifest report copied. Tokens and playback URLs are omitted.")

    def save_manifest_report(self):
        if self.manifest_report is None:
            return
        path = filedialog.asksaveasfilename(
            parent=self.manifest_window,
            title="Save manifest report",
            defaultextension=".json",
            initialfile="f1tv-manifest-check.json",
            filetypes=[("JSON report", "*.json")],
        )
        if path:
            try:
                Path(path).write_text(self.manifest_report.to_json(), encoding="utf-8")
                self.status.set("Manifest report saved.")
            except OSError:
                messagebox.showerror(
                    "Save manifest report",
                    "Could not save the report. Choose a writable folder.",
                    parent=self.manifest_window,
                )

    def copy_link(self):
        if len(self._channels()) != 1:
            self.status.set("Select one feed to copy its playback link.")
            return
        channel = self._channel()
        profile = PROFILES[self.playback_profile.get()]
        self._run(
            "Getting playback link...",
            lambda cancel: self.service.stream_url(self.content, channel, profile=profile),
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
            playback_profile=PROFILES[self.playback_profile.get()],
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
