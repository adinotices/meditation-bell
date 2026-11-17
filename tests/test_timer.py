"""Integration tests: the timer, bells, pause, warm-up, extra time and sleep
detection, driven through the real app window with a fake clock."""
import pytest

import bell_timer as bt
from conftest import begin, configure, journal, run_for


def bells(app):
    return [e for e in app.events if e in ("start", "interval", "end")]


# ---------------------------------------------------------------- schedule

def test_timed_meditation_bell_schedule(app, clock):
    begin(app, clock, minutes=5, interval_min=1)
    run_for(app, clock, 5 * 60)
    assert bells(app) == ["start", "interval", "interval", "interval", "interval", "end"]
    assert app.bells_rung == 4


def test_no_interval_bell_on_the_closing_bell(app, clock):
    begin(app, clock, minutes=4, interval_min=2)
    run_for(app, clock, 4 * 60 + 5)
    assert bells(app) == ["start", "interval", "end"]


def test_opening_bell_can_be_turned_off(app, clock):
    begin(app, clock, minutes=1, start_bell=False)
    assert "start" not in app.events


def test_interval_bell_timing_is_on_active_time(app, clock):
    begin(app, clock, minutes=10, interval_min=2)          # 2 s already elapsed
    run_for(app, clock, 117)
    assert bells(app) == ["start"]
    run_for(app, clock, 2)
    assert bells(app) == ["start", "interval"]


def test_infinite_with_intervals_runs_for_hours(app, clock):
    begin(app, clock, infinite=True, interval_min=30)
    run_for(app, clock, 10 * 3600, step=5)
    assert app.running and not app.overtime
    assert bells(app).count("interval") == 20 and "end" not in app.events
    assert app.ring_state()[0] == pytest.approx((app.elapsed() % 1800) / 1800)


def test_ten_hour_timed_meditation(app, clock):
    begin(app, clock, hours=10, interval_min=45)
    run_for(app, clock, 10 * 3600, step=5)
    assert app.overtime and app.events.count("end") == 1
    assert bells(app).count("interval") == 13   # 45 min marks before 10 h, not at 10 h


def test_skipped_interval_bells_ring_once(app, clock):
    begin(app, clock, infinite=True, interval_min=1)
    clock.advance(5 * 60)       # e.g. the process was stopped
    app._tick()
    assert bells(app).count("interval") == 1
    run_for(app, clock, 61)
    assert bells(app).count("interval") == 2


# ---------------------------------------------------------------- pause

def test_pause_excludes_time_and_delays_bells(app, clock):
    begin(app, clock, minutes=5, interval_min=1)
    run_for(app, clock, 30)
    app.on_start_pause(None)                     # pause
    assert app.paused and app.btn_start.get_label() == "_Resume"
    run_for(app, clock, 600)
    assert app.elapsed() == pytest.approx(32)
    assert bells(app) == ["start"]
    app.on_start_pause(None)                     # resume
    run_for(app, clock, 28)
    assert bells(app) == ["start", "interval"]


def test_double_click_on_begin_does_not_pause(app, clock):
    configure(app, minutes=5)
    app.on_start_pause(None)
    clock.advance(0.3)
    app.on_start_pause(None)
    assert app.running and not app.paused


# ---------------------------------------------------------------- start checks

def test_zero_length_is_refused(app, clock):
    configure(app, minutes=0, hours=0)
    app.on_start_pause(None)
    assert not app.running and "Set a length" in app.status.get_text()


def test_zero_volume_is_refused(app, clock):
    configure(app, minutes=5)
    app.vol.set_value(0)
    app.on_start_pause(None)
    assert not app.running and "Volume is at zero" in app.status.get_text()


def test_start_takes_sleep_lock_and_finish_releases_it(app, clock, dialogs):
    begin(app, clock, minutes=5)
    assert app.events.count("inhibit_on") == 1
    dialogs.answer(bt.Gtk.ResponseType.REJECT)
    app.on_stop_clicked(None)
    assert app.events.count("inhibit_off") == 1


# ---------------------------------------------------------------- warm-up

def test_warmup_counts_down_then_rings_opening_bell(app, clock):
    configure(app, minutes=5, warmup=15)
    app.on_start_pause(None)
    assert app.warming and not app.running
    assert app.phase.get_text() == "WARM UP" and app.clock.get_text() == "15"
    assert app.btn_start.get_label() == "_Start now" and app.btn_stop.get_label() == "_Cancel"
    run_for(app, clock, 10)
    assert app.clock.get_text() == "5" and "start" not in app.events
    run_for(app, clock, 5.2, step=0.2)
    assert app.running and not app.warming
    assert app.events.count("start") == 1
    assert app.elapsed() < 1       # warm-up time is not counted


def test_warmup_cancel_returns_to_ready(app, clock):
    configure(app, minutes=5, warmup=15)
    app.on_start_pause(None)
    clock.advance(3)
    app.on_stop_clicked(None)
    run_for(app, clock, 30)
    assert not app.running and not app.warming and app.events == []
    assert app.phase.get_text() == "READY"


def test_warmup_start_now(app, clock):
    configure(app, minutes=5, warmup=60)
    app.on_start_pause(None)
    clock.advance(3)
    app.on_start_pause(None)
    assert app.running and app.events.count("start") == 1


def test_zero_warmup_starts_immediately(app, clock):
    configure(app, minutes=5, warmup=0)
    app.on_start_pause(None)
    assert app.running and app.events.count("start") == 1


def test_closing_during_warmup_just_closes(app, clock, dialogs):
    configure(app, minutes=5, warmup=15)
    app.on_start_pause(None)
    assert app.on_close() is False and not app.warming
    assert dialogs.seen == [] and journal(bt.DATA_DIR) == []


# ---------------------------------------------------------------- extra time

def test_planned_end_starts_extra_time(app, clock):
    begin(app, clock, minutes=5, interval_min=1)
    run_for(app, clock, 5 * 60 - 2 + 150)
    app.on_user_input()            # you open your eyes and move the mouse
    assert app.running and app.overtime
    assert app.events.count("end") == 1
    assert bells(app).count("interval") == 4          # none during extra time
    assert app.clock.get_text() == "+2:30"
    assert app.clock_sub.get_text() == "5m complete · total 0:07:30"
    assert app.phase.get_text() == "EXTRA TIME"
    assert app.ring_state() == (1.0, [])


def test_extra_time_over_an_hour_shows_hours(app, clock):
    begin(app, clock, minutes=1)
    run_for(app, clock, 58 + 3700, step=5)
    app.on_user_input()
    assert app.clock.get_text() == "+1:01:40"


def test_extra_time_can_be_paused(app, clock):
    begin(app, clock, minutes=1)
    run_for(app, clock, 58 + 30)
    app.on_user_input()            # the click on Pause
    app.on_start_pause(None)
    run_for(app, clock, 600)
    assert app.clock.get_text() == "+0:30" and app.phase.get_text() == "PAUSED"


def test_extra_time_dimmed_view(app, clock):
    begin(app, clock, minutes=5)
    run_for(app, clock, 298 + 150)
    assert app.dimmed                      # no input for 30 s
    assert app.clock.get_text() == "+2m" and app.clock_sub.get_text() == "5m complete · total 7m"
    app.on_user_input()
    assert not app.dimmed and app.clock.get_text() == "+2:30"


# ---------------------------------------------------------------- dimming

def test_dims_after_quiet_and_wakes_on_input(app, clock):
    begin(app, clock, minutes=60, interval_min=10)
    run_for(app, clock, bt.DIM_AFTER - 5)
    assert not app.dimmed
    run_for(app, clock, 10)
    assert app.dimmed and app.clock.get_text() == "0:00"
    assert app.clock_sub.get_text() == "next bell in 10 minutes"
    app.on_user_input()
    assert not app.dimmed and app.clock.get_text() == "0:00:37"


def test_paused_view_does_not_dim(app, clock):
    begin(app, clock, minutes=60)
    app.on_start_pause(None)
    run_for(app, clock, 120)
    assert not app.dimmed


# ---------------------------------------------------------------- laptop sleep

def test_laptop_sleep_is_detected_and_recorded(app, clock, dialogs):
    begin(app, clock, infinite=True, interval_min=30)
    run_for(app, clock, 60)
    clock.sleep(3600)            # suspended for an hour: no ticks
    app._tick()
    assert app.asleep == pytest.approx(3600, abs=2)
    assert app.status.get_text().startswith("Note: The laptop slept for 1h")
    dialogs.answer(bt.Gtk.ResponseType.OK)
    app.on_stop_clicked(None)
    rec = journal(bt.DATA_DIR)[-1]
    assert rec["asleep_seconds"] == pytest.approx(3600, abs=2)
    assert "the laptop slept for 1h" in app.status.get_text()


def test_paused_gap_is_not_reported_as_sleep(app, clock):
    begin(app, clock, infinite=True)
    app.on_start_pause(None)
    clock.advance(600)
    app._tick()
    assert app.asleep == 0
