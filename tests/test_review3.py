"""Tests for the fixes from the second review (bugs, risks, UX and nits)."""
import json
import os
import stat
import time
import types

import pytest
from gi.repository import GLib, Gtk

import bell_timer as bt
from conftest import begin, configure, journal, pump, run_for

R = Gtk.ResponseType


# ---------------------------------------------------------------- one copy per journal

def test_second_copy_on_the_same_journal_steps_aside(make_app, dialogs, monkeypatch):
    first = make_app()
    assert first._lock_fd is not None
    second = bt.BellTimer()
    second.set_flags(bt.Gio.ApplicationFlags.NON_UNIQUE)
    monkeypatch.setattr(second, "watch_shutdown", lambda: None)
    dialogs.answer(R.OK)
    second.register(None)
    second.activate()
    assert second.win is None and second._lock_fd is None
    assert dialogs.last["title"] == "Meditation Bell is already open"


def test_finish_never_adds_the_same_meditation_twice(app, clock, dialogs):
    begin(app, clock, minutes=30)
    run_for(app, clock, 300)
    app.checkpoint()
    bt.append_session(json.load(open(bt.CURRENT_PATH)))   # e.g. another copy recovered it
    dialogs.answer(R.OK)
    app.on_stop_clicked(None)
    assert len(journal(bt.DATA_DIR)) == 1


# ---------------------------------------------------------------- laptop sleep in the End dialog

def test_end_dialog_offers_to_leave_out_laptop_sleep(app, clock, dialogs):
    begin(app, clock, infinite=True, interval_min=30)
    run_for(app, clock, 1798)
    clock.sleep(3600)
    app._tick()
    dialogs.answer(R.APPLY)
    app.on_stop_clicked(None)
    assert dialogs.last["buttons"] == ["_Keep meditating", "End _without saving",
                                       "Save 30m, without _sleep", "End and _save"]
    assert "The laptop slept for 1h 00m of that." in dialogs.last["body"]
    rec = journal(bt.DATA_DIR)[-1]
    assert rec["seconds"] == 1800 and rec["sleep_left_out"] is True
    assert rec["asleep_seconds"] == 3600
    assert "1h 00m of laptop sleep left out" in app.status.get_text()


def test_extra_time_dialog_offers_extra_without_sleep(app, clock, dialogs):
    begin(app, clock, minutes=5)
    run_for(app, clock, 298 + 600)
    clock.sleep(1800)
    app._tick()
    dialogs.answer(R.APPLY)
    app.on_stop_clicked(None)
    assert dialogs.last["buttons"][1:] == ["Save _5m", "Save 45m with _extra",
                                           "Save 15m, extra without _sleep"]
    rec = journal(bt.DATA_DIR)[-1]
    assert rec["seconds"] == 900 and rec["extra_seconds"] == 600 and rec["sleep_left_out"]


def test_extra_time_ended_right_after_the_bell_is_a_plain_choice(app, clock, dialogs):
    begin(app, clock, minutes=5)
    run_for(app, clock, 298 + 20)
    dialogs.answer(R.OK)
    app.on_stop_clicked(None)
    assert dialogs.last["buttons"] == ["_Keep meditating", "End and _save"]
    assert journal(bt.DATA_DIR)[-1]["seconds"] == 300


# ---------------------------------------------------------------- bells held while deciding

def test_no_interval_bells_while_end_dialog_is_open(app, clock, dialogs):
    begin(app, clock, infinite=True, interval_min=1)
    run_for(app, clock, 30)

    def think(_dlg):
        run_for(app, clock, 300)
        assert app.events.count("interval") == 0
        return R.CANCEL
    dialogs.answer(think)
    app.on_stop_clicked(None)
    run_for(app, clock, 1)
    assert app.events.count("interval") == 1        # one catch-up bell, not five


def test_saved_end_time_and_bells_are_from_when_the_dialog_opened(app, clock, dialogs, wall):
    begin(app, clock, infinite=True, interval_min=1)
    run_for(app, clock, 130)
    wall.set(bt.dt.datetime(2026, 11, 15, 12, 2, 12))

    def think(_dlg):
        wall.set(bt.dt.datetime(2026, 11, 15, 12, 30))
        return R.OK
    dialogs.answer(think)
    app.on_stop_clicked(None)
    rec = journal(bt.DATA_DIR)[-1]
    assert rec["end"].startswith("2026-11-15T12:02:12") and rec["bells"] == 2


# ---------------------------------------------------------------- logout and shutdown

def test_prepare_for_shutdown_saves(app, clock):
    begin(app, clock, minutes=30)
    run_for(app, clock, 600)
    app.on_prepare_shutdown(None, ":1.1", "/org/freedesktop/login1",
                            "org.freedesktop.login1.Manager", "PrepareForShutdown",
                            GLib.Variant("(b)", (False,)))
    assert app.running                              # False = shutdown cancelled/finished
    app.on_prepare_shutdown(None, ":1.1", "/org/freedesktop/login1",
                            "org.freedesktop.login1.Manager", "PrepareForShutdown",
                            GLib.Variant("(b)", (True,)))
    assert not app.running and journal(bt.DATA_DIR)[-1]["seconds"] == 602


def test_checkpoint_every_ten_seconds(app, clock):
    begin(app, clock, infinite=True)
    run_for(app, clock, 25)
    assert 18 <= json.load(open(bt.CURRENT_PATH))["seconds"] <= 27


def test_logout_saves_settings(app):
    app.sp_mins.set_value(33)
    app.on_signal()
    assert bt.load_json(bt.CONFIG_PATH, {})["minutes"] == 33


def test_logout_closes_open_dialogs(app, clock):
    """A dialog's run() has its own loop; quit() alone left the app hanging."""
    closed = []
    fake = types.SimpleNamespace(response=closed.append)
    app._dialogs.append(fake)
    app.on_signal()
    assert closed == [R.DELETE_EVENT]


# ---------------------------------------------------------------- start-up robustness

def test_meditation_keeps_ticking_if_a_start_step_fails(app, clock, monkeypatch,
                                                         fail_on_callback_errors):
    def broken():
        raise RuntimeError("logind went away")
    monkeypatch.setattr(app, "refresh_awake", broken)
    configure(app, minutes=5)
    app.on_start_pause(None)
    assert app.running and app.tick_id is not None
    assert GLib.MainContext.default().find_source_by_id(app.tick_id) is not None
    assert [str(e) for e in fail_on_callback_errors] == ["logind went away"]
    fail_on_callback_errors.clear()


def test_error_at_the_end_of_warmup_is_reported(app, clock, monkeypatch,
                                                fail_on_callback_errors):
    def broken():
        raise RuntimeError("bell player broke")
    configure(app, minutes=5, warmup=5)
    app.on_start_pause(None)
    monkeypatch.setattr(app, "begin_meditation", broken)
    run_for(app, clock, 6)
    assert [str(e) for e in fail_on_callback_errors] == ["bell player broke"]
    fail_on_callback_errors.clear()


def test_real_timer_source_lifecycle(app, clock, dialogs):
    configure(app, minutes=5)
    app.on_start_pause(None)
    tid = app.tick_id
    ctx = GLib.MainContext.default()
    assert ctx.find_source_by_id(tid) is not None
    ticks = []
    real = app._tick
    app._tick = lambda: (ticks.append(1), real())
    end = time.time() + 0.8
    while time.time() < end:        # the real 250 ms GLib timer drives the ticks
        ctx.iteration(False)
        time.sleep(0.01)
    assert 2 <= len(ticks) <= 5
    dialogs.answer(R.REJECT)
    clock.advance(2)
    app.on_stop_clicked(None)
    assert app.tick_id is None and ctx.find_source_by_id(tid) is None


def test_bad_config_values_fall_back_to_defaults(make_app):
    open(bt.CONFIG_PATH, "w").write(json.dumps(
        {"hours": "abc", "minutes": 500, "interval_min": None, "warmup_sec": -3,
         "infinite": "yes", "start_bell": 0}))
    app = make_app()
    assert app.sp_hours.get_value() == 1 and app.sp_mins.get_value() == 59
    assert app.sp_int.get_value() == 30 and app.sp_warm.get_value() == 0
    assert not app.sw_inf.get_active() and app.sw_start.get_active()


def test_existing_data_files_are_made_private(make_app):
    for p in (bt.LOG_PATH, bt.CONFIG_PATH):
        with open(p, "w") as f:
            f.write("{}\n")
        os.chmod(p, 0o664)
    make_app()
    for p in (bt.LOG_PATH, bt.CONFIG_PATH):
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_backup_is_private(app, dialogs):
    bt.append_session({"start": "2026-11-15T07:00:00-05:00", "seconds": 900})
    app.refresh_stats()
    app.tree.get_selection().select_path(Gtk.TreePath(0))
    dialogs.answer(R.OK)
    app.on_delete_selected(None)
    assert stat.S_IMODE(os.stat(bt.LOG_PATH + ".bak").st_mode) == 0o600


# ---------------------------------------------------------------- keep-awake needs

def test_no_lock_when_no_bell_can_ring(app, clock):
    begin(app, clock, infinite=True, interval_min=0)
    run_for(app, clock, 60)
    assert "inhibit_on" not in app.events and not app.idle_blocked


def test_lock_released_while_paused_and_taken_again(app, clock):
    begin(app, clock, minutes=30, interval_min=5)
    assert app.events.count("inhibit_on") == 1 and app.idle_blocked
    app.on_start_pause(None)
    run_for(app, clock, 10)
    assert app.events.count("lock_off") == 1 and not app.idle_blocked
    app.on_start_pause(None)
    run_for(app, clock, 10)
    assert app.events.count("inhibit_on") == 2 and app.idle_blocked


def test_audio_check_never_blocks(app, monkeypatch):
    monkeypatch.setattr(bt, "AUDIO_SINK_CMD", ["sleep", "30"])
    t0 = time.monotonic()
    bt.BellTimer.check_audio_route(app)
    assert time.monotonic() - t0 < 0.5
    end = time.time() + 5
    while app._audio_proc is not None and time.time() < end:
        GLib.MainContext.default().iteration(False)
        time.sleep(0.02)
    assert app._audio_proc is None and app.audio_on_display is True   # gave up: play safe


@pytest.mark.parametrize("sink,on_display", [
    ("alsa_output.pci-0000_00_1f.3.hdmi-stereo", True),
    ("alsa_output.pci-0000_00_1f.3.analog-stereo", False),
])
def test_audio_route_detection(app, monkeypatch, sink, on_display):
    monkeypatch.setattr(bt, "AUDIO_SINK_CMD", ["echo", sink])
    app.audio_on_display = None
    bt.BellTimer.check_audio_route(app)
    end = time.time() + 5
    while app._audio_proc is not None and time.time() < end:
        GLib.MainContext.default().iteration(False)
        time.sleep(0.01)
    assert app.audio_on_display is on_display


# ---------------------------------------------------------------- bell player watchdog

def _fake_player(state):
    return types.SimpleNamespace(get_state=lambda timeout: (None, state, None))


def test_player_that_never_starts_is_retried(monkeypatch):
    bell = bt.Bell(bt.BELL_PATH, bt.START_BELL_PATH, bt.END_BELL_PATH)
    failures = []
    monkeypatch.setattr(bell, "_failed", lambda p, uri, retry, err: failures.append(err))
    p = _fake_player(bt.Gst.State.PAUSED)
    bell.players.append(p)
    bell._check_started(p, bell.uri, True)
    assert failures == ["it didn’t start playing"]


def test_player_that_never_finishes_is_cleaned_up(monkeypatch):
    bell = bt.Bell(bt.BELL_PATH, bt.START_BELL_PATH, bt.END_BELL_PATH)
    cleaned = []
    monkeypatch.setattr(bell, "_cleanup", cleaned.append)
    p = _fake_player(bt.Gst.State.PLAYING)
    bell.players.append(p)
    bell._check_started(p, bell.uri, True)        # playing: fine, check again later
    bell._check_finished(p)
    assert cleaned == [p]


# ---------------------------------------------------------------- journal page

def test_selection_survives_updates_during_a_meditation(app, clock, wall):
    bt.append_session({"start": "2026-11-14T07:00:00-05:00", "seconds": 900})
    app.refresh_stats()
    app.stack.set_visible_child_name("journal")
    app.tree.get_selection().select_path(Gtk.TreePath(0))
    begin(app, clock, infinite=True)
    run_for(app, clock, 180)
    model, it = app.tree.get_selection().get_selected()
    assert it is not None
    assert app.stat_labels["today"][0].get_text() == "3m"


def test_today_moves_on_at_midnight(app, wall):
    wall.set(bt.dt.datetime(2026, 11, 15, 23, 59))
    bt.append_session({"start": "2026-11-15T22:00:00-05:00", "seconds": 600})
    app._today = bt.dt.date.today()
    app.refresh_stats()
    assert app.stat_labels["today"][0].get_text() == "10m"
    wall.set(bt.dt.datetime(2026, 11, 16, 0, 1))
    app._watch_day()
    assert app.stat_labels["today"][0].get_text() == "0m"


def test_dimmed_clock_reads_in_minutes(app, clock):
    begin(app, clock, infinite=True)
    run_for(app, clock, 45 * 60, step=5)
    assert app.dimmed and app.clock.get_text() == "45m"
    run_for(app, clock, 20 * 60, step=5)
    assert app.clock.get_text() == "1h 05m"
