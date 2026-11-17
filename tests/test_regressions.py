"""Regression tests: one per bug found and fixed earlier, so they stay fixed."""
from gi.repository import Gtk

import os

import bell_timer as bt
from conftest import begin, configure, journal, run_for


def test_dimmed_countdown_rounds_up(app, clock):
    """4:59 left used to read 'next bell in 6 minutes'... then '4 minutes'; it is 5."""
    begin(app, clock, minutes=30, interval_min=10)
    run_for(app, clock, 299)           # 5:01 elapsed, 4:59 to the next bell
    app.set_dimmed(True)
    assert app.clock_sub.get_text() == "next bell in 5 minutes"


def test_timer_survives_an_exception_in_a_tick(app, clock, monkeypatch):
    """An error inside one tick used to stop the timer for good."""
    begin(app, clock, minutes=30)
    calls = []

    def boom():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("display glitch")
    monkeypatch.setattr(app, "update_display", boom)
    assert app.tick() is True and app.tick() is True and len(calls) == 2


def test_length_fields_follow_infinity_after_a_meditation(app, clock, dialogs):
    app.sw_inf.set_active(True)
    begin(app, clock, infinite=True)
    dialogs.answer(Gtk.ResponseType.REJECT)
    app.on_stop_clicked(None)
    assert not app.dur_box.get_sensitive()


def test_start_now_rings_opening_bell_once(app, clock):
    configure(app, minutes=5, warmup=30)
    app.on_start_pause(None)
    clock.advance(5)
    app.on_start_pause(None)       # Start now
    run_for(app, clock, 60)
    assert app.events.count("start") == 1


def test_planned_end_passing_while_end_dialog_is_open_saves_what_was_shown(app, clock, dialogs):
    """The planned end may pass while you're reading the End dialog: the closing
    bell waits, and what's saved is what the dialog said."""
    begin(app, clock, minutes=2)
    run_for(app, clock, 100)

    def end_passes_while_open(_dlg):
        run_for(app, clock, 60)
        assert "end" not in app.events and not app.overtime
        return Gtk.ResponseType.OK
    dialogs.answer(end_passes_while_open)
    app.on_stop_clicked(None)
    rec = journal(bt.DATA_DIR)[-1]
    assert rec["seconds"] == 102 and rec["completed"] is False
    assert "end" not in app.events


def test_end_passing_during_dialog_then_discard_keeps_its_word(app, clock, dialogs):
    """Reviewer's probe: 1-minute sit, End opened at 40 s. Clicking End must not
    discard a meditation that has meanwhile become complete, with a bell mid-dialog."""
    begin(app, clock, minutes=1)
    run_for(app, clock, 38)

    def wait_then_end(_dlg):
        run_for(app, clock, 60)
        return Gtk.ResponseType.REJECT
    dialogs.answer(wait_then_end)
    app.on_stop_clicked(None)
    assert dialogs.last["buttons"] == ["_Keep meditating", "_End"]
    assert "end" not in app.events and journal(bt.DATA_DIR) == []


def test_keep_after_end_passed_in_dialog_rings_closing_bell_once(app, clock, dialogs):
    begin(app, clock, minutes=1)
    run_for(app, clock, 50)

    def wait_then_keep(_dlg):
        run_for(app, clock, 30)
        return Gtk.ResponseType.CANCEL
    dialogs.answer(wait_then_keep)
    app.on_stop_clicked(None)
    run_for(app, clock, 2)
    assert app.events.count("end") == 1 and app.overtime


def test_hand_edited_planned_value_does_not_break_journal(app):
    with open(bt.LOG_PATH, "w") as f:
        f.write('{"start": "2026-11-15T07:00:00-05:00", "seconds": 600, "planned": "1 hour"}\n')
    app.refresh_stats()
    assert [tuple(r)[1:3] for r in app.store] == [("10m", "∞")]


def test_overshoot_after_laptop_sleep_is_offered_as_planned(app, clock, dialogs):
    """Waking long after the planned end used to inflate the saved time silently;
    now the extra (including the sleep) is shown and 'Save planned' trims it."""
    begin(app, clock, minutes=5)
    run_for(app, clock, 200)
    clock.sleep(3600)
    app._tick()
    assert app.overtime and app.asleep > 3500
    dialogs.answer(Gtk.ResponseType.REJECT)
    app.on_stop_clicked(None)
    assert journal(bt.DATA_DIR)[-1]["seconds"] == 300


# ---------------------------------------------------------------- from the second review

def test_append_after_a_line_without_newline_keeps_both():
    """Probe: a hand edit dropped the final newline; the next save was glued on."""
    with open(bt.LOG_PATH, "w") as f:
        f.write('{"start": "2026-11-14T07:00:00-05:00", "seconds": 600}')
    bt.append_session({"start": "2026-11-15T07:00:00-05:00", "seconds": 300})
    sessions, junk = bt.load_sessions()
    assert [s["seconds"] for s in sessions] == [600, 300] and junk == []
    bt.append_session({"start": "2026-11-16T07:00:00-05:00", "seconds": 60})
    assert open(bt.LOG_PATH).read().count("\n") == 3


def test_no_interval_bell_with_closing_bell_after_sleep(app, clock):
    """Probe A: 60 min sit, bell every 25, lid closed 40 -> 70: rang end AND interval."""
    begin(app, clock, minutes=60, interval_min=25)
    run_for(app, clock, 40 * 60 - 2)
    clock.sleep(30 * 60)
    app._tick()
    assert [e for e in app.events if e in ("start", "interval", "end")] == \
        ["start", "interval", "end"]
    assert app.bells_rung == 1


def test_extra_time_lets_the_laptop_sleep(app, clock, monkeypatch):
    refreshes = []
    monkeypatch.setattr(app, "refresh_awake", lambda: refreshes.append(1))
    begin(app, clock, minutes=5)
    assert app.events.count("inhibit_on") == 1
    run_for(app, clock, 298 + 1)
    assert app.overtime and app.events.count("inhibit_off") == 1
    refreshes.clear()
    run_for(app, clock, 600)
    assert refreshes == []          # idle is no longer blocked in extra time


def test_extra_time_stops_counting_when_left_running(app, clock, dialogs):
    begin(app, clock, minutes=5)
    run_for(app, clock, 298 + bt.EXTRA_MAX + 3600, step=5)
    app.on_user_input()
    assert app.paused and app.clock.get_text() == "+4:00:00"
    assert app.status.get_text().startswith("Note: Extra time stopped counting at 4h")
    app.on_start_pause(None)        # Resume: counts on, and isn't stopped again
    run_for(app, clock, 600)
    app.on_user_input()
    assert not app.paused and app.clock.get_text() == "+4:10:00"
    dialogs.answer(Gtk.ResponseType.OK)
    app.on_stop_clicked(None)
    assert journal(bt.DATA_DIR)[-1]["extra_seconds"] == bt.EXTRA_MAX + 600


def test_sleep_note_in_extra_time_doesnt_mention_bells(app, clock):
    begin(app, clock, minutes=5)
    run_for(app, clock, 300)
    clock.sleep(1800)
    app._tick()
    assert app.status.get_text() == "Note: The laptop slept for 30m during extra time."


def test_busy_app_is_not_reported_as_sleep(app, clock):
    """A main-loop stall used to be counted as laptop sleep."""
    begin(app, clock, infinite=True)
    clock.advance(120)              # awake, but no ticks ran
    app._tick()
    assert app.asleep == 0 and app.status.get_text() == ""


def test_typed_warmup_is_remembered(app, clock):
    """Probe N: typing 45 and pressing Begin used 45 but saved 15."""
    configure(app, minutes=5)
    app.sp_warm.set_text("45")      # typed, not yet confirmed with Enter or Tab
    app.on_start_pause(None)
    assert app.warm_total == 45
    assert bt.load_json(bt.CONFIG_PATH, {})["warmup_sec"] == 45


def test_journal_tab_stays_current_during_a_meditation(app, clock, wall):
    app.stack.set_visible_child_name("journal")
    begin(app, clock, infinite=True)
    run_for(app, clock, 30 * 60, step=5)
    assert app.stat_labels["today"][0].get_text() == "30m"


def test_dimmed_extra_time_over_an_hour_reads_as_hours(app, clock):
    begin(app, clock, minutes=1)
    run_for(app, clock, 58 + 3900, step=5)
    assert app.dimmed and app.clock.get_text() == "+1h 05m"


def test_data_is_private(make_app):
    import stat
    os.chmod(bt.DATA_DIR, 0o755)
    make_app()
    assert stat.S_IMODE(os.stat(bt.DATA_DIR).st_mode) == 0o700
    bt.append_session({"start": "2026-11-15T07:00:00-05:00", "seconds": 300})
    bt.atomic_write_json(bt.CONFIG_PATH, {})
    for p in (bt.LOG_PATH, bt.CONFIG_PATH):
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_render_failures_are_caught(app, monkeypatch, fail_on_callback_errors):
    """The harness must notice an exception inside a draw handler."""
    from test_ui import render

    def broken():
        raise RuntimeError("ring broke")
    monkeypatch.setattr(app, "ring_state", broken)
    render(app)
    assert any("ring broke" in str(e) for e in fail_on_callback_errors)
    fail_on_callback_errors.clear()


def test_chart_is_focusable_and_draws_focus_ring(app, monkeypatch):
    from test_ui import render
    app.stack.set_visible_child_name("journal")
    render(app)
    app.win.set_focus(app.chart)
    assert app.chart.is_focus()
    # a test display has no active window, so stand in for "this window has focus"
    monkeypatch.setattr(app.chart, "has_focus", lambda: True)
    render(app)
