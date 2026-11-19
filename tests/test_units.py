"""Unit tests: pure helpers, the journal file format, and colour contrast."""
import datetime as dt
import json
import os

import pytest

import bell_timer as bt


# ---------------------------------------------------------------- formatting

@pytest.mark.parametrize("secs,out", [
    (0, "0:00:00"), (59, "0:00:59"), (61, "0:01:01"), (3600, "1:00:00"),
    (36000, "10:00:00"), (-5, "0:00:00"), (59.9, "0:00:59"),
])
def test_fmt_hms(secs, out):
    assert bt.fmt_hms(secs) == out


@pytest.mark.parametrize("secs,out", [
    (0, "0m"), (60, "1m"), (3600, "1h"), (3660, "1h 01m"), (10800, "3h"), (5400, "1h 30m"),
])
def test_fmt_hm(secs, out):
    assert bt.fmt_hm(secs) == out


@pytest.mark.parametrize("secs,out", [
    (0, "0s"), (45, "45s"), (300, "5m"), (450, "7m 30s"), (61, "1m 01s"), (3600, "1h 00m"),
    (3930, "1h 05m 30s"), (36000, "10h 00m"),
])
def test_fmt_hms_short(secs, out):
    assert bt.fmt_hms_short(secs) == out


@pytest.mark.parametrize("secs,out", [
    (60, "1 minute"), (120, "2 minutes"), (3600, "1 hour"), (3660, "1 hour 1 minute"),
    (7320, "2 hours 2 minutes"), (0, "0 minutes"),
])
def test_fmt_words(secs, out):
    assert bt.fmt_words(secs) == out


@pytest.mark.parametrize("secs,out", [
    (299, "5 minutes"), (300, "5 minutes"), (301, "6 minutes"), (1, "1 minute"), (0, "0 minutes"),
])
def test_fmt_words_up_rounds_up_for_countdowns(secs, out):
    assert bt.fmt_words_up(secs) == out


# ---------------------------------------------------------------- journal records

def test_valid_session_accepts_and_coerces():
    s = bt._valid_session({"start": "2026-11-15T10:00:00-05:00", "seconds": "90.7",
                           "planned": "300"})
    assert s["seconds"] == 90 and s["planned"] == 300


@pytest.mark.parametrize("bad", [
    None, [], "x", {}, {"start": 5}, {"start": "not a date", "seconds": 1},
    {"start": "2026-11-15T10:00:00", "seconds": "lots"},
])
def test_valid_session_rejects(bad):
    assert bt._valid_session(bad) is None


def test_valid_session_drops_unreadable_planned_but_keeps_row():
    s = bt._valid_session({"start": "2026-11-15T10:00:00", "seconds": 60, "planned": "an hour"})
    assert s is not None and s["planned"] is None


def test_negative_seconds_clamped():
    assert bt._valid_session({"start": "2026-11-15T10:00:00", "seconds": -4})["seconds"] == 0


def _write(lines):
    with open(bt.LOG_PATH, "w") as f:
        f.write("\n".join(lines) + "\n")


def test_load_sessions_sorts_and_keeps_junk():
    a = json.dumps({"start": "2026-11-15T12:00:00", "seconds": 60})
    b = json.dumps({"start": "2026-11-14T12:00:00", "seconds": 120})
    _write([a, "garbage {", "", b, json.dumps({"no": "start"})])
    sessions, junk = bt.load_sessions()
    assert [s["seconds"] for s in sessions] == [120, 60]
    assert junk == [(1, "garbage {"), (2, ""), (4, json.dumps({"no": "start"}))]


def test_load_sessions_missing_file():
    assert bt.load_sessions() == ([], [])


def test_append_then_rewrite_preserves_junk_and_makes_backup():
    bt.append_session({"start": "2026-11-15T12:00:00", "seconds": 60})
    with open(bt.LOG_PATH, "a") as f:
        f.write("hand-written note\n")
    bt.append_session({"start": "2026-11-15T13:00:00", "seconds": 90})
    sessions, junk = bt.load_sessions()
    before = open(bt.LOG_PATH).read()
    bt.rewrite_sessions(sessions[1:], junk)
    assert open(bt.LOG_PATH + ".bak").read() == before
    after_sessions, after_junk = bt.load_sessions()
    assert [s["seconds"] for s in after_sessions] == [90]
    assert after_junk == [(0, "hand-written note")]   # same place: the line above it went
    assert open(bt.LOG_PATH).read().splitlines()[0] == "hand-written note"
    assert not os.path.exists(bt.LOG_PATH + ".tmp")


def test_atomic_write_and_load_json(tmp_path):
    p = str(tmp_path / "x.json")
    bt.atomic_write_json(p, {"a": 1})
    assert bt.load_json(p, None) == {"a": 1}
    assert not os.path.exists(p + ".tmp")
    open(p, "w").write("{broken")
    assert bt.load_json(p, "default") == "default"
    assert bt.load_json(str(tmp_path / "missing"), 7) == 7


# ---------------------------------------------------------------- days

def test_split_by_day_within_one_day():
    start = dt.datetime(2026, 11, 15, 10, 0).astimezone()
    assert list(bt.split_by_day(start, 3600)) == [(start.date(), 3600)]


def test_split_by_day_across_midnight():
    start = dt.datetime(2026, 11, 15, 23, 30).astimezone()
    parts = list(bt.split_by_day(start, 3 * 3600))
    assert [p[0].day for p in parts] == [15, 16]
    assert [round(p[1]) for p in parts] == [1800, 9000]


def test_split_by_day_zero():
    assert list(bt.split_by_day(dt.datetime(2026, 11, 15, 1).astimezone(), 0)) == []


# ---------------------------------------------------------------- colour contrast (WCAG 2.1 AA)

def _lum(hexc):
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hexc[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


TEXT_PAIRS = [  # foreground, background: must reach 4.5:1
    ("text", "bg"), ("text", "surface"), ("text", "field"), ("text", "hover"), ("text", "track"),
    ("muted", "bg"), ("muted", "surface"), ("muted", "field"), ("muted", "hover"),
    ("accent", "bg"), ("accent", "surface"), ("on_accent", "accent"),
    ("danger", "bg"), ("danger", "surface"), ("bg", "text"),
]
UI_PAIRS = [  # control boundaries and meaningful graphics: must reach 3:1
    ("border", "bg"), ("border", "surface"), ("border", "field"), ("accent", "field"),
    ("bar", "surface"),
]


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_text_contrast_meets_aa(mode):
    p = bt.PALETTES[mode]
    low = {f"{a}/{b}": round(contrast(p[a], p[b]), 2) for a, b in TEXT_PAIRS
           if contrast(p[a], p[b]) < 4.5}
    assert not low


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_ui_contrast_meets_aa(mode):
    p = bt.PALETTES[mode]
    low = {f"{a}/{b}": round(contrast(p[a], p[b]), 2) for a, b in UI_PAIRS
           if contrast(p[a], p[b]) < 3}
    assert not low


def test_palettes_have_same_keys():
    assert set(bt.PALETTES["light"]) == set(bt.PALETTES["dark"])


def test_hex_rgb():
    assert bt.hex_rgb("#ff8000") == pytest.approx((1.0, 128 / 255, 0.0))


# ---------------------------------------------------------------- second review

def test_sort_uses_real_time_across_offsets():
    """01:50-04:00 is 05:50 UTC, earlier than 01:10-05:00 (06:10 UTC)."""
    bt.append_session({"start": "2026-11-01T01:10:00-05:00", "seconds": 60})
    bt.append_session({"start": "2026-11-01T01:50:00-04:00", "seconds": 120})
    assert [s["seconds"] for s in bt.load_sessions()[0]] == [120, 60]


def test_rewrite_keeps_hand_edits_exactly():
    lines = ['{"start": "2026-11-14T07:00:00-05:00", "seconds": 600.5, "planned": "1 hour"}',
             "my note about the retreat",
             '{"start": "2026-11-15T07:00:00-05:00", "seconds": 300}']
    with open(bt.LOG_PATH, "w") as f:
        f.write("\n".join(lines) + "\n")
    sessions, junk = bt.load_sessions()
    bt.rewrite_sessions(sessions[:1], junk)          # delete the second meditation
    assert open(bt.LOG_PATH).read().splitlines() == lines[:2]


@pytest.fixture
def new_york(monkeypatch):
    import time
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_split_by_day_on_the_daylight_saving_change(new_york):
    """1 Nov 2026: clocks go back at 2 am, so that day has 25 hours."""
    start = dt.datetime(2026, 10, 31, 23, 0).astimezone()
    parts = list(bt.split_by_day(start, 27 * 3600))
    assert [(p[0].day, round(p[1])) for p in parts] == [(31, 3600), (1, 25 * 3600), (2, 3600)]


def test_fmt_dim():
    assert [bt.fmt_dim(s) for s in (0, 59, 60, 2700, 3600, 3900)] == \
        ["0m", "0m", "1m", "45m", "1h 00m", "1h 05m"]


@pytest.mark.durable
def test_atomic_write_is_durable(monkeypatch, tmp_path):
    synced = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real(fd)))
    bt.atomic_write_json(str(tmp_path / "x.json"), {"a": 1})
    assert len(synced) == 2          # the file, then its folder


def _css_rules():
    """(selector, declarations) pairs from the stylesheet, braces un-doubled."""
    import re
    css = bt.CSS_TEMPLATE.format(**bt.PALETTES["light"])   # fills {text} etc., un-doubles braces
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [(sel.strip(), body) for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)]


def test_thin_type_is_only_used_large():
    """Contrast ratios don't see stroke weight: hairline type (weight below 400) is
    hard to read unless it's big, whatever its colour. Allow it only from 3em up
    (the clock); smaller text uses regular weight or heavier."""
    import re
    bad = {}
    rules = _css_rules()
    assert any(".clock" in sel for sel, _ in rules), "stylesheet didn't parse"
    for sel, body in rules:
        w = re.search(r"font-weight:\s*(\d+)", body)
        if w and int(w.group(1)) < 400:
            size = re.search(r"font-size:\s*([\d.]+)em", body)
            if not size or float(size.group(1)) < 3:
                bad[sel] = body.strip()
    assert not bad, f"thin type at small size: {bad}"
