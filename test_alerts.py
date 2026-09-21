#!/usr/bin/env python3
"""
test_alerts.py
==============

Tests for the obstacle alert behaviour.

This needs NO hardware - no camera, no Robot HAT, no sound card - so it runs
anywhere, including a Windows development PC:

    python3 test_alerts.py

It covers the two things that are hard to check by waving your hand at the
sensor: that a person standing still produces exactly one tone, and that
jitter around the 25 cm and 50 cm boundaries does not produce a stream of
them.
"""

import sys
import threading
import time

import alerts
import config
from hardware.audio import TONE_DANGER, TONE_WARNING, BeepController

FAILURES = []


def check(label, actual, expected):
    ok = actual == expected
    print("  [{}] {}".format("PASS" if ok else "FAIL", label))
    if not ok:
        print("        expected {!r}".format(expected))
        print("        actual   {!r}".format(actual))
        FAILURES.append(label)


class FakeClock:
    """Controllable clock so cooldown tests do not have to sleep."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def run(distances, clock=None):
    """Feed distances to a fresh policy. Returns (statuses, tones, repeats)."""
    policy = alerts.AlertPolicy(now=clock or FakeClock())
    statuses, tones, repeats = [], 0, []
    for distance in distances:
        decision = policy.update(distance)
        statuses.append(decision.status)
        repeats.append(decision.repeat_interval)
        if decision.play_warning_tone:
            tones += 1
    return statuses, tones, repeats


# ==========================================================================
print("\nBand thresholds (no hysteresis effect on a fresh policy)")
# ==========================================================================
statuses, _, _ = run([150, 101, 100, 75, 50, 49, 25, 24, 1])
check("150,101 -> SAFE", statuses[:2], ["SAFE", "SAFE"])
check("100,75,50 -> CAUTION", statuses[2:5], ["CAUTION"] * 3)
check("49,25 -> WARNING", statuses[5:7], ["WARNING", "WARNING"])
check("24,1 -> DANGER", statuses[7:], ["DANGER", "DANGER"])

# ==========================================================================
print("\nOnly DANGER repeats; CAUTION and WARNING are silent bands")
# ==========================================================================
_, _, repeats = run([150, 75, 40, 10])
check("SAFE repeat interval", repeats[0], None)
check("CAUTION repeat interval", repeats[1], None)
check("WARNING repeat interval", repeats[2], None)
check("DANGER repeat interval", repeats[3], config.BEEP_INTERVAL_DANGER_S)

# ==========================================================================
print("\nA person walking in, then standing still at 30-40 cm")
# ==========================================================================
statuses, tones, _ = run([150, 80, 45, 45, 38, 42, 36, 39, 33, 41, 37])
check("ends in WARNING", statuses[-1], "WARNING")
check("exactly ONE tone for the whole approach + standing still", tones, 1)

# ==========================================================================
print("\nJitter across the 50 cm boundary does not re-trigger")
# ==========================================================================
_, tones, _ = run([80, 45, 52, 48, 53, 47, 51, 49, 54, 46])
check("one tone on entry, none from jitter", tones, 1)

statuses, _, _ = run([80, 45, 52])
check("52 cm stays WARNING (needs >55 to leave)", statuses[-1], "WARNING")
statuses, _, _ = run([80, 45, 56])
check("56 cm does leave WARNING", statuses[-1], "CAUTION")

# ==========================================================================
print("\nJitter across the 25 cm boundary does not produce a stream of tones")
# ==========================================================================
clock = FakeClock()
_, tones, _ = run([80, 27, 24, 31, 23, 32, 22, 30, 24, 33], clock=clock)
check("one tone despite crossing 25 cm five times", tones, 1)

statuses, _, _ = run([80, 27, 24, 28])
check("28 cm stays DANGER (needs >30 to leave)", statuses[-1], "DANGER")
statuses, _, _ = run([80, 27, 24, 31])
check("31 cm does leave DANGER", statuses[-1], "WARNING")

# ==========================================================================
print("\nGetting closer is never delayed by hysteresis")
# ==========================================================================
statuses, _, _ = run([150, 24])
check("SAFE straight to DANGER in one reading", statuses[-1], "DANGER")
statuses, _, _ = run([150, 45])
check("SAFE straight to WARNING in one reading", statuses[-1], "WARNING")

# ==========================================================================
print("\nReplay rule 1: leaves the band, comes back later")
# ==========================================================================
clock = FakeClock()
policy = alerts.AlertPolicy(now=clock)
tones = 0
for distance in (80, 40):                 # enter WARNING -> tone
    tones += bool(policy.update(distance).play_warning_tone)
check("tone on first entry", tones, 1)

policy.update(200)                        # walk away to SAFE
clock.advance(config.WARNING_TONE_MIN_GAP_S + 1)
tones += bool(policy.update(40).play_warning_tone)
check("tone again on genuine re-entry", tones, 2)

# ==========================================================================
print("\nReplay rule 2: dips into DANGER, then backs off into WARNING")
# ==========================================================================
clock = FakeClock()
policy = alerts.AlertPolicy(now=clock)
tones = int(bool(policy.update(40).play_warning_tone))
policy.update(15)                         # into DANGER
clock.advance(config.WARNING_TONE_MIN_GAP_S + 1)
tones += bool(policy.update(40).play_warning_tone)
check("tone again after backing out of DANGER", tones, 2)

# ==========================================================================
print("\nRe-arm delay suppresses a too-soon repeat")
# ==========================================================================
clock = FakeClock()
policy = alerts.AlertPolicy(now=clock)
tones = int(bool(policy.update(40).play_warning_tone))
policy.update(200)
clock.advance(0.5)                        # well under WARNING_TONE_MIN_GAP_S
tones += bool(policy.update(40).play_warning_tone)
check("re-entry within the re-arm delay stays silent", tones, 1)

# ==========================================================================
print("\nA failing sensor stays silent (never a fake alarm)")
# ==========================================================================
statuses, tones, repeats = run([40, None, None])
check("no reading -> UNKNOWN", statuses[-1], "UNKNOWN")
check("no reading -> no repeating beep", repeats[-1], None)
check("no reading -> no tone", tones, 1)   # the 1 is the initial 40 cm entry

# ==========================================================================
print("\nBeepController plays one-shots and repeats without blocking")
# ==========================================================================


class FakePlayer:
    """Stands in for BeepPlayer so this test needs no sound card."""

    def __init__(self):
        self.played = []
        self.lock = threading.Lock()

    def play(self, tone=TONE_DANGER):
        with self.lock:
            self.played.append(tone)

    def snapshot(self):
        with self.lock:
            return list(self.played)


player = FakePlayer()
beeper = BeepController(player)
beeper.start()
try:
    # A one-shot must not need an interval to be set.
    beeper.play_once(TONE_WARNING)
    time.sleep(0.3)
    check("one-shot warning tone played once", player.snapshot(), [TONE_WARNING])

    # Repeating danger beeps.
    beeper.set_interval(0.05)
    time.sleep(0.4)
    danger_count = player.snapshot().count(TONE_DANGER)
    check("danger beeps repeated while interval is set", danger_count > 2, True)

    # Silence on request.
    beeper.set_interval(None)
    time.sleep(0.2)
    before = len(player.snapshot())
    time.sleep(0.3)
    check("goes silent when the interval is cleared",
          len(player.snapshot()), before)
finally:
    stop_started = time.perf_counter()
    beeper.stop()
    stop_took = time.perf_counter() - stop_started

check("stop() returns promptly (Q / Ctrl+C stay responsive)", stop_took < 1.0, True)
check("beeper thread actually exited", beeper.is_alive(), False)

# ==========================================================================
print("")
if FAILURES:
    print("{} FAILED: {}".format(len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("All alert behaviour tests passed.")
