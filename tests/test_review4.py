"""Tests for the fixes from the third review."""
import io
import json
import os
import sys
import types

import pytest
from gi.repository import GLib, Gtk

import bell_timer as bt
from conftest import begin, configure, journal, pump, run_for

R = Gtk.ResponseType


# ---------------------------------------------------------------- sleep and the End dialog

def test_sleep_past_the_planned_end_can_be_left_out(app, clock, dialogs):
    """60 min planned, 30 awake, then 40 asleep: only 30 real minutes were sat."""
    begin(app, clock, hours=1)
    run_for(app, clock, 1798)
    clock.sleep(2400)
    app._tick()
    assert app.overtime
    run_for(app, clock, 120)
    dialogs.answer(R.APPLY)
    app.on_stop_clicked(None)
    assert "Save 32m, without s_leep" in dialogs.last["buttons"]
    rec = journal(bt.DATA_DIR)[-1]
    assert rec["seconds"] == 1920 and rec["completed"] is False and rec["sleep_left_out"]


def test_sleep_option_right_after_the_closing_bell(app, clock, dialogs):
    begin(app, clock, hours=1)
    run_for(app, clock, 1798)
    clock.sleep(1830)
    app._tick()
    dialogs.answer(R.APPLY)
    app.on_stop_clicked(None)
    assert dialogs.last["buttons"] == ["_Keep meditating", "Save 30m, without s_leep",
                                       "End and _save"]
    assert journal(bt.DATA_DIR)[-1]["seconds"] == 1800


def test_without_sleep_matches_the_dialog_that_was_answered(app, clock, dialogs):
    """Sleep while the dialog is open makes it stale: it's asked again, and
    "without sleep" then uses the sleep that second dialog shows."""
    begin(app, clock, infinite=True)
    run_for(app, clock, 1798)
    clock.sleep(600)
    app._tick()

    def sleeps_again(_dlg):
        clock.sleep(1200)
        app._tick()
        return R.APPLY
    dialogs.answer(sleeps_again, R.APPLY)
    app.on_stop_clicked(None)
    assert "Save 30m, without s_leep" in dialogs.seen[0]["buttons"]
    assert "The laptop slept for 30m of that." in dialogs.seen[1]["body"]
    rec = journal(bt.DATA_DIR)[-1]
    assert rec["seconds"] == 1800 and rec["asleep_seconds"] == 1800


def test_without_sleep_uses_the_snapshot_when_answered_promptly(app, clock, dialogs):
    begin(app, clock, infinite=True)
    run_for(app, clock, 1798)
    clock.sleep(600)
    app._tick()
    app.asleep += 0.4                # sub-second drift isn't "stale"

    def prompt(_dlg):
        run_for(app, clock, 10)
        return R.APPLY
    dialogs.answer(prompt)
    app.on_stop_clicked(None)
    assert len(dialogs.seen) == 1 and journal(bt.DATA_DIR)[-1]["seconds"] == 1800


@pytest.mark.parametrize("setup", ["early", "early_sleep", "extra", "extra_sleep", "just_rang"])
def test_every_end_dialog_has_unique_access_keys(app, clock, dialogs, setup):
    if setup.startswith("early"):
        begin(app, clock, infinite=True)
        run_for(app, clock, 600)
    else:
        begin(app, clock, minutes=5)
        run_for(app, clock, 298 + (20 if setup == "just_rang" else 600))
    if setup.endswith("sleep"):
        clock.sleep(300)
        app._tick()
    for quitting in (False, True):
        dialogs.answer(R.CANCEL)
        app.ask_end("End?", quitting=quitting)
        keys = [b[b.index("_") + 1].lower() for b in dialogs.last["buttons"]]
        assert len(keys) == len(set(keys)), dialogs.last["buttons"]
        assert all(k.isalpha() for k in keys), dialogs.last["buttons"]


def test_window_close_while_end_dialog_is_open_keeps_meditating(app, clock, dialogs):
    begin(app, clock, infinite=True, interval_min=1)
    run_for(app, clock, 10)

    def close_requested(_dlg):
        assert app.on_close() is True    # nested request: just keep going
        assert app.asking
        return R.CANCEL
    dialogs.answer(close_requested)
    app.on_stop_clicked(None)
    assert app.running and len(dialogs.seen) == 1 and not app.asking


# ---------------------------------------------------------------- journal robustness

def test_journal_in_another_encoding_doesnt_stop_the_app(make_app, clock, dialogs):
    raw = (b'{"start": "2026-11-14T07:00:00-05:00", "seconds": 600}\n'
           b'note: caf\xe9 retreat\n')
    with open(bt.LOG_PATH, "wb") as f:
        f.write(raw)
    app = make_app()
    assert app.stat_labels["total"][0].get_text() == "10m"
    begin(app, clock, minutes=30)
    run_for(app, clock, 300)
    dialogs.answer(R.OK)
    app.on_stop_clicked(None)
    assert len(journal_lines := open(bt.LOG_PATH, "rb").read().splitlines()) == 3
    assert journal_lines[1] == b"note: caf\xe9 retreat"
    # deleting a meditation keeps the odd line byte for byte
    app.refresh_stats()
    app.tree.get_selection().select_path(Gtk.TreePath(0))
    dialogs.answer(R.OK)
    app.on_delete_selected(None)
    assert open(bt.LOG_PATH, "rb").read().splitlines()[1] == b"note: caf\xe9 retreat"


def test_absurd_numbers_in_journal_and_config(make_app):
    with open(bt.LOG_PATH, "w") as f:
        f.write('{"start": "2026-11-14T07:00:00-05:00", "seconds": 1e999}\n')
        f.write('{"start": "2026-11-15T07:00:00-05:00", "seconds": 9e15, "planned": 1e999}\n')
    with open(bt.CONFIG_PATH, "w") as f:
        f.write('{"hours": 1e999, "minutes": -1e999}')
    app = make_app()
    sessions, junk = bt.load_sessions()
    assert len(junk) == 1 and sessions[0]["seconds"] == bt.MAX_RECORD_SECONDS
    assert sessions[0]["planned"] is None
    assert app.sp_hours.get_value() == 1 and app.sp_mins.get_value() == 0


def test_second_bad_checkpoint_doesnt_overwrite_the_first(make_app):
    for text in ("{broken one", "{broken two"):
        open(bt.CURRENT_PATH, "w").write(text)
        make_app()
    bads = sorted(f for f in os.listdir(bt.DATA_DIR) if f.endswith(".bad"))
    contents = sorted(open(os.path.join(bt.DATA_DIR, f)).read() for f in bads)
    assert contents == ["{broken one", "{broken two"]


def test_rewrite_keeps_blank_lines():
    with open(bt.LOG_PATH, "w") as f:
        f.write('{"start": "2026-11-14T07:00:00-05:00", "seconds": 600}\n\n'
                '{"start": "2026-11-15T07:00:00-05:00", "seconds": 300}\n')
    sessions, junk = bt.load_sessions()
    bt.rewrite_sessions(sessions[1:], junk)
    assert open(bt.LOG_PATH).read() == '\n{"start": "2026-11-15T07:00:00-05:00", "seconds": 300}\n'


# ---------------------------------------------------------------- carrying on after a crash

def _crash(make_app, clock, **settings):
    app = make_app()
    begin(app, clock, **settings)
    return app


def test_recent_crash_offers_to_carry_on(make_app, clock, dialogs):
    app = _crash(make_app, clock, hours=1, interval_min=10)
    run_for(app, clock, 590)                    # 9m 52s in: no bell yet
    app.checkpoint()
    app.running = False                         # the process died here
    clock.advance(130)                          # the desktop took two minutes to come back
    dialogs.answer(R.OK)
    again = make_app()
    pump()
    assert dialogs.last["title"] == "Carry on with your meditation?"
    assert "0:12:02 in" in dialogs.last["body"]
    assert again.running and again.elapsed() == pytest.approx(722)
    assert again.target == 3600 and again.interval == 600 and again.bells_rung == 0
    assert "start" not in again.events          # no second opening bell
    run_for(again, clock, 1)
    assert again.events.count("interval") == 1  # the bell missed at 10 min, once
    assert journal(bt.DATA_DIR) == []


def test_carry_on_keeps_pause_and_extra_time(make_app, clock, dialogs):
    app = _crash(make_app, clock, minutes=5)
    run_for(app, clock, 298 + 100)
    app.on_user_input()
    app.on_start_pause(None)                    # paused in extra time
    app.running = False
    clock.advance(60)
    dialogs.answer(R.OK)
    again = make_app()
    pump()
    assert again.overtime and again.paused and again.elapsed() == pytest.approx(400)
    assert "end" not in again.events


def test_old_or_other_boot_crash_is_just_recorded(make_app, clock, dialogs, monkeypatch):
    app = _crash(make_app, clock, infinite=True)
    run_for(app, clock, 600)
    app.checkpoint()
    app.running = False
    clock.advance(bt.RESUME_WITHIN + 60)        # too long ago
    again = make_app()
    pump()
    assert not again.running and journal(bt.DATA_DIR)[-1]["seconds"] == 602
    assert dialogs.seen == []
    assert not any(k.startswith("_") for k in journal(bt.DATA_DIR)[-1])


def test_crash_from_a_previous_boot_is_just_recorded(make_app, clock, dialogs, monkeypatch):
    app = _crash(make_app, clock, infinite=True)
    run_for(app, clock, 600)
    app.checkpoint()
    app.running = False
    monkeypatch.setattr(bt, "boot_id", lambda: "another-boot")
    again = make_app()
    pump()
    assert not again.running and dialogs.seen == [] and len(journal(bt.DATA_DIR)) == 1


# ---------------------------------------------------------------- reporting and shutdown

def test_bell_failure_after_terminal_closed_still_retries(monkeypatch):
    class DeadTerminal(io.StringIO):
        def write(self, *_):
            raise OSError(5, "Input/output error")
    monkeypatch.setattr(sys, "stderr", DeadTerminal())
    bell = bt.Bell(bt.BELL_PATH, bt.START_BELL_PATH, bt.END_BELL_PATH)
    scheduled = []
    monkeypatch.setattr(bt.GLib, "timeout_add", lambda ms, fn, *a: scheduled.append(ms))
    p = types.SimpleNamespace(get_bus=lambda: types.SimpleNamespace(), set_state=lambda s: None,
                              _hid=None)
    bell.players.append(p)
    bell._failed(p, bell.uri, True, "sink went away")
    assert scheduled == [300] and p not in bell.players


def test_shutdown_closes_open_dialogs(app, clock):
    closed = []
    app._dialogs.append(types.SimpleNamespace(response=closed.append))
    begin(app, clock, minutes=30)
    app.on_prepare_shutdown(GLib.Variant("(b)", (True,)))
    assert closed == [R.DELETE_EVENT] and not app.running


def test_refused_begin_beeps(app, monkeypatch):
    beeps = []
    monkeypatch.setattr(app, "beep", lambda: beeps.append(1))
    configure(app, minutes=0, hours=0)
    app.on_start_pause(None)
    assert beeps == [1] and not app.running


def test_slow_sink_gets_more_time_before_retry(monkeypatch):
    bell = bt.Bell(bt.BELL_PATH, bt.START_BELL_PATH, bt.END_BELL_PATH)
    failures, rechecks = [], []
    monkeypatch.setattr(bell, "_failed", lambda *a: failures.append(1))
    monkeypatch.setattr(bt.GLib, "timeout_add_seconds", lambda s, fn, *a: rechecks.append(a))
    Gst = bt.Gst
    p = types.SimpleNamespace(get_state=lambda t: (Gst.StateChangeReturn.ASYNC,
                                                   Gst.State.PAUSED, Gst.State.PLAYING))
    bell.players.append(p)
    bell._check_started(p, bell.uri, True)
    assert failures == [] and len(rechecks) == 1          # still waking: checked again
    bell._check_started(p, bell.uri, True, bt.PLAYER_START_LIMIT)
    assert failures == [1]                                # waited long enough: give up


def test_interval_bell_starts_with_a_moment_of_silence():
    import wave
    w = wave.open(bt.BELL_PATH)
    lead = w.readframes(int(w.getframerate() * 0.1))
    assert set(lead) == {0}


# ---------------------------------------------------------------- add by hand

def _fill(date, time_, hours, minutes):
    from test_saving import _fill_manual
    return _fill_manual(date, time_, hours, minutes)


def test_add_by_hand_refuses_the_future(app, dialogs, wall):
    dialogs.answer(_fill("2026-11-16", "07:00", 0, 30), R.CANCEL)
    app.on_add_manual(None)
    assert journal(bt.DATA_DIR) == []


def test_add_by_hand_refuses_a_duplicate_start(app, dialogs, wall):
    bt.append_session({"start": bt.dt.datetime(2026, 11, 14, 7, 0).astimezone()
                       .isoformat(timespec="seconds"), "seconds": 600})
    dialogs.answer(_fill("2026-11-14", "07:00", 0, 30), R.CANCEL)
    app.on_add_manual(None)
    assert len(journal(bt.DATA_DIR)) == 1


def test_under_a_minute_message_says_saved(app, clock, dialogs):
    begin(app, clock, minutes=30)
    run_for(app, clock, 20)
    app.finish("save")
    assert app.status.get_text() == "Under a minute, so it wasn’t saved"
