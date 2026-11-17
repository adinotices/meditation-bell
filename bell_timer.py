#!/usr/bin/env python3
"""Meditation Bell — a quiet meditation timer with interval bells and a practice journal.

GTK3 + GStreamer. Data lives in ~/.local/share/bell-timer/ (readable only by you):
  sessions.jsonl     one JSON object per saved meditation
  current.json       checkpoint of the meditation in progress (recovered after a crash)
  unsaved-*.json     meditations that couldn't be written to the journal; retried
                     at launch and before each new meditation
  config.json        last-used settings

Colours: two palettes (paper / ink) that follow the COSMIC light/dark mode
unless overridden. Every text/background pair is WCAG 2.2 AA (most AAA);
UI boundaries, focus rings and chart marks are >= 3:1 (SC 1.4.11).
"""
import datetime as dt
import fcntl
import glob
import json
import math
import os
import re
import shutil
import signal
import sys
import time
import warnings

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gst", "1.0")
gi.require_version("Gdk", "3.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, Gio, GLib, Gst, Gtk, Pango, PangoCairo  # noqa: E402

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.expanduser("~/.local/share/bell-timer")
BELL_PATH = os.path.join(APP_DIR, "interval_bell.wav")   # interval bell
START_BELL_PATH = os.path.join(APP_DIR, "start_bell.wav")  # opening bell
END_BELL_PATH = os.path.join(APP_DIR, "end_bell.wav")      # closing bell
LOG_PATH = os.path.join(DATA_DIR, "sessions.jsonl")
CURRENT_PATH = os.path.join(DATA_DIR, "current.json")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
COSMIC_DARK_PATH = os.path.expanduser(
    "~/.config/cosmic/com.system76.CosmicTheme.Mode/v1/is_dark")
COSMIC_IDLE_DIR = os.path.expanduser("~/.config/cosmic/com.system76.CosmicIdle/v1")
AUDIO_SINK_CMD = ["pactl", "get-default-sink"]   # pulseaudio-utils (works with PipeWire)

MAX_RECORD_SECONDS = 7 * 86400   # a hand-typed length beyond this is a typo
MIN_LOG_SECONDS = 60          # meditations shorter than this aren't logged
CHECKPOINT_EVERY = 10         # seconds between crash-recovery checkpoints
END_STRIKES = 3               # fallback if the end bell file is missing
STRIKE_GAP_MS = 4000
DIM_AFTER = 30                # seconds without input before the meditation view dims
AWAKE_CHECK_EVERY = 5         # seconds between re-checks of whether idle must be blocked
SLEEP_GAP = 10                # a suspend longer than this is reported
RESUME_WITHIN = 15 * 60       # a crash checkpoint younger than this can be carried on with
EXTRA_MAX = 4 * 3600          # extra time stops counting after this (left running by mistake)
PLAYER_START_TIMEOUT = 4      # seconds between checks that a bell has started playing
PLAYER_START_LIMIT = 12       # ... and how long a slow-to-wake sink gets in all
PLAYER_MAX_AGE = 90           # no bell is longer than this; a player still around is stuck

INT_SETTINGS = {"hours": (0, 99), "minutes": (0, 59), "interval_min": (0, 720),
                "warmup_sec": (0, 600)}
DEFAULTS = {
    "hours": 1, "minutes": 0, "infinite": False,
    "interval_min": 30, "start_bell": True, "volume": 0.8,
    "warmup_sec": 15,         # settle-in countdown before the opening bell; 0 = none
    "theme": "auto",          # auto | light | dark
}

# ---------------------------------------------------------------- palettes
# Contrast ratios (vs bg / surface) verified with the WCAG relative-luminance
# formula: text ≥13:1, muted ≥6:1, accent ≥5.4:1, on_accent/accent ≥6:1,
# border ≥3.2:1, bar ≥4.8:1, focus ≥5.4:1.
PALETTES = {
    "light": {  # "paper"
        "bg": "#f4f0e8", "surface": "#fbf9f4", "field": "#ffffff",
        "text": "#262622", "muted": "#5c5850", "accent": "#7a5c22",
        "on_accent": "#fffcf5", "border": "#8a8478", "track": "#e4ded2",
        "bar": "#4d7663", "danger": "#a3392a", "hover": "#efe9de",
    },
    "dark": {   # "ink"
        "bg": "#141615", "surface": "#1c1f1e", "field": "#242827",
        "text": "#ebe6dc", "muted": "#a9a59b", "accent": "#d8b878",
        "on_accent": "#17160f", "border": "#6f7773", "track": "#2c3130",
        "bar": "#86ad99", "danger": "#eba596", "hover": "#262a29",
    },
}

CSS_TEMPLATE = """
* {{ font-family: "Fira Sans", sans-serif; }}
window, dialog, messagedialog {{ background-color: {bg}; color: {text}; }}
window.background, dialog.background {{ background-color: {bg}; }}
label {{ color: {text}; }}
label.muted, .muted {{ color: {muted}; }}
label:disabled {{ color: {muted}; }}
window.dim label.clock, window.dim label.clock-sub {{ color: {muted}; }}
window.dim button.quiet {{ color: {muted}; }}
window.dim button.quiet label {{ color: {muted}; }}
*:focus {{ outline-color: {accent}; outline-style: solid;
           outline-width: 2px; outline-offset: 2px; }}

/* header */
headerbar {{ background: {bg}; border: none; box-shadow: none;
             min-height: 46px; padding: 0 8px; }}
headerbar .title {{ color: {text}; }}
headerbar button {{ min-width: 30px; min-height: 30px; }}
stackswitcher button {{ background: transparent; border: none;
             border-radius: 999px; padding: 4px 18px; color: {muted};
             box-shadow: none; font-weight: 500; }}
stackswitcher button:hover {{ background: {hover}; color: {text}; }}
stackswitcher button:checked {{ background: {surface}; color: {text};
             box-shadow: inset 0 0 0 1px {border}; }}

/* buttons */
button {{ background-image: none; background-color: transparent;
          color: {text}; border: 1px solid {border}; border-radius: 999px;
          box-shadow: none; text-shadow: none; -gtk-icon-shadow: none;
          padding: 6px 16px; font-weight: 500; }}
button:hover {{ background-color: {hover}; }}
button:active {{ background-color: {track}; }}
button:disabled {{ color: {muted}; border-color: {track}; }}
button:disabled label {{ color: {muted}; }}
button.flat, button.titlebutton {{ border-color: transparent; }}
button.flat:hover, button.titlebutton:hover {{ background-color: {hover}; }}
button.titlebutton image, button image {{ color: {text}; }}
button.primary {{ background-color: {accent}; border-color: {accent};
          padding: 12px 40px; font-weight: 600; font-size: 1.1em; }}
button.primary label {{ color: {on_accent}; }}
button.primary:hover {{ background-color: shade({accent}, 1.08); }}
button.primary:active {{ background-color: shade({accent}, 0.94); }}
button.quiet {{ padding: 12px 26px; font-size: 1.1em; }}
button.chip {{ padding: 4px 14px; font-size: 0.9em; }}
button.chip.inf {{ padding: 0 12px; font-size: 1.5em; font-weight: 400; }}
button.chip.inf:checked {{ background-color: {accent}; color: {on_accent};
          border-color: {accent}; }}
button.chip.inf:checked label {{ color: {on_accent}; }}
dialog button, messagedialog button {{ margin: 4px; }}

/* inputs */
entry, spinbutton {{ background-color: {field}; color: {text};
          border: 1px solid {border}; border-radius: 10px; box-shadow: none;
          caret-color: {text}; }}
entry:focus, spinbutton:focus {{ border-color: {accent};
          box-shadow: inset 0 0 0 1px {accent}; }}
spinbutton:disabled, entry:disabled {{ opacity: 0.45; }}
spinbutton entry {{ border: none; background: transparent; }}
spinbutton button {{ border: none; border-radius: 0; padding: 4px 8px;
          color: {text}; }}
entry selection, spinbutton selection, label selection {{
          background-color: {accent}; color: {on_accent}; }}
switch {{ background-color: {field}; border: 1px solid {border};
          border-radius: 999px; min-height: 22px; box-shadow: none;
          background-image: none; }}
switch:checked {{ background-color: {accent}; border-color: {accent}; }}
switch slider {{ background-color: {text}; border: none; border-radius: 999px;
          min-width: 20px; min-height: 20px; margin: 2px; box-shadow: none;
          background-image: none; }}
switch:checked slider {{ background-color: {on_accent}; }}
switch image {{ color: transparent; }}
scale trough {{ background-color: {border}; border: none; min-height: 4px;
          border-radius: 999px; }}
scale highlight {{ background-color: {accent}; border-radius: 999px; }}
scale slider {{ background-color: {accent}; border: 2px solid {surface};
          min-width: 18px; min-height: 18px; border-radius: 999px;
          box-shadow: none; background-image: none; }}

/* cards */
.card {{ background-color: {surface}; border: 1px solid {track};
         border-radius: 18px; padding: 18px 20px; }}
.card-title {{ color: {muted}; font-size: 0.85em; font-weight: 600; }}
.row-label {{ color: {text}; }}
.hint {{ color: {muted}; font-size: 0.9em; }}

/* timer */
.phase {{ color: {muted}; font-size: 0.85em; font-weight: 600; }}
.clock {{ font-size: 4.2em; font-weight: 300; color: {text};
          font-feature-settings: "tnum"; }}
.clock-sub {{ color: {muted}; font-size: 1.0em; }}
.status {{ color: {accent}; font-size: 0.95em; }}
.status.error {{ color: {danger}; }}
.summary {{ color: {muted}; font-size: 1.0em; }}

/* journal */
.stat-num {{ font-size: 1.7em; font-weight: 300; color: {text};
             font-feature-settings: "tnum"; }}
.stat-lbl {{ color: {muted}; font-size: 0.85em; }}
treeview.view {{ background-color: {surface}; color: {text}; }}
treeview.view:selected, treeview.view:selected:focus {{
             background-color: {accent}; color: {on_accent}; }}
treeview.view header button {{ background: {surface}; color: {muted};
             border: none; border-bottom: 1px solid {track};
             border-radius: 0; padding: 4px 8px; font-weight: 600; }}
scrolledwindow {{ border: none; background: transparent; }}
scrollbar, scrollbar trough {{ background: transparent; border: none; }}
scrollbar slider {{ background-color: {border}; min-width: 6px;
             min-height: 6px; border-radius: 999px; border: none; }}
tooltip, tooltip.background {{ background-color: {text}; color: {bg};
             border-radius: 8px; }}
tooltip label {{ color: {bg}; }}
separator {{ background-color: {track}; min-height: 1px; }}
decoration {{ border: none; box-shadow: 0 4px 16px rgba(0,0,0,0.35); }}
window.csd, window.csd.background {{ border: none; }}
"""


# ---------------------------------------------------------------- helpers

def now_clock():
    """Seconds on a clock that keeps counting through suspend (CLOCK_BOOTTIME),
    so bells stay on schedule after a laptop sleep (see _tick for how sleep is
    noticed and reported)."""
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def log(*args):
    """print() to stderr, but never fail: after the terminal the app was started
    from is closed, writing to it raises an error."""
    try:
        print(*args, file=sys.stderr)
    except (OSError, ValueError):
        pass


def report_exception():
    """Show an unexpected exception (and let the tests see it) without letting
    the reporting itself fail."""
    try:
        sys.excepthook(*sys.exc_info())
    except Exception:
        pass


def now_awake():
    """Seconds on a clock that stops during suspend (CLOCK_MONOTONIC). Comparing it
    with now_clock() separates real sleep from the app merely being busy."""
    return time.clock_gettime(time.CLOCK_MONOTONIC)

def fmt_hms(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def fmt_hm(seconds):
    minutes = int(round(seconds / 60))
    h, m = divmod(minutes, 60)
    if not h:
        return f"{m}m"
    return f"{h}h" if not m else f"{h}h {m:02d}m"


def fmt_hms_short(seconds):
    """'7m 30s', '5m', '30m 02s', '1h 05m 30s', '45s': exact, for lengths you
    choose to save."""
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if not h and not m:
        return f"{s}s"
    out = f"{h}h {m:02d}m" if h else f"{m}m"
    return out + (f" {s:02d}s" if s else "")


def fmt_dim(seconds):
    """Dimmed view: whole minutes, unmistakable at a glance ('45m', '1h 05m')."""
    m = max(0, int(seconds)) // 60
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


def fmt_words(seconds):
    """Spoken form for screen readers: '1 hour 5 minutes'."""
    minutes = int(round(seconds / 60))
    h, m = divmod(minutes, 60)
    parts = []
    if h:
        parts.append(f"{h} hour{'s' if h != 1 else ''}")
    if m or not h:
        parts.append(f"{m} minute{'s' if m != 1 else ''}")
    return " ".join(parts)


def fmt_words_up(seconds):
    """Like fmt_words but rounds up, for countdowns: 4:59 left is '5 minutes'."""
    return fmt_words(math.ceil(max(0, seconds) / 60) * 60)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _private_open(path, mode):
    """open() for our data files: created readable only by you."""
    flags = {"w": os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
             "a+b": os.O_RDWR | os.O_CREAT | os.O_APPEND}[mode]
    fd = os.open(path, flags, 0o600)
    if "b" in mode:
        return os.fdopen(fd, mode)
    # surrogateescape: lines that weren't UTF-8 are written back byte for byte
    return os.fdopen(fd, mode, encoding="utf-8", errors="surrogateescape")


def _fsync_dir(path):
    """Make a rename durable: fsync the folder that holds it."""
    try:
        fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def atomic_write_json(path, obj):
    tmp = path + ".tmp"
    with _private_open(tmp, "w") as f:
        json.dump(obj, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _fsync_dir(path)


def start_time(s):
    """A record's start as an aware datetime (old hand-written ones may lack an offset)."""
    t = dt.datetime.fromisoformat(s["start"])
    return t if t.tzinfo else t.astimezone()


def _valid_session(s):
    if not isinstance(s, dict) or not isinstance(s.get("start"), str):
        return None
    try:
        dt.datetime.fromisoformat(s["start"])
        s["seconds"] = min(MAX_RECORD_SECONDS, max(0, int(float(s.get("seconds", 0)))))
    except (TypeError, ValueError, OverflowError):
        return None
    try:   # hand-edited logs: keep the row, drop a planned length we can't read
        s["planned"] = None if s.get("planned") is None else \
            min(MAX_RECORD_SECONDS, max(0, int(float(s["planned"]))))
    except (TypeError, ValueError, OverflowError):
        s["planned"] = None
    return s


def load_sessions():
    """Returns (valid sessions sorted by start time, lines that didn't parse).

    Every entry remembers its place in the file ("_n") and its original text
    ("_line"), so rewriting the journal after a delete leaves hand-written and
    unreadable lines exactly as they were, where they were."""
    sessions, junk = [], []
    try:
        # a hand edit saved in another encoding mustn't stop the app: such bytes
        # are kept as they are (surrogateescape) and the line counts as unreadable
        with open(LOG_PATH, encoding="utf-8", errors="surrogateescape") as f:
            for n, line in enumerate(f):
                line = line.rstrip("\n")
                if not line.strip():
                    junk.append((n, line))     # kept, so a rewrite keeps the layout
                    continue
                try:
                    line.encode("utf-8")
                    s = _valid_session(json.loads(line))
                except (ValueError, RecursionError):
                    s = None
                if s is None:
                    junk.append((n, line))
                else:
                    s["_n"], s["_line"] = n, line
                    sessions.append(s)
    except OSError:
        pass
    sessions.sort(key=start_time)
    return sessions, junk


def append_session(rec):
    with _private_open(LOG_PATH, "a+b") as f:
        data = (json.dumps(rec) + "\n").encode()
        end = f.seek(0, os.SEEK_END)
        if end:
            f.seek(end - 1)
            if f.read(1) != b"\n":
                # a hand edit (or a half-written line) left no final newline:
                # without this the new record would be glued onto that line
                data = b"\n" + data
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def boot_id():
    """Changes at every boot: tells a crash in this session from an older one."""
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return None


def unique_bad_path(path):
    """path.bad, or path.2.bad, ...: an unreadable file is never overwritten."""
    candidate, n = path + ".bad", 1
    while os.path.exists(candidate):
        n += 1
        candidate = f"{path}.{n}.bad"
    return candidate


def unsaved_path(rec):
    """Where a record that couldn't be written to the journal waits for a retry."""
    return os.path.join(DATA_DIR, "unsaved-" + re.sub(r"[^0-9]", "", rec["start"]) + ".json")


def rewrite_sessions(sessions, junk=()):
    """Rewrite the journal with these entries, keeping file order and original text."""
    try:
        shutil.copyfile(LOG_PATH, LOG_PATH + ".bak")
        os.chmod(LOG_PATH + ".bak", 0o600)
    except OSError:
        pass
    entries = [(n, line) for n, line in junk]
    for s in sessions:
        line = s.get("_line") or json.dumps({k: v for k, v in s.items()
                                             if not k.startswith("_")})
        entries.append((s.get("_n", float("inf")), line))
    entries.sort(key=lambda e: e[0])
    tmp = LOG_PATH + ".tmp"
    with _private_open(tmp, "w") as f:
        for _, line in entries:
            f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, LOG_PATH)
    _fsync_dir(LOG_PATH)


def split_by_day(start, seconds):
    """Yield (date, seconds) for a meditation, splitting at local midnight
    (worked out in local time each day, so a daylight-saving change is right)."""
    t = start.astimezone()
    remaining = seconds
    while remaining > 0:
        midnight = dt.datetime.combine(t.date() + dt.timedelta(days=1), dt.time()).astimezone()
        chunk = min(remaining, (midnight - t).total_seconds())
        yield t.date(), chunk
        remaining -= chunk
        t = midnight


def hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def add_class(widget, *names):
    ctx = widget.get_style_context()
    for n in names:
        ctx.add_class(n)
    return widget


def label(text="", *classes, xalign=None):
    l = Gtk.Label(label=text)
    add_class(l, *classes)
    if xalign is not None:
        l.set_xalign(xalign)
    return l


def set_a11y(widget, name=None, description=None):
    acc = widget.get_accessible()
    if acc is not None:
        if name:
            acc.set_name(name)
        if description:
            acc.set_description(description)


# ---------------------------------------------------------------- audio

class Bell:
    """Plays the bells; each strike gets its own player so strikes can overlap."""

    def __init__(self, path, start_path=None, end_path=None, on_error=None):
        self.missing = [os.path.basename(x) for x in (path, start_path, end_path)
                        if x and not os.path.exists(x)]
        self.on_error = on_error
        self.preview = None
        self.uri = Gst.filename_to_uri(path)
        self.start_uri = (Gst.filename_to_uri(start_path)
                          if start_path and os.path.exists(start_path) else self.uri)
        self.end_uri = (Gst.filename_to_uri(end_path)
                        if end_path and os.path.exists(end_path) else None)
        if not os.path.exists(path) and start_path and os.path.exists(start_path):
            # no interval bell installed: use the opening bell for intervals too
            self.uri = self.start_uri
            self.missing.remove(os.path.basename(path))
        self.volume = 0.8
        self.players = []

    def strike(self, times=1):
        for i in range(times):
            GLib.timeout_add(i * STRIKE_GAP_MS, lambda: self._play_once(self.uri) and False)

    def strike_start(self):
        self._play_once(self.start_uri)

    def strike_end(self):
        if self.end_uri:
            self._play_once(self.end_uri)
        else:
            self.strike(END_STRIKES)

    def play_preview(self, which):
        """Preview a bell, cutting off any preview still ringing."""
        if self.preview is not None:
            self._cleanup(self.preview)
        uri = {"start": self.start_uri, "interval": self.uri,
               "end": self.end_uri or self.uri}[which]
        self.preview = self._play_once(uri)

    def _play_once(self, uri, retry=True):
        p = Gst.ElementFactory.make("playbin", None)
        if p is None:
            self._report("GStreamer playbin is unavailable")
            return None
        p.set_property("uri", uri)
        p.set_property("volume", self.volume)
        bus = p.get_bus()
        bus.add_signal_watch()
        p._hid = bus.connect("message", self._on_msg, p, uri, retry)
        self.players.append(p)
        p._born = time.monotonic()
        p.set_state(Gst.State.PLAYING)
        # a player can also stall without an error (e.g. a half-gone Bluetooth sink)
        GLib.timeout_add_seconds(PLAYER_START_TIMEOUT, self._check_started, p, uri, retry)
        return p

    def _check_started(self, p, uri, retry, waited=0):
        if p not in self.players:
            return False
        ret, state, pending = p.get_state(0)
        waited += PLAYER_START_TIMEOUT
        if (state != Gst.State.PLAYING and ret == Gst.StateChangeReturn.ASYNC
                and pending == Gst.State.PLAYING and waited < PLAYER_START_LIMIT):
            # still starting (a Bluetooth or dock sink waking up): give it longer
            # rather than cut it off and play it twice
            GLib.timeout_add_seconds(PLAYER_START_TIMEOUT, self._check_started,
                                     p, uri, retry, waited)
            return False
        if state != Gst.State.PLAYING:
            self._failed(p, uri, retry, "it didn’t start playing")
        else:
            GLib.timeout_add_seconds(PLAYER_MAX_AGE, self._check_finished, p)
        return False

    def _check_finished(self, p):
        if p in self.players:   # never reported its end: let it go
            self._cleanup(p)
        return False

    def _failed(self, p, uri, retry, err):
        log("bell error:", err)
        self._cleanup(p)
        if retry:   # e.g. audio device changed (dock/Bluetooth): try a fresh player
            GLib.timeout_add(300, lambda: self._play_once(uri, retry=False) and False)
        else:
            self._report(f"A bell couldn’t play ({err})")

    def _cleanup(self, p):
        bus = p.get_bus()
        if getattr(p, "_hid", None):
            bus.disconnect(p._hid)   # break the p -> bus -> closure -> p cycle
            p._hid = None
            bus.remove_signal_watch()
        p.set_state(Gst.State.NULL)
        if p in self.players:
            self.players.remove(p)
        if p is self.preview:
            self.preview = None

    def _on_msg(self, bus, msg, p, uri, retry):
        if msg.type == Gst.MessageType.EOS:
            self._cleanup(p)
        elif msg.type == Gst.MessageType.ERROR:
            self._failed(p, uri, retry, msg.parse_error()[0].message)

    def _report(self, text):
        if self.on_error:
            self.on_error(text)


# ---------------------------------------------------------------- ring

class Ring(Gtk.DrawingArea):
    """Thin progress ring with small marks where interval bells fall.

    Purely decorative: the same information is in the labels overlaid on it,
    which is what assistive technology reads.
    """

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.connect("draw", self.on_draw)

    def on_draw(self, widget, cr):
        pal = self.app.palette
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        cx, cy = w / 2, h / 2
        r = min(w, h) / 2 - 12
        start = -math.pi / 2

        frac, marks = self.app.ring_state()
        dim = self.app.dimmed
        arc_color = pal["muted"] if dim else pal["accent"]

        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_width(3)
        # with nothing to show (until-I-stop, no interval) keep the circle visible
        if self.app.running:
            target, interval = self.app.target, self.app.interval
        else:
            target, interval = self.app.planned_settings()
        quiet = target is None and not interval
        cr.set_source_rgb(*hex_rgb(pal["border"] if quiet and not dim else pal["track"]))
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.stroke()

        if frac > 0:
            cr.set_line_width(5)
            cr.set_source_rgb(*hex_rgb(arc_color))
            cr.arc(cx, cy, r, start, start + 2 * math.pi * min(frac, 1.0))
            cr.stroke()

        for m in marks:
            a = start + 2 * math.pi * m
            x, y = cx + r * math.cos(a), cy + r * math.sin(a)
            reached = frac >= m
            cr.set_source_rgb(*hex_rgb(arc_color if reached else pal["border"]))
            cr.arc(x, y, 4.5, 0, 2 * math.pi)
            cr.fill()
            if not reached:
                cr.set_source_rgb(*hex_rgb(pal["bg"]))
                cr.arc(x, y, 2, 0, 2 * math.pi)
                cr.fill()


# ---------------------------------------------------------------- chart

class DayChart(Gtk.DrawingArea):
    """Bar chart of time per day for the last N days. A text summary is exposed
    as the accessible description."""

    def __init__(self, app, days=14):
        super().__init__()
        self.app = app
        self.days = days
        self.values = []
        self.set_size_request(-1, 150)
        self.set_can_focus(True)   # so screen readers reach its text summary
        self.connect("draw", self.on_draw)

    def set_data(self, values):
        self.values = values
        total = sum(v for _, v in values)
        active = sum(1 for _, v in values if v)
        set_a11y(self, "Time meditated per day, last 14 days",
                 f"{fmt_words(total)} across {active} day{'s' if active != 1 else ''}. " +
                 "; ".join(f"{d.strftime('%A %B %-d')}: {fmt_words(v)}"
                           for d, v in values if v))
        self.queue_draw()

    def on_draw(self, widget, cr):
        pal = self.app.palette
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        if not self.values:
            return
        layout_font = Pango.FontDescription("Fira Sans 9")
        top, bottom = 20, 22
        ch = h - top - bottom
        n = len(self.values)
        slot = w / n
        bw = max(4, min(18, slot * 0.5))
        peak = max(max(v for _, v in self.values), 3600)
        today = dt.date.today()

        bold_font = layout_font.copy()
        bold_font.set_weight(Pango.Weight.BOLD)

        def text(s, x, y, color, bold=False):
            lay = widget.create_pango_layout(s)
            lay.set_font_description(bold_font if bold else layout_font)
            tw, th = lay.get_pixel_size()
            cr.set_source_rgb(*hex_rgb(color))
            cr.move_to(x - tw / 2, y)
            PangoCairo.show_layout(cr, lay)

        # baseline
        cr.set_source_rgb(*hex_rgb(pal["track"]))
        cr.set_line_width(1)
        cr.move_to(0, top + ch + 0.5)
        cr.line_to(w, top + ch + 0.5)
        cr.stroke()

        for i, (d, v) in enumerate(self.values):
            cx = i * slot + slot / 2
            x = cx - bw / 2
            bh = (v / peak) * ch if v else 0
            if bh:
                bh = max(bh, 3)
                color = pal["accent"] if d == today else pal["bar"]
                cr.set_source_rgb(*hex_rgb(color))
                rad = min(bw / 2, 5, bh / 2)
                y0 = top + ch - bh
                # rounded top
                cr.new_sub_path()
                cr.arc(x + rad, y0 + rad, rad, math.pi, 1.5 * math.pi)
                cr.arc(x + bw - rad, y0 + rad, rad, 1.5 * math.pi, 2 * math.pi)
                cr.line_to(x + bw, top + ch)
                cr.line_to(x, top + ch)
                cr.close_path()
                cr.fill()
                if d == today or v == max(val for _, val in self.values):
                    hrs = v / 3600
                    vl = f"{hrs:.1f}h" if hrs >= 1 else f"{int(v // 60)}m"
                    text(vl, cx, y0 - 16, pal["text"])
            # today is marked by weight as well as colour
            text(d.strftime("%-d"), cx, h - 17,
                 pal["accent"] if d == today else pal["muted"], bold=d == today)

        if widget.has_focus():
            cr.set_source_rgb(*hex_rgb(pal["accent"]))
            cr.set_line_width(2)
            cr.rectangle(1, 1, w - 2, h - 2)
            cr.stroke()


# ---------------------------------------------------------------- app

class BellTimer(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="io.github.adinotices.MeditationBell")
        self.win = None
        self.running = False
        self.warming = False        # warm-up countdown before the meditation starts
        self.overtime = False       # planned length reached; extra time is counting
        self.asking = False         # the End dialog is open
        self._ask_asleep = 0.0
        self.extra_capped = False
        self.sleep_fd = None        # logind sleep lock, released by closing it
        self._sleep_pending = False
        self._audio_proc = None
        self._lock_fd = None        # one app per journal
        self._dialogs = []          # open modal dialogs (closed on logout)
        self._ask_wall = None       # wall time / bells when the End dialog opened
        self._ask_bells = 0
        self._today = None
        self._last_awake = None
        self._stats_min = None
        self.warm_total = 0
        self.warm_until = 0.0
        self.warm_id = None
        self.paused = False
        self.accum = 0.0           # active seconds before current segment
        self.seg_start = None      # monotonic start of current running segment
        self.target = None         # seconds, or None for infinite
        self.interval = 0          # seconds, 0 = none
        self.next_k = 1
        self.bells_rung = 0
        self.start_wall = None
        self.last_checkpoint = 0.0
        self.dimmed = False
        self.last_input = 0.0
        self.started_at = 0.0
        self._last_shown = None
        self._last_a11y_min = None
        self._vol_save_id = None
        self.inhibit_cookie = 0     # GTK (Wayland idle-inhibit on our window)
        self.ss_cookie = None       # org.freedesktop.ScreenSaver (cosmic-idle)
        self.idle_blocked = False
        self.audio_on_display = False
        self._next_awake_check = 0.0
        self._next_audio_check = 0.0
        self._last_tick = None
        self.asleep = 0.0           # seconds the laptop slept during this meditation
        self.tick_id = None
        self.css_provider = None
        self.palette = PALETTES["light"]
        self._sessions = []
        self._junk = []

    # ------------------------------------------------------------ theme
    def system_is_dark(self):
        try:
            with open(COSMIC_DARK_PATH) as f:
                return f.read().strip() == "true"
        except OSError:
            pass
        if self._iface_settings is not None:
            return self._iface_settings.get_string("color-scheme") == "prefer-dark"
        return False

    def apply_theme(self, *_):
        mode = self.cfg.get("theme", "auto")
        dark = self.system_is_dark() if mode == "auto" else mode == "dark"
        self.palette = PALETTES["dark" if dark else "light"]
        screen = Gdk.Screen.get_default()
        if self.css_provider:
            Gtk.StyleContext.remove_provider_for_screen(screen, self.css_provider)
        self.css_provider = Gtk.CssProvider()
        self.css_provider.load_from_data(CSS_TEMPLATE.format(**self.palette).encode())
        Gtk.StyleContext.add_provider_for_screen(
            screen, self.css_provider, Gtk.STYLE_PROVIDER_PRIORITY_USER)
        Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", dark)
        if self.win:
            self.ring.queue_draw()
            self.chart.queue_draw()
            self.update_theme_button()

    def watch_system_theme(self):
        self._iface_settings = None
        src = Gio.SettingsSchemaSource.get_default()
        if src and src.lookup("org.gnome.desktop.interface", True):
            s = Gio.Settings.new("org.gnome.desktop.interface")
            if s.get_property("settings-schema").has_key("color-scheme"):
                self._iface_settings = s
                s.connect("changed::color-scheme", self.apply_theme)
        self._cosmic_monitor = None
        try:
            f = Gio.File.new_for_path(COSMIC_DARK_PATH)
            self._cosmic_monitor = f.monitor_file(Gio.FileMonitorFlags.NONE, None)
            self._cosmic_monitor.connect("changed", lambda *a: GLib.idle_add(self._idle_theme))
        except GLib.Error:
            pass

    def _idle_theme(self):
        self.apply_theme()
        return False

    def cycle_theme(self, _btn):
        order = ["auto", "light", "dark"]
        self.cfg["theme"] = order[(order.index(self.cfg.get("theme", "auto")) + 1) % 3]
        self.save_config()
        self.apply_theme()

    def update_theme_button(self):
        mode = self.cfg.get("theme", "auto")
        icon, tip = {
            "auto": ("display-brightness-symbolic", "Appearance: follows system"),
            "light": ("weather-clear-symbolic", "Appearance: light"),
            "dark": ("weather-clear-night-symbolic", "Appearance: dark"),
        }[mode]
        nxt = {"auto": "light", "light": "dark", "dark": "follow system"}[mode]
        self.theme_btn.set_image(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON))
        self.theme_btn.set_tooltip_text(f"{tip} · next: {nxt}")
        set_a11y(self.theme_btn, tip, f"Activate to switch to {nxt}")

    # ------------------------------------------------------------ setup
    def do_activate(self):
        if self.win:
            self.win.present()
            return
        os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
        for path in (DATA_DIR, LOG_PATH, LOG_PATH + ".bak", CONFIG_PATH, CURRENT_PATH):
            try:   # the journal is private
                os.chmod(path, 0o700 if path == DATA_DIR else 0o600)
            except OSError:
                pass
        if not self.take_lock():
            self.already_running()
            return
        cfg = load_json(CONFIG_PATH, {})
        self.cfg = {**DEFAULTS, **(cfg if isinstance(cfg, dict) else {})}
        for key, (lo, hi) in INT_SETTINGS.items():   # a hand-edited config can't break startup
            try:
                self.cfg[key] = min(hi, max(lo, int(float(self.cfg[key]))))
            except (TypeError, ValueError, OverflowError):
                self.cfg[key] = DEFAULTS[key]
        for key in ("infinite", "start_bell"):
            if not isinstance(self.cfg[key], bool):
                self.cfg[key] = DEFAULTS[key]
        if self.cfg.get("theme") not in ("auto", "light", "dark"):
            self.cfg["theme"] = "auto"
        try:
            self.cfg["volume"] = min(1.0, max(0.0, float(self.cfg["volume"])))
        except (TypeError, ValueError):
            self.cfg["volume"] = DEFAULTS["volume"]
        self.bell = Bell(BELL_PATH, START_BELL_PATH, END_BELL_PATH,
                         on_error=self.bell_error)
        self.bell.volume = self.cfg["volume"]
        self.watch_system_theme()
        self.apply_theme()

        self.win = Gtk.ApplicationWindow(application=self, title="Meditation Bell")
        self.win.set_default_size(480, 800)
        try:
            self.win.set_icon_from_file(os.path.join(APP_DIR, "logo.png"))
        except GLib.Error:
            pass
        self.win.connect("delete-event", self.on_close)
        self.win.add_events(Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.KEY_PRESS_MASK
                            | Gdk.EventMask.BUTTON_PRESS_MASK)
        for sig in ("motion-notify-event", "key-press-event", "button-press-event"):
            self.win.connect(sig, self.on_user_input)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(180)
        self.stack.add_titled(self.build_timer_page(), "meditate", "Meditate")
        self.stack.add_titled(self.build_journal_page(), "journal", "Journal")
        self.stack.connect("notify::visible-child", lambda *_: self.refresh_stats())

        hb = Gtk.HeaderBar()
        hb.set_show_close_button(True)
        sw = Gtk.StackSwitcher(stack=self.stack)
        hb.set_custom_title(sw)
        self.theme_btn = add_class(Gtk.Button(), "flat")
        self.theme_btn.connect("clicked", self.cycle_theme)
        hb.pack_start(self.theme_btn)
        self.update_theme_button()
        self.win.set_titlebar(hb)
        self.win.add(self.stack)

        resumable = None
        try:
            resumable = self.resumable_checkpoint()
            if resumable is None:
                self.recover_pending()
        except Exception:
            report_exception()
        self._today = dt.date.today()
        GLib.timeout_add_seconds(60, self._watch_day)
        self.watch_shutdown()
        if self.bell.missing:
            self.set_status("Missing bell file: " + ", ".join(self.bell.missing), error=True)
        try:
            self.refresh_stats()
        except Exception:
            report_exception()   # a damaged journal mustn't keep the window from opening
        self.update_display()
        self.win.show_all()
        self.update_visibility()
        self.btn_start.grab_focus()
        if resumable is not None:
            GLib.idle_add(lambda: self.offer_resume(resumable) and False)

    def take_lock(self):
        """Only one copy of the app may use the journal: two would recover each
        other's meditation in progress and count it twice."""
        try:
            fd = os.open(os.path.join(DATA_DIR, ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return True   # can't lock (odd filesystem): carry on as before
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._lock_fd = fd
        return True

    def release_lock(self):
        if self._lock_fd is not None:
            os.close(self._lock_fd)   # closing releases the lock
            self._lock_fd = None

    def already_running(self):
        dlg = Gtk.MessageDialog(modal=True, message_type=Gtk.MessageType.INFO,
                                buttons=Gtk.ButtonsType.OK,
                                text="Meditation Bell is already open")
        dlg.format_secondary_text("Another copy is using your journal. "
                                  "Switch to that window instead.")
        self.run_dialog(dlg)
        dlg.destroy()

    def close_dialogs(self):
        # a dialog's run() has its own main loop, which quit() doesn't end
        for dlg in list(self._dialogs):
            dlg.response(Gtk.ResponseType.DELETE_EVENT)

    def run_dialog(self, dlg):
        """dlg.run(), remembering the dialog so a logout can close it."""
        self._dialogs.append(dlg)
        try:
            return dlg.run()
        finally:
            self._dialogs.remove(dlg)

    def _watch_day(self):
        # "today" moves on at midnight even if nothing else happens
        if dt.date.today() != self._today:
            self._today = dt.date.today()
            self.refresh_stats()
        return True

    def watch_shutdown(self):
        """Save before the system powers off, in case the desktop closes the app
        before a SIGTERM arrives."""
        try:
            self._system_bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            self._system_bus.signal_subscribe(
                "org.freedesktop.login1", "org.freedesktop.login1.Manager",
                "PrepareForShutdown", "/org/freedesktop/login1", None,
                Gio.DBusSignalFlags.NONE, self.on_prepare_shutdown)
        except GLib.Error:
            pass

    def on_prepare_shutdown(self, *args):
        params = next((a for a in args if isinstance(a, GLib.Variant)), None)
        if params is None or params.unpack()[0]:   # True: about to shut down
            self.finish("auto")
            self.close_dialogs()

    def build_timer_page(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.timer_outer = outer
        outer.set_margin_start(28)
        outer.set_margin_end(28)
        outer.set_margin_top(0)
        outer.set_margin_bottom(12)

        # ring + centred labels
        self.ring = Ring(self)
        self.ring.set_size_request(272, 272)
        center = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        center.set_valign(Gtk.Align.CENTER)
        center.set_halign(Gtk.Align.CENTER)
        self.phase = label("READY", "phase")
        self.clock = label("1:00:00", "clock")
        self.clock_sub = label("", "clock-sub")
        center.pack_start(self.phase, False, False, 0)
        center.pack_start(self.clock, False, False, 0)
        center.pack_start(self.clock_sub, False, False, 0)
        ov = Gtk.Overlay()
        ov.add(self.ring)
        ov.add_overlay(center)
        ov.set_halign(Gtk.Align.CENTER)
        outer.pack_start(ov, False, False, 0)

        self.status = label("", "status")
        self.status.set_line_wrap(True)
        self.status.set_justify(Gtk.Justification.CENTER)
        outer.pack_start(self.status, False, False, 6)

        # settings card (hidden during a meditation)
        card = add_class(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0), "card")
        grid = Gtk.Grid(column_spacing=12, row_spacing=12)
        grid.set_hexpand(True)
        card.pack_start(grid, False, False, 0)

        def row_label(text, widget, r):
            l = label(text, "row-label", xalign=0)
            l.set_use_underline(True)
            l.set_mnemonic_widget(widget)
            l.set_hexpand(True)
            grid.attach(l, 0, r, 1, 1)
            return l

        # duration
        self.sp_hours = self._spin(0, 99, self.cfg["hours"], "Hours")
        self.sp_mins = self._spin(0, 59, self.cfg["minutes"], "Minutes")
        self.dur_box = Gtk.Box(spacing=6)
        self.dur_box.pack_start(self.sp_hours, False, False, 0)
        self.dur_box.pack_start(label("h", "muted"), False, False, 0)
        self.dur_box.pack_start(self.sp_mins, False, False, 4)
        self.dur_box.pack_start(label("m", "muted"), False, False, 0)
        self.len_label = row_label("_Length", self.sp_hours, 0)
        # ∞ = no set length: keep going (with interval bells) until you stop
        self.sw_inf = add_class(Gtk.ToggleButton(label="∞"), "chip", "inf")
        self.sw_inf.set_active(self.cfg["infinite"])
        self.sw_inf.set_tooltip_text("No set length: meditate until you stop. "
                                     "Interval bells keep ringing.")
        set_a11y(self.sw_inf, "Infinite length, until you stop")
        self.sw_inf.connect("notify::active", self.on_settings_changed)
        len_box = Gtk.Box(spacing=10)
        len_box.pack_start(self.dur_box, False, False, 0)
        len_box.pack_start(self.sw_inf, False, False, 0)
        grid.attach(len_box, 1, 0, 1, 1)

        self.sp_int = self._spin(0, 720, self.cfg["interval_min"], "Interval bell, minutes")
        ib = Gtk.Box(spacing=6)
        ib.pack_start(self.sp_int, False, False, 0)
        ib.pack_start(label("min", "muted"), False, False, 0)
        row_label("_Interval bell", self.sp_int, 1)
        grid.attach(ib, 1, 1, 1, 1)
        hint = label("Rings to mark a posture change. Set 0 for none.", "hint", xalign=0)
        hint.set_line_wrap(True)
        grid.attach(hint, 0, 2, 2, 1)

        self.sw_start = self._switch(self.cfg["start_bell"])
        row_label("_Opening bell", self.sw_start, 3)
        grid.attach(self.sw_start, 1, 3, 1, 1)

        self.sp_warm = self._spin(0, 600, self.cfg["warmup_sec"], "Warm up, seconds")
        self.sp_warm.set_increments(5, 30)
        wb = Gtk.Box(spacing=6)
        wb.pack_start(self.sp_warm, False, False, 0)
        wb.pack_start(label("sec", "muted"), False, False, 0)
        row_label("_Warm up", self.sp_warm, 4)
        grid.attach(wb, 1, 4, 1, 1)
        self.sp_warm.set_tooltip_text("Seconds to settle before the meditation starts. Set 0 for none.")
        set_a11y(self.sp_warm, "Warm up, seconds",
                 "Time to settle before the meditation starts. Set 0 for none.")

        grid.attach(Gtk.Separator(), 0, 5, 2, 1)

        self.vol = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 0.05)
        self.vol.set_value(self.cfg["volume"])
        self.vol.set_draw_value(False)
        self.vol.set_size_request(170, -1)
        self.vol.connect("value-changed", self.on_volume)
        set_a11y(self.vol, "Bell volume")
        row_label("_Volume", self.vol, 6)
        grid.attach(self.vol, 1, 6, 1, 1)

        pv = Gtk.Box(spacing=6)
        for text, which, a11y in (("Opening", "start", "Play opening bell"),
                                  ("Interval", "interval", "Play interval bell"),
                                  ("Closing", "end", "Play closing bell")):
            fn = (lambda w: lambda *_: self.bell.play_preview(w))(which)
            b = add_class(Gtk.Button(label=text), "chip")
            b.connect("clicked", fn)
            set_a11y(b, a11y)
            pv.pack_start(b, False, False, 0)
        pv.set_halign(Gtk.Align.END)
        grid.attach(label("Preview", "row-label", xalign=0), 0, 7, 1, 1)
        grid.attach(pv, 1, 7, 1, 1)

        for w in (len_box, ib, self.sw_start, wb, self.vol):
            w.set_halign(Gtk.Align.END)
            w.set_valign(Gtk.Align.CENTER)

        self.settings_rev = Gtk.Revealer()
        self.settings_rev.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.settings_rev.set_transition_duration(220)
        self.settings_rev.add(card)
        self.settings_rev.set_reveal_child(True)
        outer.pack_start(self.settings_rev, False, False, 4)

        # running summary (shown during a meditation)
        self.summary = label("", "summary")
        outer.pack_start(self.summary, False, False, 12)

        for sb in (self.sp_hours, self.sp_mins, self.sp_int):
            sb.connect("value-changed", self.on_settings_changed)

        # controls
        bb = Gtk.Box(spacing=12)
        bb.set_halign(Gtk.Align.CENTER)
        self.btn_start = add_class(Gtk.Button.new_with_mnemonic("_Begin"), "primary")
        self.btn_start.connect("clicked", self.on_start_pause)
        self.btn_stop = add_class(Gtk.Button.new_with_mnemonic("_End meditation"), "quiet")
        # a click mustn't leave focus on End or Pause, where a stray Space/Enter
        # would act on it with your eyes closed
        self.btn_start.set_focus_on_click(False)
        self.btn_stop.set_focus_on_click(False)
        self.btn_stop.connect("clicked", self.on_stop_clicked)
        bb.pack_start(self.btn_start, False, False, 0)
        bb.pack_start(self.btn_stop, False, False, 0)
        bb.set_margin_top(10)
        bb.set_margin_bottom(22)

        # settings scroll if the window is short; controls stay pinned below
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.add(outer)
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        page.pack_start(scroller, True, True, 0)
        page.pack_start(bb, False, False, 0)
        return page

    def build_journal_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_margin_start(24)
        box.set_margin_end(24)
        box.set_margin_top(12)
        box.set_margin_bottom(24)

        g = Gtk.Grid(column_spacing=10, row_spacing=10, column_homogeneous=True)
        self.stat_labels = {}
        keys = [("today", "Today"), ("week", "Last 7 days"), ("month", "Last 30 days"),
                ("total", "All time"), ("streak", "Day streak"), ("longest", "Longest")]
        for i, (k, title) in enumerate(keys):
            tile = add_class(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2), "card")
            num = label("–", "stat-num", xalign=0)
            tile.pack_start(num, False, False, 0)
            tile.pack_start(label(title, "stat-lbl", xalign=0), False, False, 0)
            g.attach(tile, i % 3, i // 3, 1, 1)
            self.stat_labels[k] = (num, title)
        box.pack_start(g, False, False, 0)

        cc = add_class(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10), "card")
        cc.pack_start(label("LAST 14 DAYS", "card-title", xalign=0), False, False, 0)
        self.chart = DayChart(self, 14)
        cc.pack_start(self.chart, False, False, 0)
        box.pack_start(cc, False, False, 0)

        lc = add_class(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10), "card")
        lc.pack_start(label("MEDITATIONS", "card-title", xalign=0), False, False, 0)
        self.store = Gtk.ListStore(str, str, str, str, int)  # when, length, planned, bells, idx
        tv = Gtk.TreeView(model=self.store)
        tv.set_headers_visible(True)
        for i, title in enumerate(["Started", "Length", "Planned", "Bells"]):
            cell = Gtk.CellRendererText()
            cell.set_padding(6, 6)
            col = Gtk.TreeViewColumn(title, cell, text=i)
            col.set_expand(i == 0)
            tv.append_column(col)
        set_a11y(tv, "Logged meditations", "Press Delete to remove the selected meditation")
        tv.connect("key-press-event",
                   lambda w, e: e.keyval == Gdk.KEY_Delete and (self.on_delete_selected(None) or True))
        self.tree = tv
        sw = Gtk.ScrolledWindow()
        sw.set_vexpand(True)
        sw.set_min_content_height(160)
        sw.add(tv)
        lc.pack_start(sw, True, True, 0)

        bb = Gtk.Box(spacing=8)
        add = Gtk.Button.new_with_mnemonic("_Add a meditation…")
        add.connect("clicked", self.on_add_manual)
        rm = add_class(Gtk.Button.new_with_mnemonic("_Delete selected"), "flat")
        rm.connect("clicked", self.on_delete_selected)
        bb.pack_start(add, False, False, 0)
        bb.pack_end(rm, False, False, 0)
        lc.pack_start(bb, False, False, 0)
        box.pack_start(lc, True, True, 0)
        return box

    def _spin(self, lo, hi, val, a11y_name):
        s = Gtk.SpinButton.new_with_range(lo, hi, 1)
        s.set_value(val)
        s.set_numeric(True)
        s.set_width_chars(3)
        s.set_alignment(0.5)
        set_a11y(s, a11y_name)
        return s

    def _switch(self, active):
        s = Gtk.Switch()
        s.set_active(active)
        return s

    # ------------------------------------------------------------ settings
    def on_settings_changed(self, *_):
        if not self.running:
            self.dur_box.set_sensitive(not self.sw_inf.get_active())
            self.update_display()

    def on_volume(self, scale):
        self.bell.volume = scale.get_value()
        if self._vol_save_id:
            GLib.source_remove(self._vol_save_id)

        def save():
            self._vol_save_id = None
            self.save_config()
            return False
        self._vol_save_id = GLib.timeout_add(500, save)

    def save_config(self):
        if hasattr(self, "sp_hours"):
            self.cfg.update({
                "hours": int(self.sp_hours.get_value()),
                "minutes": int(self.sp_mins.get_value()),
                "infinite": self.sw_inf.get_active(),
                "interval_min": int(self.sp_int.get_value()),
                "start_bell": self.sw_start.get_active(),
                "warmup_sec": int(self.sp_warm.get_value()),
                "volume": round(self.vol.get_value(), 2),
            })
        try:
            atomic_write_json(CONFIG_PATH, self.cfg)
        except OSError as e:
            log("config save failed:", e)

    def planned_settings(self):
        target = None if self.sw_inf.get_active() else \
            int(self.sp_hours.get_value()) * 3600 + int(self.sp_mins.get_value()) * 60
        return target, int(self.sp_int.get_value()) * 60

    # ------------------------------------------------------------ timing
    def elapsed(self):
        if self.running and not self.paused and self.seg_start is not None:
            return self.accum + (now_clock() - self.seg_start)
        return self.accum

    def ring_state(self):
        """(progress fraction 0..1, list of bell-mark fractions)."""
        if self.warming:
            left = max(0.0, self.warm_until - now_clock())
            return 1 - left / self.warm_total, []
        if self.running:
            target, interval, el = self.target, self.interval, self.elapsed()
            if self.overtime:
                return 1.0, []
        else:
            (target, interval), el = self.planned_settings(), 0
        if target:
            marks = []
            if interval and target // interval <= 24:   # more marks just reads as noise
                marks = [k * interval / target for k in range(1, int(target // interval) + 1)
                         if k * interval < target]
            return el / target, marks
        if interval:
            return (el % interval) / interval, []
        return 0.0, []

    def on_user_input(self, *_):
        self.last_input = now_clock()
        if self.dimmed:
            self.set_dimmed(False)
        return False

    def set_dimmed(self, dim):
        """Low-attention view during a meditation: no ticking seconds, muted ring and text."""
        self.dimmed = dim
        ctx = self.win.get_style_context()
        (ctx.add_class if dim else ctx.remove_class)("dim")
        self._last_shown = None
        self.update_display()

    def set_status(self, text, error=False, prefix="Error: "):
        """error=True uses the danger colour plus a text prefix, so colour is
        never the only cue."""
        ctx = self.status.get_style_context()
        (ctx.add_class if error else ctx.remove_class)("error")
        self.status.set_text((prefix + text) if error and text else text)
        if error and text and prefix.startswith("Error"):
            # a label change isn't announced by screen readers; a notification is
            self.notify("status-error", prefix + text)

    def notify(self, nid, text):
        try:
            n = Gio.Notification.new("Meditation Bell")
            n.set_body(text)
            self.send_notification(nid, n)
        except Exception:
            pass

    def bell_error(self, text):
        self.set_status(text, error=True)   # also sends a notification

    def on_start_pause(self, _btn):
        now = now_clock()
        if (self.running or self.warming) and now - self.started_at < 1.5:
            return   # swallow the second click of a double-click on Begin
        if self.warming:
            self.end_warmup(begin=True)   # "Start now"
        elif not self.running:
            self.start_session()
        elif self.paused:
            self.paused = False
            self.seg_start = now
            self.set_status("")
            self.win.set_focus(None)
        else:
            self.accum = self.elapsed()
            self.paused = True
            self.seg_start = None
            self.checkpoint()
        self._last_shown = None
        self.update_display()
        self.update_visibility()

    def start_session(self):
        for sb in (self.sp_hours, self.sp_mins, self.sp_int, self.sp_warm):
            sb.update()        # take in a value typed but not yet confirmed
        self.save_config()
        self.target, self.interval = self.planned_settings()
        if self.target is not None and self.target <= 0:
            self.set_status("Set a length, or choose ∞ to meditate until you stop.", error=True)
            self.beep()      # with eyes closed, a refused Begin mustn't pass unnoticed
            return
        if self.vol.get_value() < 0.05 and not self.confirm(
                "Start with the bells silent?",
                "The volume is at zero, so no bell will ring.",
                "Cancel", "Start _silently"):
            self.set_status("Volume is at zero, so the bells would be silent.", error=True)
            return
        warm = int(self.sp_warm.get_value())
        if warm > 0:
            self.warming = True
            self.warm_total = warm
            self.started_at = now_clock()
            self.warm_until = self.started_at + warm
            self.set_status("")
            self.win.set_focus(None)
            self.warm_id = GLib.timeout_add(200, self.warm_tick)
            return
        self.begin_meditation()

    def warm_tick(self):
        if not self.warming:
            self.warm_id = None
            return False
        if now_clock() >= self.warm_until:
            self.warm_id = None
            try:
                self.end_warmup(begin=True)
            except Exception:
                report_exception()
            return False
        self.update_display()
        return True

    def end_warmup(self, begin):
        """Leave the warm-up: start the meditation, or (begin=False) go back to ready."""
        if not self.warming:
            return
        self.warming = False
        if self.warm_id:
            GLib.source_remove(self.warm_id)
            self.warm_id = None
        self._last_shown = None
        if begin:
            self.begin_meditation()
        self.update_display()
        self.update_visibility()

    def begin_meditation(self, fresh=True):
        self.running = True
        # the timer comes first: nothing below may stop the bells from ringing
        if self.tick_id is None:
            self.tick_id = GLib.timeout_add(250, self.tick)
        self.paused = False
        self.accum = 0.0
        self.seg_start = self.started_at = self.last_input = now_clock()
        self.start_wall = dt.datetime.now().astimezone()
        self.next_k = 1
        self.bells_rung = 0
        self.overtime = False
        self.extra_capped = False
        self.last_checkpoint = 0.0
        self.asleep = 0.0
        self._last_tick = None
        self._next_awake_check = 0.0
        self.set_status("")
        # keep focus off Pause so a stray Space/Enter can't silently pause the meditation
        self.win.set_focus(None)
        for step in (lambda: fresh and self.sw_start.get_active() and self.bell.strike_start(),
                     self.refresh_awake,
                     # anything left unsaved earlier goes in first, so the checkpoint
                     # can't replace it (a resumed meditation's own checkpoint is
                     # carried on with, not recovered)
                     lambda: fresh and self.recover_pending(),
                     lambda: fresh and self.checkpoint()):
            try:
                step()
            except Exception:
                report_exception()

    def tick(self):
        if not self.running:
            self.tick_id = None
            return False
        try:
            self._tick()
        except Exception:
            report_exception()   # printed, and visible to the tests
        if not self.running:
            self.tick_id = None
            return False
        return True

    def _tick(self):
        now, awake = now_clock(), now_awake()
        if self._last_tick is not None and not self.paused and self._last_awake is not None:
            # time that passed on the suspend-proof clock but not on the awake one
            slept = (now - self._last_tick) - (awake - self._last_awake)
            if slept > SLEEP_GAP:
                self.asleep += slept
                if self.overtime:
                    msg = f"The laptop slept for {fmt_hm(self.asleep)} during extra time."
                else:
                    msg = (f"The laptop slept for {fmt_hm(self.asleep)} during this meditation. "
                           "No bells rang during that time.")
                self.set_status(msg, error=True, prefix="Note: ")
        self._last_tick, self._last_awake = now, awake
        if now >= self._next_awake_check:
            self.refresh_awake()
        el = self.elapsed()
        if self.target is not None and el >= self.target and not self.overtime:
            # the planned meditation is done: ring the closing bell, then keep
            # counting extra time until you end it and choose what to save.
            # Bells ring even while the End dialog is open: a dialog opened by
            # mistake mustn't silence the rest of a meditation. (What's saved is
            # still what the dialog showed.)
            self.bell.strike_end()
            self.overtime = True
            self.refresh_awake()  # no bells are left to ring, so sleep is harmless now
            self.checkpoint()
        if self.overtime and not self.paused and el - self.target >= EXTRA_MAX \
                and not self.extra_capped:
            # probably left running: stop counting until told otherwise
            self.extra_capped = True
            self.accum = el = self.target + EXTRA_MAX
            self.paused = True
            self.seg_start = None
            self.checkpoint()
            self.set_status(f"Extra time stopped counting at {fmt_hm(EXTRA_MAX)}. "
                            "Press End to choose what to save, or Resume to keep counting.",
                            error=True, prefix="Note: ")
        if (self.interval and not self.paused
                and (self.target is None or el < self.target)):
            due = self.next_k * self.interval
            if el >= due:
                # if several were skipped (e.g. after a suspend), ring once
                self.next_k = int(el // self.interval) + 1
                self.bells_rung += 1
                self.bell.strike(1)
        if el - self.last_checkpoint >= CHECKPOINT_EVERY:
            self.checkpoint()
        if (self.stack.get_visible_child_name() == "journal"
                and self._stats_min != int(el // 60)):
            self._stats_min = int(el // 60)
            self.refresh_stats(rows=False)   # keep "Today" current while you look at it
        if (not self.dimmed and not self.paused
                and now - self.last_input > DIM_AFTER):
            self.set_dimmed(True)
        self.update_display()

    def beep(self):
        try:
            self.win.get_display().beep()
        except Exception:
            pass

    def confirm(self, title, body, cancel, ok):
        dlg = Gtk.MessageDialog(transient_for=self.win, modal=True,
                                message_type=Gtk.MessageType.QUESTION,
                                buttons=Gtk.ButtonsType.NONE, text=title)
        dlg.format_secondary_text(body)
        dlg.add_buttons(cancel, Gtk.ResponseType.CANCEL, ok, Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.CANCEL)
        resp = self.run_dialog(dlg)
        dlg.destroy()
        return resp == Gtk.ResponseType.OK

    def ask_end(self, title, quitting=False):
        """Ask how to end the meditation in progress. Returns (choice, seconds):
        choice is "keep", "save", "discard", "planned" (only the planned length) or
        "nosleep" (without the time the laptop slept); seconds is the time shown
        in the dialog, which is what gets saved however long the dialog stays open."""
        if self.asking:      # e.g. a window-close request while the dialog is up
            return "keep", self.elapsed()
        self.asking = True
        try:
            el = self.elapsed()
            self._ask_wall = dt.datetime.now().astimezone()
            self._ask_bells = self.bells_rung
            self._ask_asleep = self.asleep
            choice = self._ask_end(title, quitting, el)
        finally:
            self.asking = False
        if choice == "keep" and self.win:
            self.win.set_focus(None)   # not back on End, where Space would reopen this
        return choice, el

    def _ask_end(self, title, quitting, el):
        R = Gtk.ResponseType
        dlg = Gtk.MessageDialog(transient_for=self.win, modal=True,
                                message_type=Gtk.MessageType.QUESTION,
                                buttons=Gtk.ButtonsType.NONE, text=title)
        quit_ = " and quit" if quitting else ""
        dlg.add_button("_Keep meditating", R.CANCEL)
        choices = {}
        slept = int(self.asleep)
        awake = el - slept
        sleep_note = (f" The laptop slept for {fmt_hms_short(slept)} of that." if slept else "")
        # access keys: K keep, W without saving, S save, P planned, E extra, L without sleep
        sleep_button = (f"Save {fmt_hms_short(awake)}, without s_leep{quit_}"
                        if slept and awake >= MIN_LOG_SECONDS else None)
        if self.overtime and el - self.target < 60:
            # the closing bell just rang: nothing extra worth choosing between
            dlg.format_secondary_text(
                f"Your {fmt_hms_short(self.target)} meditation is complete.{sleep_note}")
            if sleep_button and awake < self.target:
                dlg.add_button(sleep_button, R.APPLY)
                choices[R.APPLY] = "nosleep"
            dlg.add_button("_Save and quit" if quitting else "End and _save", R.OK)
            choices[R.OK] = "planned"
        elif self.overtime:
            planned = self.target
            dlg.format_secondary_text(
                f"Your {fmt_hms_short(planned)} meditation is complete, and you "
                f"continued for {fmt_hms_short(el - planned)} more.{sleep_note} "
                "Which length should go in your journal?")
            dlg.add_button(f"Save the _planned {fmt_hms_short(planned)}{quit_}", R.REJECT)
            dlg.add_button(f"Save {fmt_hms_short(el)} with _extra{quit_}", R.OK)
            choices = {R.OK: "save", R.REJECT: "planned"}
            if sleep_button:
                dlg.add_button(sleep_button, R.APPLY)
                choices[R.APPLY] = "nosleep"
        elif el >= MIN_LOG_SECONDS:
            dlg.format_secondary_text(
                f"You have meditated for {fmt_hms(el)}.{sleep_note} "
                "Save it to your journal, or end without saving?")
            dlg.add_button("End _without saving", R.REJECT)
            choices = {R.REJECT: "discard"}
            if sleep_button:
                dlg.add_button(sleep_button, R.APPLY)
                choices[R.APPLY] = "nosleep"
            dlg.add_button("_Save and quit" if quitting else "End and _save", R.OK)
            choices[R.OK] = "save"
        else:
            dlg.format_secondary_text(
                f"{fmt_hms(el)} so far. Under a minute isn’t saved to your journal.")
            dlg.add_button("_End and quit" if quitting else "_End", R.REJECT)
            choices = {R.REJECT: "discard"}
        if len(choices) >= 3:   # four buttons don't fit side by side
            with warnings.catch_warnings():   # deprecated, but GTK 3 has no other way
                warnings.simplefilter("ignore", DeprecationWarning)
                area = dlg.get_action_area()
            area.set_orientation(Gtk.Orientation.VERTICAL)
            area.set_layout(Gtk.ButtonBoxStyle.EXPAND)
        dlg.set_default_response(R.CANCEL)
        resp = self.run_dialog(dlg)
        dlg.destroy()
        return choices.get(resp, "keep")

    def on_stop_clicked(self, _btn):
        if self.warming:
            self.end_warmup(begin=False)   # "Cancel"
            return
        if not self.running:
            return
        choice, el = self.ask_end("End this meditation?")
        if choice != "keep" and self.running:
            self.finish(choice, at=el)

    def finish(self, choice="save", at=None):
        """End the meditation. choice: "save" (all the time meditated), "planned" (only
        the planned length), "discard", or "auto" when there's nobody to ask
        (logout): everything before the planned end, or the planned length with
        any extra time noted but not added. at: elapsed seconds to save, if not now."""
        if not self.running:
            return
        el = self.elapsed() if at is None else at
        completed = self.overtime and el >= self.target
        extra = unsaved_extra = 0
        sleep_left_out = 0
        asleep = self._ask_asleep if at is not None else self.asleep
        if choice == "nosleep":
            sleep_left_out = int(asleep)
            el = max(0, el - sleep_left_out)
            completed = completed and el >= self.target
            choice = "save"
        if completed:
            if choice in ("planned", "auto"):
                unsaved_extra = max(0, int(el) - self.target) if choice == "auto" else 0
                el = self.target
            else:
                extra = max(0, int(el) - self.target)
        save = choice != "discard"
        end_wall = self._ask_wall if at is not None else None
        bells = self._ask_bells if at is not None else self.bells_rung
        self.running = False
        self.overtime = False
        self.paused = False
        self.accum = 0.0
        self.seg_start = None
        if self.tick_id:
            GLib.source_remove(self.tick_id)
            self.tick_id = None
        self.inhibit_off()
        if self.dimmed:
            self.dimmed = False
            self.win.get_style_context().remove_class("dim")
        rec = self.make_record(el, completed, end_wall=end_wall)
        rec["bells"] = bells
        if asleep:
            rec["asleep_seconds"] = int(asleep)
        else:
            rec.pop("asleep_seconds", None)
        if extra:
            rec["extra_seconds"] = extra
        if unsaved_extra:
            rec["unsaved_extra_seconds"] = unsaved_extra
        if sleep_left_out:
            rec["sleep_left_out"] = True
        try:
            if save and any(s["start"] == rec["start"] for s in load_sessions()[0]):
                save = False    # already in the journal (e.g. recovered by another copy)
        except Exception:
            report_exception()
        saved = True
        if not save:
            self.set_status("Meditation ended · not saved"
                            if el >= MIN_LOG_SECONDS and choice == "discard" else "")
        elif el >= MIN_LOG_SECONDS:
            try:
                append_session(rec)
                msg = (f"Meditation complete · {fmt_hms_short(el)} added to your journal"
                       if completed else
                       f"Meditation ended · {fmt_hms_short(el)} added to your journal")
                if extra:
                    msg += f" (including {fmt_hms_short(extra)} extra)"
                if sleep_left_out:
                    msg += f" · {fmt_hms_short(sleep_left_out)} of laptop sleep left out"
                elif asleep:
                    msg += f" · the laptop slept for {fmt_hm(asleep)} during it"
                self.set_status(msg)
            except OSError as e:
                try:   # park it under its own name; it's retried at launch and next Begin
                    atomic_write_json(unsaved_path(rec), rec)
                except OSError:
                    saved = False   # at least keep the last checkpoint
                    try:
                        os.replace(CURRENT_PATH, unsaved_path(rec))
                    except OSError:
                        pass
                self.set_status(f"Couldn’t save this meditation ({e.strerror}). "
                                "It will be added next time.", error=True)
        else:
            self.set_status("Under a minute, so it wasn’t saved")
        if saved:
            try:
                os.remove(CURRENT_PATH)
            except OSError:
                pass
        self._last_shown = None
        self.refresh_stats()
        self.update_display()
        self.update_visibility()

    def make_record(self, el, completed, end_wall=None):
        """The journal line for this meditation."""
        rec = {
            "start": self.start_wall.isoformat(timespec="seconds"),
            "end": (end_wall or dt.datetime.now().astimezone()).isoformat(timespec="seconds"),
            "seconds": int(el),
            "planned": self.target,          # None = infinite
            "interval": self.interval,
            "bells": self.bells_rung,
            "completed": completed,
        }
        if self.asleep:
            rec["asleep_seconds"] = int(self.asleep)
        return rec

    def checkpoint(self):
        """Write what would be saved if the app died now. In extra time that's the
        planned length, with the extra noted but not added (see finish "auto")."""
        el = self.elapsed()
        self.last_checkpoint = el
        if self.overtime:
            rec = self.make_record(self.target, True)
            rec["unsaved_extra_seconds"] = max(0, int(el) - self.target)
        else:
            rec = self.make_record(el, False)
        # enough to carry on after a crash ("_" fields never reach the journal)
        rec["_resume"] = {"boot_id": boot_id(), "clock": now_clock(), "elapsed": el,
                          "paused": self.paused, "overtime": self.overtime,
                          "next_k": self.next_k, "asleep": self.asleep}
        try:
            atomic_write_json(CURRENT_PATH, rec)
        except OSError as e:
            log("checkpoint failed:", e)

    def resumable_checkpoint(self):
        """A checkpoint left by a crash moments ago in this boot (e.g. the desktop
        restarted) can be carried on with, not just recorded."""
        rec = load_json(CURRENT_PATH, None)
        info = rec.get("_resume") if isinstance(rec, dict) else None
        if not isinstance(info, dict) or _valid_session(dict(rec)) is None:
            return None
        try:
            gap = now_clock() - float(info["clock"])
            ok = (info.get("boot_id") is not None and info["boot_id"] == boot_id()
                  and 0 <= gap <= RESUME_WITHIN)
        except (KeyError, TypeError, ValueError):
            ok = False
        return rec if ok else None

    def offer_resume(self, rec):
        info = rec["_resume"]
        el = float(info["elapsed"]) + (0 if info.get("paused") else
                                       max(0.0, now_clock() - float(info["clock"])))
        R = Gtk.ResponseType
        dlg = Gtk.MessageDialog(transient_for=self.win, modal=True,
                                message_type=Gtk.MessageType.QUESTION,
                                buttons=Gtk.ButtonsType.NONE,
                                text="Carry on with your meditation?")
        dlg.format_secondary_text(
            f"The app closed unexpectedly during a meditation, {fmt_hms(el)} in. "
            "Carry on from where it is now, or save it to your journal.")
        dlg.add_button("_Save to journal", R.REJECT)
        dlg.add_button("_Carry on", R.OK)
        dlg.set_default_response(R.OK)
        resp = self.run_dialog(dlg)
        dlg.destroy()
        if resp == R.OK and not self.running:
            self.resume(rec, el)
        else:
            self.recover_pending()
            self.refresh_stats()

    def resume(self, rec, el):
        """Carry on from a crash checkpoint: the time while the app was gone counts
        (you were still sitting); a bell that fell due meanwhile rings once."""
        info = rec["_resume"]
        planned = rec.get("planned")
        self.target = None if planned is None else int(planned)
        self.interval = int(rec.get("interval") or 0)
        self.begin_meditation(fresh=False)
        self.start_wall = dt.datetime.fromisoformat(rec["start"])
        self.accum = el
        self.seg_start = now_clock()
        self.bells_rung = int(rec.get("bells") or 0)
        self.next_k = max(1, int(info.get("next_k") or 1))
        self.asleep = float(info.get("asleep") or 0)
        self.overtime = bool(info.get("overtime"))
        if info.get("paused"):
            self.accum, self.paused, self.seg_start = el, True, None
        self.checkpoint()
        self.set_status(f"Carrying on · {fmt_hms_short(el)} so far")
        self._last_shown = None
        self.update_display()
        self.update_visibility()

    def recover_pending(self):
        """Add any meditation a crash or a failed save left behind to the journal.
        A file is only removed once its meditation is safely in the journal (or is
        too short to keep); one that can't be read is kept, renamed *.bad."""
        paths = sorted(glob.glob(os.path.join(DATA_DIR, "unsaved-*.json")))
        if os.path.exists(CURRENT_PATH):
            paths.append(CURRENT_PATH)
        if not paths:
            return
        sessions, _ = load_sessions()
        known = {s["start"] for s in sessions}
        added, bad = [], 0
        for path in paths:
            try:
                with open(path, "rb") as f:
                    raw = f.read()
            except OSError:
                continue            # can't read it right now: try again next time
            try:
                rec = _valid_session(json.loads(raw.decode("utf-8")))
            except (ValueError, RecursionError):
                rec = None
            if rec is None:
                bad += 1
                try:
                    os.replace(path, unique_bad_path(path))
                except OSError:
                    pass
                continue
            rec = {k: v for k, v in rec.items() if not k.startswith("_")}
            if rec["seconds"] >= MIN_LOG_SECONDS and rec["start"] not in known:
                rec["recovered"] = True
                try:
                    append_session(rec)
                except OSError:
                    if path == CURRENT_PATH:   # move it aside so a new checkpoint can't replace it
                        try:
                            os.replace(path, unsaved_path(rec))
                        except OSError:
                            pass
                    continue
                known.add(rec["start"])
                added.append(rec)
            try:
                os.remove(path)
            except OSError:
                pass
        if added:
            total = sum(r["seconds"] for r in added)
            what = (f"an unfinished meditation ({fmt_hm(total)})" if len(added) == 1
                    else f"{len(added)} unsaved meditations ({fmt_hm(total)})")
            self.set_status(f"Recovered {what} into your journal")
        if bad:
            self.set_status(f"{bad} unreadable saved meditation file{'s' if bad > 1 else ''} "
                            f"kept (*.bad) in {DATA_DIR}", error=True, prefix="Note: ")

    # ------------------------------------------------------------ display
    def update_visibility(self):
        active = self.running or self.warming
        self.btn_stop.set_label("_Cancel" if self.warming else "_End meditation")
        self.settings_rev.set_reveal_child(not active)
        self.timer_outer.set_valign(Gtk.Align.CENTER if active else Gtk.Align.START)
        self.summary.set_visible(active)
        self.btn_stop.set_visible(active)
        self.dur_box.set_sensitive(not self.sw_inf.get_active())
        # Pause is the action you least want hit mid-meditation: don't make it the loud one
        ctx = self.btn_start.get_style_context()
        if active:
            ctx.remove_class("primary")
            ctx.add_class("quiet")
        else:
            ctx.remove_class("quiet")
            ctx.add_class("primary")

    def bell_phrase(self, interval):
        if not interval:
            return "No interval bells"
        m = interval // 60
        return f"Bell every {m} min" if m < 60 or m % 60 else \
            f"Bell every {m // 60} h"

    def update_display(self):
        if self.warming:
            left = max(0, math.ceil(self.warm_until - now_clock()))
            key = ("warm", left)
            if key == self._last_shown:
                return
            self._last_shown = key
            self.ring.queue_draw()
            self.phase.set_text("WARM UP")
            self.clock.set_text(str(left))
            set_a11y(self.clock, f"Warm up: {left} seconds until the meditation starts")
            self.clock_sub.set_text("opening bell when this ends" if self.sw_start.get_active()
                                    else "meditation starts when this ends")
            target, interval = self.planned_settings()
            length = "Until you stop" if target is None else f"{fmt_hm(target)} meditation"
            self.summary.set_text("  ·  ".join([length, self.bell_phrase(interval)]))
            self.btn_start.set_label("_Start now")
            return
        if not self.running:
            target, interval = self.planned_settings()
            self.ring.queue_draw()
            self.phase.set_text("READY")
            self.clock.set_text("∞" if target is None else fmt_hms(target))
            set_a11y(self.clock, "Planned length: " +
                     ("until you stop" if target is None else fmt_words(target)))
            self.clock_sub.set_text(self.bell_phrase(interval))
            self.btn_start.set_label("_Begin")
            return
        el = self.elapsed()
        # widgets only change when what's shown changes (1/s, or 1/min when dimmed)
        unit = 60 if self.dimmed else 1
        key = (int(el // unit), self.paused, self.dimmed, self.bells_rung, self.overtime)
        if key == self._last_shown:
            return
        self._last_shown = key
        self.ring.queue_draw()
        fmt = fmt_dim if self.dimmed else fmt_hms
        if self.overtime:
            e = max(0, int(el - self.target))
            if self.dimmed:          # minutes only, unambiguous at a glance
                shown, total = fmt_dim(e), fmt_dim(el)
            else:
                shown = fmt_hms(e) if e >= 3600 else f"{e // 60}:{e % 60:02d}"
                total = fmt_hms(el)
            self.phase.set_text("PAUSED" if self.paused else "EXTRA TIME")
            self.clock.set_text("+" + shown)
            if self._last_a11y_min != int(el // 60):
                self._last_a11y_min = int(el // 60)
                set_a11y(self.clock, f"Planned meditation complete. Extra time: {fmt_words(e)}")
            self.clock_sub.set_text(f"{fmt_hm(self.target)} complete · total {total}")
            self.summary.set_text(f"{fmt_hm(self.target)} meditation complete  ·  "
                                  "End to choose what to save")
            self.btn_start.set_label("_Resume" if self.paused else "_Pause")
            return
        self.phase.set_text("PAUSED" if self.paused else "MEDITATING")
        self.clock.set_text(fmt(el))
        if self._last_a11y_min != int(el // 60):
            self._last_a11y_min = int(el // 60)
            set_a11y(self.clock, f"Elapsed: {fmt_words(el)}")
        if self.dimmed:
            fmt = fmt_words_up
        if self.interval:
            due = self.next_k * self.interval
            if self.target is not None and due >= self.target:
                self.clock_sub.set_text(f"closing bell in {fmt(self.target - el)}")
            else:
                self.clock_sub.set_text(f"next bell in {fmt(due - el)}")
        elif self.target is not None:
            self.clock_sub.set_text(f"{fmt(self.target - el)} remaining")
        else:
            self.clock_sub.set_text("")
        length = "Until you stop" if self.target is None else f"{fmt_hm(self.target)} meditation"
        bits = [length, self.bell_phrase(self.interval)]
        if self.interval:
            bits.append(f"{self.bells_rung} rung")
        self.summary.set_text("  ·  ".join(bits))
        self.btn_start.set_label("_Resume" if self.paused else "_Pause")

    def refresh_stats(self, rows=True):
        """Recompute the journal page. rows=False leaves the list alone (so a
        selected row stays selected while the tiles update during a meditation)."""
        if not hasattr(self, "stat_labels"):
            return
        sessions, junk = load_sessions()
        today = dt.date.today()
        per_day = {}

        def add(start, seconds):
            for d, sec in split_by_day(start, seconds):
                per_day[d] = per_day.get(d, 0) + sec

        for s in sessions:
            add(start_time(s), s["seconds"])
        if self.running:
            add(self.start_wall, self.elapsed())

        def span(days):
            return sum(v for d, v in per_day.items() if 0 <= (today - d).days < days)

        streak = 0
        d = today if per_day.get(today) else today - dt.timedelta(days=1)
        while per_day.get(d, 0) > 0:
            streak += 1
            d -= dt.timedelta(days=1)
        longest = max([s["seconds"] for s in sessions] + [0])

        values = {
            "today": (fmt_hm(per_day.get(today, 0)), fmt_words(per_day.get(today, 0))),
            "week": (fmt_hm(span(7)), fmt_words(span(7))),
            "month": (fmt_hm(span(30)), fmt_words(span(30))),
            "total": (fmt_hm(sum(per_day.values())), fmt_words(sum(per_day.values()))),
            "streak": (str(streak), f"{streak} day{'s' if streak != 1 else ''}"),
            "longest": (fmt_hm(longest), fmt_words(longest)),
        }
        for k, (lbl, title) in self.stat_labels.items():
            shown, spoken = values[k]
            lbl.set_text(shown)
            set_a11y(lbl, f"{title}: {spoken}")

        self.chart.set_data([(today - dt.timedelta(days=i), per_day.get(today - dt.timedelta(days=i), 0))
                             for i in range(13, -1, -1)])

        if not rows:
            return
        self.store.clear()
        self._sessions, self._junk = sessions, junk
        for idx in range(len(sessions) - 1, -1, -1):
            s = sessions[idx]
            when = start_time(s).astimezone().strftime("%a %b %-d · %H:%M")
            planned = "∞" if s.get("planned") is None else fmt_hm(s["planned"])
            if s.get("manual"):
                planned = "added"
            bells = str(s.get("bells", "")) if s.get("interval") else "–"
            self.store.append([when, fmt_hms_short(s["seconds"]), planned, bells, idx])

    # ------------------------------------------------------------ journal edits
    def on_delete_selected(self, _btn):
        model, it = self.tree.get_selection().get_selected()
        if not it:
            return
        target = self._sessions[model[it][4]]
        if self.confirm("Delete this meditation?", f"{model[it][0]} ({model[it][1]}). This can’t be undone.",
                        "Cancel", "Delete"):
            # re-read and match by content: the log may have changed while the dialog was open
            sessions, junk = load_sessions()
            for i, s in enumerate(sessions):
                if s["start"] == target["start"] and s["seconds"] == target["seconds"]:
                    del sessions[i]
                    break
            try:
                rewrite_sessions(sessions, junk)
            except OSError as e:
                self.set_status(f"Couldn’t update the journal ({e.strerror})", error=True)
            self.refresh_stats()

    def on_add_manual(self, _btn):
        dlg = Gtk.Dialog(title="Add a meditation", transient_for=self.win, modal=True)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Add", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        g = Gtk.Grid(column_spacing=10, row_spacing=12)
        g.set_border_width(20)
        now = dt.datetime.now()
        date_e = Gtk.Entry(text=now.strftime("%Y-%m-%d"), activates_default=True)
        time_e = Gtk.Entry(text=now.strftime("%H:%M"), activates_default=True)
        h = self._spin(0, 99, 0, "Hours")
        m = self._spin(0, 59, 30, "Minutes")
        err = label("", "status", "error", xalign=0)
        for r, (text, w) in enumerate((("_Date (YYYY-MM-DD)", date_e),
                                       ("_Start time (HH:MM)", time_e))):
            l = label(text, "row-label", xalign=0)
            l.set_use_underline(True)
            l.set_mnemonic_widget(w)
            g.attach(l, 0, r, 1, 1)
            g.attach(w, 1, r, 3, 1)
        l = label("_Length", "row-label", xalign=0)
        l.set_use_underline(True)
        l.set_mnemonic_widget(h)
        g.attach(l, 0, 2, 1, 1)
        g.attach(h, 1, 2, 1, 1)
        g.attach(label("h", "muted"), 2, 2, 1, 1)
        lenbox = Gtk.Box(spacing=6)
        lenbox.pack_start(m, False, False, 0)
        lenbox.pack_start(label("m", "muted"), False, False, 0)
        g.attach(lenbox, 3, 2, 1, 1)
        g.attach(err, 0, 3, 4, 1)
        dlg.get_content_area().add(g)
        dlg.show_all()

        def show_error(text, field):
            # text for the eye; focus + description so a screen reader says it too
            err.set_text("Error: " + text)
            set_a11y(field, None, "Error: " + text)
            field.grab_focus()
        while True:
            if self.run_dialog(dlg) != Gtk.ResponseType.OK:
                break
            try:
                start = dt.datetime.strptime(f"{date_e.get_text().strip()} {time_e.get_text().strip()}",
                                             "%Y-%m-%d %H:%M").astimezone()
            except ValueError:
                show_error("check the date and time format.", date_e)
                continue
            h.update()
            m.update()
            secs = int(h.get_value()) * 3600 + int(m.get_value()) * 60
            if secs <= 0:
                show_error("length must be more than zero.", h)
                continue
            if start > dt.datetime.now().astimezone():
                show_error("that start time hasn’t happened yet.", time_e)
                continue
            if any(s["start"] == start.isoformat(timespec="seconds")
                   for s in load_sessions()[0]):
                show_error("there’s already a meditation starting then.", time_e)
                continue
            try:
                append_session({
                    "start": start.isoformat(timespec="seconds"),
                    "end": (start + dt.timedelta(seconds=secs)).isoformat(timespec="seconds"),
                    "seconds": secs, "planned": secs, "interval": 0, "bells": 0,
                    "completed": True, "manual": True,
                })
            except OSError as e:
                show_error(f"couldn’t save ({e.strerror}).", date_e)
                continue
            self.refresh_stats()
            break
        dlg.destroy()

    # ------------------------------------------------------------ staying awake
    # What can silence the bells is the laptop suspending, not the screen blanking.
    # On this desktop cosmic-idle does both from one idle timer, so:
    #   * if idle-suspend is off for the current power source (and sound isn't
    #     going to a monitor), let the screen switch off as usual;
    #   * otherwise block idle, via cosmic-idle's own ScreenSaver API (tied to
    #     this process, so it holds even when the window is hidden) plus GTK's
    #     Wayland idle-inhibit as a fallback.
    # A logind sleep lock is held as a second line of defence. All of this ends at
    # the planned end: extra time has no bells, so the laptop may sleep as usual.
    # Closing the lid still suspends; a suspend is detected and reported (see _tick).
    def idle_would_stop_bells(self):
        try:
            on_battery = False
            for p in glob.glob("/sys/class/power_supply/BAT*/status"):
                with open(p) as f:
                    on_battery = on_battery or f.read().strip() == "Discharging"
            key = "suspend_on_battery_time" if on_battery else "suspend_on_ac_time"
            with open(os.path.join(COSMIC_IDLE_DIR, key)) as f:
                if f.read().strip() != "None":
                    return True
        except OSError:
            return True   # can't tell: play safe
        return self.audio_on_display

    def bells_due(self):
        """Is there any bell left that sleep or a dark screen could silence?"""
        return (self.running and not self.paused and not self.overtime
                and (bool(self.interval) or self.target is not None))

    def check_audio_route(self):
        """If sound goes out through a monitor, a blanked screen is a silent bell.
        Runs in the background so a hung sound server can't stall the timer."""
        if self._audio_proc is not None:
            return
        try:
            proc = Gio.Subprocess.new(AUDIO_SINK_CMD,
                                      Gio.SubprocessFlags.STDOUT_PIPE
                                      | Gio.SubprocessFlags.STDERR_SILENCE)
        except GLib.Error:
            self.audio_on_display = True   # can't tell: play safe
            return
        self._audio_proc = proc

        def done(p, res):
            self._audio_proc = None
            try:
                _, out, _ = p.communicate_utf8_finish(res)
            except GLib.Error:
                out = None
            if out is None or not p.get_successful():
                self.audio_on_display = True   # failed or timed out: play safe
            else:
                sink = out.lower()
                self.audio_on_display = "hdmi" in sink or "displayport" in sink

        def give_up():
            if self._audio_proc is proc:
                proc.force_exit()
            return False
        proc.communicate_utf8_async(None, None, done)
        GLib.timeout_add_seconds(3, give_up)

    def refresh_awake(self):
        """Hold the sleep lock and block idle only while a bell is still due."""
        now = now_clock()
        self._next_awake_check = now + AWAKE_CHECK_EVERY
        need = self.bells_due()
        if need:
            if now >= self._next_audio_check:
                self._next_audio_check = now + 30
                self.check_audio_route()
            if self.sleep_fd is None and not self._sleep_pending:
                self.inhibit_on()
        else:
            self.inhibit_off_sleep()
        self.block_idle(need and self.idle_would_stop_bells())

    def _screensaver(self, method, args, reply=None, done=None):
        """Call cosmic-idle's org.freedesktop.ScreenSaver without blocking;
        done(result or None) runs when it answers."""
        def finished(bus, res):
            try:
                result = bus.call_finish(res)
            except GLib.Error:
                result = None
            if done:
                done(result)
        conn = self.get_dbus_connection()
        if conn is None:
            if done:
                done(None)
            return
        conn.call("org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver",
                  "org.freedesktop.ScreenSaver", method, args,
                  GLib.VariantType(reply) if reply else None,
                  Gio.DBusCallFlags.NONE, 2000, None, finished)

    def block_idle(self, on):
        if on == self.idle_blocked:
            return
        self.idle_blocked = on
        if on:
            def got_cookie(result):
                cookie = result.unpack()[0] if result is not None else None
                if cookie is None:
                    return
                if self.idle_blocked and self.ss_cookie is None:
                    self.ss_cookie = cookie
                else:          # no longer wanted by the time it arrived
                    self._screensaver("UnInhibit", GLib.Variant("(u)", (cookie,)))
            self._screensaver(
                "Inhibit", GLib.Variant("(ss)", ("Meditation Bell", "Meditation in progress")),
                "(u)", got_cookie)
            try:
                self.inhibit_cookie = self.inhibit(
                    self.win, Gtk.ApplicationInhibitFlags.IDLE, "Meditation in progress")
            except Exception:
                self.inhibit_cookie = 0
        else:
            if self.ss_cookie is not None:
                self._screensaver("UnInhibit", GLib.Variant("(u)", (self.ss_cookie,)))
                self.ss_cookie = None
            if self.inhibit_cookie:
                self.uninhibit(self.inhibit_cookie)
                self.inhibit_cookie = 0

    def inhibit_on(self):
        """Take a logind sleep lock, without blocking. It's a file descriptor held
        by this process, so it ends with the process however it ends."""
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except GLib.Error:
            return
        self._sleep_pending = True

        def finished(bus, res):
            self._sleep_pending = False
            try:
                result, fds = bus.call_with_unix_fd_list_finish(res)
                idx, got = result.unpack()[0], fds.steal_fds()
            except (GLib.Error, AttributeError, IndexError, TypeError):
                return
            fd = got[idx] if idx < len(got) else None
            for i, other in enumerate(got):
                if i != idx:
                    os.close(other)
            if fd is None:
                return
            if self.bells_due() and self.sleep_fd is None:
                self.sleep_fd = fd
            else:              # the meditation ended (or paused) meanwhile
                os.close(fd)
        bus.call_with_unix_fd_list(
            "org.freedesktop.login1", "/org/freedesktop/login1",
            "org.freedesktop.login1.Manager", "Inhibit",
            GLib.Variant("(ssss)", ("sleep", "Meditation Bell",
                                    "Meditation in progress", "block")),
            GLib.VariantType("(h)"), Gio.DBusCallFlags.NONE, 5000, None, None, finished)

    def inhibit_off_sleep(self):
        if self.sleep_fd is not None:
            try:
                os.close(self.sleep_fd)
            except OSError:
                pass
            self.sleep_fd = None

    def inhibit_off(self):
        self.block_idle(False)
        self.inhibit_off_sleep()

    # ------------------------------------------------------------ close
    def on_close(self, *_):
        self.end_warmup(begin=False)
        if not self.running:
            self.save_config()
            return False
        choice, el = self.ask_end("A meditation is in progress.", quitting=True)
        if choice != "keep":
            self.finish(choice, at=el)
            return False
        return self.running   # True keeps the window open

    def on_signal(self, *_):
        try:
            self.end_warmup(begin=False)
            self.finish("auto")   # logout/shutdown: nobody to ask
            if hasattr(self, "cfg"):
                self.save_config()
        finally:
            self.close_dialogs()
            self.release_lock()
            self.quit()
        return GLib.SOURCE_REMOVE


def main():
    GLib.set_prgname("bell-timer")   # window class -> StartupWMClass in the desktop file
    GLib.set_application_name("Meditation Bell")
    Gst.init(None)
    app = BellTimer()
    # closing a terminal the app was started from shouldn't end a meditation
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    for sig in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, app.on_signal)
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
