"""Tests for the fixes from the fifth review."""
import io
import json
import os
import wave

import pytest
from gi.repository import Atk, GLib, Gtk

import bell_timer as bt
from conftest import begin, journal, pump, run_for

R = Gtk.ResponseType


def _crash(app):
    app.checkpoint()
    app.running = False             # the process dies here


# ---------------------------------------------------------------- bells come first

def test_a_keep_awake_error_doesnt_silence_the_bells(app, clock, monkeypatch,
                                                      fail_on_callback_errors):
    begin(app, clock, minutes=30, interval_min=5)

    def broken():
        raise RuntimeError("keep-awake broke")
    monkeypatch.setattr(app, "refresh_awake", broken)
    reported = []
    monkeypatch.setattr(bt, "report_exception", lambda: reported.append(1))
    run_for(app, clock, 600)
    assert app.events.count("interval") == 2 and reported


def test_an_unreadable_idle_setting_plays_safe(app, tmp_path, monkeypatch):
    d = tmp_path / "idle"
    d.mkdir()
    for key in ("suspend_on_ac_time", "suspend_on_battery_time"):
        (d / key).write_bytes(b"\xff\xfe not utf-8")
    monkeypatch.setattr(bt, "COSMIC_IDLE_DIR", str(d))
    app.audio_on_display = False
    # the fixture stubs this out; test the real one
    assert bt.BellTimer.idle_would_stop_bells(app) is True


# ---------------------------------------------------------------- paused, then a crash

def test_checkpoints_continue_while_paused(make_app, clock, dialogs):
    app = make_app()
    begin(app, clock, hours=1)
    run_for(app, clock, 300)
    app.on_start_pause(None)                    # pause
    run_for(app, clock, 20 * 60)                # longer than the carry-on window
    app.running = False                         # crash while paused
    dialogs.answer(R.OK)
    again = make_app()
    pump()
    assert dialogs.last["title"] == "Carry on with your meditation?"
    assert again.running and again.paused and again.elapsed() == pytest.approx(302)


def test_paused_checkpoints_are_not_every_tick(app, clock, monkeypatch):
    begin(app, clock, hours=1)
    app.on_start_pause(None)
    writes = []
    real = app.checkpoint
    monkeypatch.setattr(app, "checkpoint", lambda: (writes.append(1), real()))
    run_for(app, clock, 600)
    assert 9 <= len(writes) <= 11               # about once a minute


def test_sleep_after_a_crash_is_noted_on_carrying_on(make_app, clock, dialogs):
    app = make_app()
    begin(app, clock, hours=1)
    run_for(app, clock, 300)
    _crash(app)
    clock.sleep(240)                            # laptop slept before reopening
    clock.advance(10)
    dialogs.answer(R.OK)
    again = make_app()
    pump()
    assert again.running and again.asleep == pytest.approx(240)
    assert again.elapsed() == pytest.approx(302 + 250)   # counted, as in a live sleep


def test_an_older_checkpoint_without_awake_still_carries_on(make_app, clock, dialogs):
    app = make_app()
    begin(app, clock, hours=1)
    run_for(app, clock, 300)
    _crash(app)
    rec = json.load(open(bt.CURRENT_PATH))
    del rec["_resume"]["awake"]
    bt.atomic_write_json(bt.CURRENT_PATH, rec)
    dialogs.answer(R.OK)
    again = make_app()
    pump()
    assert again.running and again.asleep == 0


# ---------------------------------------------------------------- carry-on wording

def test_carry_on_under_a_minute_doesnt_offer_to_save(make_app, clock, dialogs):
    app = make_app()
    begin(app, clock, hours=1)
    run_for(app, clock, 30)
    _crash(app)
    dialogs.answer(R.REJECT)
    make_app()
    pump()
    assert "_End (under a minute isn’t saved)" in dialogs.last["buttons"]
    assert "under a minute isn’t saved" in dialogs.last["body"]
    assert journal(bt.DATA_DIR) == [] and not os.path.exists(bt.CURRENT_PATH)


def test_carry_on_in_extra_time_names_the_planned_length(make_app, clock, dialogs):
    app = make_app()
    begin(app, clock, minutes=10)
    run_for(app, clock, 15 * 60)                # 5 minutes into extra time
    _crash(app)
    dialogs.answer(R.REJECT)
    make_app()
    pump()
    assert "_Save the planned 10m" in dialogs.last["buttons"]
    assert "planned 10m to your journal (without the extra time)" in dialogs.last["body"]
    rec = journal(bt.DATA_DIR)[-1]
    assert rec["seconds"] == 600 and rec["unsaved_extra_seconds"] >= 290


# ---------------------------------------------------------------- End dialog while paused

def test_paused_end_dialog_isnt_asked_twice(app, clock, dialogs):
    begin(app, clock, minutes=30)
    run_for(app, clock, 300)
    app.on_start_pause(None)

    def think(_dlg):
        run_for(app, clock, 5 * 60)             # a long think, still paused
        return R.OK
    dialogs.answer(think)
    app.on_stop_clicked(None)
    assert len(dialogs.seen) == 1 and journal(bt.DATA_DIR)[-1]["seconds"] == 302


def test_running_end_dialog_left_open_is_still_asked_again(app, clock, dialogs):
    begin(app, clock, minutes=30)
    run_for(app, clock, 300)

    def think(_dlg):
        run_for(app, clock, 90)
        return R.OK
    dialogs.answer(think, R.OK)
    app.on_stop_clicked(None)
    assert len(dialogs.seen) == 2 and journal(bt.DATA_DIR)[-1]["seconds"] == 392


# ---------------------------------------------------------------- journal page

def test_add_by_hand_refuses_a_year_the_journal_would_ignore(app, dialogs, wall):
    from test_saving import _fill_manual
    dialogs.answer(_fill_manual("1026-11-14", "07:00", 0, 30), R.CANCEL)
    app.on_add_manual(None)
    assert journal(bt.DATA_DIR) == [] and len(dialogs.seen) == 2


def test_stat_tile_is_read_title_first_and_once(app):
    bt.append_session({"start": bt.dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                       "seconds": 3600})
    app.refresh_stats()
    lbl, title = app.stat_labels["today"]
    assert lbl.get_accessible().get_name() == "Today: 1 hour"
    shown_title = lbl.get_parent().get_children()[1]
    assert shown_title.get_text() == "Today"
    assert shown_title.get_accessible().get_role() == Atk.Role.REDUNDANT_OBJECT


def test_deleting_a_row_that_changed_leaves_the_file_alone(app, dialogs):
    bt.append_session({"start": "2026-11-15T07:00:00-05:00", "seconds": 900})
    app.refresh_stats()
    app.tree.get_selection().select_path(Gtk.TreePath(0))
    with open(bt.LOG_PATH, "w") as f:           # changed while the dialog was open
        f.write('{"start": "2026-11-15T07:00:00-05:00", "seconds": 1200}\n')
    before = open(bt.LOG_PATH, "rb").read()
    dialogs.answer(R.OK)
    app.on_delete_selected(None)
    assert open(bt.LOG_PATH, "rb").read() == before
    assert not os.path.exists(bt.LOG_PATH + ".bak")
    assert "nothing deleted" in app.status.get_text()


# ---------------------------------------------------------------- shutdown cancelled

def test_cancelled_shutdown_doesnt_end_the_meditation(app, clock):
    begin(app, clock, minutes=30)
    run_for(app, clock, 300)
    app.on_prepare_shutdown(GLib.Variant("(b)", (True,)))
    app.on_prepare_shutdown(GLib.Variant("(b)", (False,)))   # cancelled
    assert app.running
    run_for(app, clock, 300)
    assert app.elapsed() == pytest.approx(602)


def test_killed_after_prepare_for_shutdown_is_saved_next_launch(make_app, clock):
    app = make_app()
    begin(app, clock, minutes=30)
    run_for(app, clock, 305)                    # just after a timed checkpoint
    app.on_prepare_shutdown(GLib.Variant("(b)", (True,)))
    app.running = False                         # killed without a SIGTERM
    app.release_lock()
    clock.advance(bt.RESUME_WITHIN + 1)         # much later: too late to carry on
    make_app()
    pump()
    rec = journal(bt.DATA_DIR)[-1]
    assert rec["seconds"] == 307 and rec["recovered"] is True


# ---------------------------------------------------------------- refusal tone

def test_refusal_tone_fades_in_and_out():
    w = wave.open(io.BytesIO(bt.beep_wav_bytes()))
    frames = w.readframes(w.getnframes())
    first = int.from_bytes(frames[2:4], "little", signed=True)
    last = int.from_bytes(frames[-2:], "little", signed=True)
    assert w.getnchannels() == 1 and w.getframerate() == 44100
    assert abs(first) < 400 and abs(last) < 400          # no click at either end


def test_refusal_tone_is_heard_at_zero_volume(monkeypatch):
    bell = bt.Bell(bt.BELL_PATH, bt.START_BELL_PATH, bt.END_BELL_PATH)
    bell.volume = 0.0
    played = []
    monkeypatch.setattr(bell, "_play_once", lambda uri, **kw: played.append(kw))
    bell.beep()
    assert played[0]["volume"] >= 0.4 and played[0]["retry"] is False
