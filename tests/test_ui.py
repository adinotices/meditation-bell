"""Integration tests: what the window shows, settings, themes, the journal page
and accessibility. Every state is also rendered to an image to catch drawing errors."""
import json

import cairo
import pytest
from gi.repository import Gtk

import bell_timer as bt
from conftest import begin, configure, pump, run_for


def render(app):
    """Draw the whole window offscreen; any exception in a draw handler fails here."""
    pump()
    a = app.win.get_allocation()
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, max(1, a.width), max(1, a.height))
    app.win.draw(cairo.Context(surf))
    return surf


def a11y_name(w):
    return w.get_accessible().get_name()


# ---------------------------------------------------------------- ready view

def test_ready_view(app):
    configure(app, hours=1, interval_min=30)
    assert app.phase.get_text() == "READY"
    assert app.clock.get_text() == "1:00:00"
    assert app.clock_sub.get_text() == "Bell every 30 min"
    assert app.btn_start.get_label() == "_Begin" and not app.btn_stop.get_visible()
    assert a11y_name(app.clock) == "Planned length: 1 hour"


@pytest.mark.parametrize("minutes,phrase", [(0, "No interval bells"), (45, "Bell every 45 min"),
                                            (120, "Bell every 2 h"), (90, "Bell every 90 min")])
def test_bell_phrase(app, minutes, phrase):
    configure(app, minutes=5, interval_min=minutes)
    assert app.clock_sub.get_text() == phrase


def test_infinity_toggle(app):
    configure(app, minutes=20)
    assert app.dur_box.get_sensitive()
    app.sw_inf.clicked()
    assert app.sw_inf.get_active() and not app.dur_box.get_sensitive()
    assert app.clock.get_text() == "∞" and app.planned_settings() == (None, 0)
    assert a11y_name(app.sw_inf) == "Infinite length, until you stop"
    app.sw_inf.clicked()
    assert app.dur_box.get_sensitive() and app.clock.get_text() == "0:20:00"


def test_interval_marks_on_ring(app):
    configure(app, hours=1, interval_min=15)
    assert app.ring_state() == (0, [0.25, 0.5, 0.75])
    configure(app, hours=1, interval_min=1)        # 59 marks would be noise
    assert app.ring_state() == (0, [])


# ---------------------------------------------------------------- running view

def test_running_view(app, clock):
    begin(app, clock, minutes=30, interval_min=10)
    run_for(app, clock, 98)
    app.on_user_input()
    assert app.phase.get_text() == "MEDITATING" and app.clock.get_text() == "0:01:40"
    assert app.clock_sub.get_text() == "next bell in 0:08:20"
    assert app.summary.get_text() == "30m meditation  ·  Bell every 10 min  ·  0 rung"
    assert app.btn_start.get_label() == "_Pause" and app.btn_stop.get_visible()
    assert not app.settings_rev.get_reveal_child()
    # Pause must not be the loud button, and must not have keyboard focus
    assert app.btn_start.get_style_context().has_class("quiet")
    assert app.win.get_focus() is not app.btn_start


def test_closing_bell_countdown_wording(app, clock):
    begin(app, clock, minutes=10, interval_min=6)
    run_for(app, clock, 400)
    app.on_user_input()
    assert app.clock_sub.get_text() == "closing bell in 0:03:18"


def test_infinite_view_without_bells(app, clock):
    begin(app, clock, infinite=True)
    assert app.clock_sub.get_text() == "" and app.summary.get_text().startswith("Until you stop")


# ---------------------------------------------------------------- settings

def test_settings_saved_and_restored(make_app):
    app = make_app()
    configure(app, hours=2, minutes=15, interval_min=25, warmup=40, start_bell=False)
    app.sw_inf.set_active(True)
    app.vol.set_value(0.55)
    app.save_config()
    cfg = json.load(open(bt.CONFIG_PATH))
    assert cfg == {**cfg, "hours": 2, "minutes": 15, "interval_min": 25, "warmup_sec": 40,
                   "start_bell": False, "infinite": True, "volume": 0.55}
    again = make_app()
    assert (again.sp_hours.get_value(), again.sp_mins.get_value()) == (2, 15)
    assert again.sp_warm.get_value() == 40 and again.sw_inf.get_active()
    assert not again.sw_start.get_active() and again.bell.volume == 0.55


def test_defaults_on_first_launch(app):
    assert app.sp_warm.get_value() == 15 and app.sp_int.get_value() == 30
    assert app.sp_hours.get_value() == 1 and not app.sw_inf.get_active()


def test_damaged_config_falls_back(make_app):
    open(bt.CONFIG_PATH, "w").write(json.dumps({"theme": "purple", "volume": "loud"}))
    app = make_app()
    assert app.cfg["theme"] == "auto" and app.cfg["volume"] == bt.DEFAULTS["volume"]


def test_volume_slider_sets_bell_volume(app):
    app.vol.set_value(0.3)
    assert app.bell.volume == pytest.approx(0.3)


def test_preview_buttons(app):
    chips = [w for w in app.settings_rev.get_child().get_children()[0].get_children()
             if isinstance(w, Gtk.Box) and w.get_halign() == Gtk.Align.END
             and all(isinstance(c, Gtk.Button) for c in w.get_children())
             and len(w.get_children()) == 3]
    for b in chips[0].get_children():
        b.clicked()
    assert app.events == ["preview:start", "preview:interval", "preview:end"]


# ---------------------------------------------------------------- theme

def test_theme_cycles_and_is_saved(app):
    app.cfg["theme"] = "auto"
    seen = []
    for _ in range(3):
        app.cycle_theme(None)
        seen.append(app.cfg["theme"])
    assert seen == ["light", "dark", "auto"]
    assert json.load(open(bt.CONFIG_PATH))["theme"] == "auto"


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_theme_applies_palette(app, mode):
    app.cfg["theme"] = mode
    app.apply_theme()
    assert app.palette == bt.PALETTES[mode]


# ---------------------------------------------------------------- rendering every state

@pytest.mark.parametrize("mode", ["light", "dark"])
def test_every_state_renders(app, clock, dialogs, mode):
    app.cfg["theme"] = mode
    app.apply_theme()
    render(app)                                            # ready
    configure(app, minutes=5, interval_min=1, warmup=10)
    app.on_start_pause(None)
    render(app)                                            # warm-up
    run_for(app, clock, 11)
    render(app)                                            # meditating
    app.on_start_pause(None)
    render(app)                                            # paused
    app.on_start_pause(None)
    run_for(app, clock, 60)
    render(app)                                            # dimmed
    run_for(app, clock, 400)
    app.on_user_input()
    render(app)                                            # extra time
    dialogs.answer(Gtk.ResponseType.OK)
    app.on_stop_clicked(None)
    app.stack.set_visible_child_name("journal")
    render(app)                                            # journal with a row
    app.sw_inf.set_active(True)
    app.stack.set_visible_child_name("meditate")
    surf = render(app)                                     # ∞ ready
    assert surf.get_width() > 300


# ---------------------------------------------------------------- journal stats

def _log(days_ago, hour, seconds, **extra):
    start = (bt.dt.datetime.now().astimezone().replace(hour=hour, minute=0, second=0,
                                                       microsecond=0)
             - bt.dt.timedelta(days=days_ago))
    bt.append_session({"start": start.isoformat(timespec="seconds"), "seconds": seconds,
                       "planned": seconds, "interval": 0, **extra})


def stats(app):
    app.refresh_stats()
    return {k: lbl.get_text() for k, (lbl, _title) in app.stat_labels.items()}


def test_stats(app, wall):
    _log(0, 6, 3600)
    _log(0, 9, 1800)
    _log(1, 6, 7200)
    _log(2, 6, 600)
    _log(10, 6, 3600)
    _log(40, 6, 36000)
    s = stats(app)
    assert s == {"today": "1h 30m", "week": "3h 40m", "month": "4h 40m",
                 "total": "14h 40m", "streak": "3", "longest": "10h"}
    assert len(app.chart.values) == 14 and app.chart.values[-1][1] == 5400


def test_streak_counts_from_yesterday_if_nothing_today(app, wall):
    _log(1, 6, 600)
    _log(2, 6, 600)
    assert stats(app)["streak"] == "2"


def test_meditation_in_progress_counts_today(app, clock, wall):
    begin(app, clock, infinite=True)
    run_for(app, clock, 1198)
    assert stats(app)["today"] == "20m"


def test_meditation_across_midnight_splits_between_days(app, clock, wall, dialogs):
    wall.set(bt.dt.datetime(2026, 11, 15, 23, 30))
    begin(app, clock, infinite=True)
    run_for(app, clock, 3598, step=5)
    wall.set(bt.dt.datetime(2026, 11, 16, 0, 30))
    dialogs.answer(Gtk.ResponseType.OK)
    app.on_stop_clicked(None)
    s = stats(app)
    assert s["today"] == "30m" and s["week"] == "1h" and s["streak"] == "2"
    assert app.chart.values[-1][1] == 1800 and app.chart.values[-2][1] == 1800


def test_journal_rows(app, wall):
    _log(0, 6, 450, planned=300, extra_seconds=150, interval=60, bells=4)
    _log(0, 7, 900, planned=None)
    _log(0, 8, 1800, manual=True)
    rows = [tuple(r)[1:4] for r in (stats(app) and app.store)]
    assert rows == [("30m", "added", "–"), ("15m", "∞", "–"), ("7m 30s", "5m", "4")]


def test_chart_has_spoken_summary(app, wall):
    _log(0, 6, 3600)
    app.refresh_stats()
    acc = app.chart.get_accessible()
    assert acc.get_name() == "Time meditated per day, last 14 days"
    assert acc.get_description().startswith("1 hour across 1 day. ")


# ---------------------------------------------------------------- accessibility

def test_settings_have_keyboard_mnemonics(app):
    labels = {}

    def walk(w):
        if isinstance(w, Gtk.Label) and w.get_mnemonic_widget():
            labels[w.get_label()] = w.get_mnemonic_widget()
        if isinstance(w, Gtk.Container):
            for c in w.get_children():
                walk(c)
    walk(app.settings_rev)
    assert set(labels) >= {"_Length", "_Interval bell", "_Opening bell", "_Warm up", "_Volume"}
    letters = [k[k.index("_") + 1].lower() for k in labels]
    assert len(letters) == len(set(letters)), "two settings share an Alt shortcut"


def test_controls_have_accessible_names(app):
    for w, name in [(app.vol, "Bell volume"), (app.sp_warm, "Warm up, seconds"),
                    (app.sp_int, "Interval bell, minutes"), (app.tree, "Logged meditations")]:
        assert a11y_name(w) == name


def test_errors_are_marked_in_text_not_only_colour(app):
    configure(app, minutes=0, hours=0)
    app.on_start_pause(None)
    assert app.status.get_text().startswith("Error: ")
    assert app.status.get_style_context().has_class("error")


# ---------------------------------------------------------------- bells setup

def test_missing_interval_bell_falls_back_to_opening_bell(tmp_path):
    bell = bt.Bell(str(tmp_path / "nope.wav"), bt.START_BELL_PATH, bt.END_BELL_PATH)
    assert bell.missing == [] and bell.uri == bell.start_uri


def test_missing_bells_are_reported(make_app, monkeypatch, tmp_path):
    for name in ("BELL_PATH", "START_BELL_PATH", "END_BELL_PATH"):
        monkeypatch.setattr(bt, name, str(tmp_path / f"{name}.wav"))
    app = make_app()
    assert app.status.get_text().startswith("Error: Missing bell file:")


def test_shipped_bells_are_found():
    bell = bt.Bell(bt.BELL_PATH, bt.START_BELL_PATH, bt.END_BELL_PATH)
    assert bell.missing == []
    assert len({bell.uri, bell.start_uri, bell.end_uri}) == 3
