#!/usr/bin/env python3
"""
test_alerts.py
==============

Tests for the obstacle alert behaviour.

This needs NO hardware - no camera, no Robot HAT, no sound card - so it runs
anywhere, including a Windows development PC:

    python3 test_alerts.py

It covers the things that are hard to check by waving your hand at the
sensor: that a person standing still produces exactly one tone, that jitter
around the 25 cm and 50 cm boundaries does not produce a stream of them, and
that the Gemini trigger fires once per approach rather than continuously.

No network access happens here either - the Gemini tests exercise the queue,
the staleness rules and the failure handling with the API call stubbed out.
"""

import sys
import threading
import time

import alerts
import config
import vision
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


def trace(distances, clock=None, policy=None):
    """Feed distances to a policy and return the list of AlertDecisions."""
    policy = policy or alerts.AlertPolicy(now=clock or FakeClock())
    return [policy.update(d) for d in distances]


def ai_calls(distances, clock=None):
    """Distances at which an AI request would have been made."""
    clock = clock or FakeClock()
    policy = alerts.AlertPolicy(now=clock)
    return [d for d in distances if policy.update(d).request_ai]


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
print("\nGemini trigger: the transitions you asked about")
# ==========================================================================
check("SAFE -> CAUTION requests AI", ai_calls([150, 80]), [80])
check("SAFE -> DANGER requests AI (fast-approach gap closed)",
      ai_calls([150, 20]), [20])
check("SAFE -> WARNING requests AI (head-turn gap closed)",
      ai_calls([150, 40]), [40])

# Normal walk-up: CAUTION fires, the later bands are absorbed by the cooldown
# because they follow within a second or two.
check("CAUTION -> WARNING does not duplicate the request",
      ai_calls([150, 80, 40]), [80])
check("CAUTION -> DANGER respects the cooldown",
      ai_calls([150, 80, 20]), [80])
check("WARNING -> DANGER respects the cooldown",
      ai_calls([150, 40, 20]), [40])

check("standing still in CAUTION never re-requests",
      ai_calls([150, 80, 80, 75, 82, 78, 85, 79, 81]), [80])
check("boundary noise around 50 cm does not spam Gemini",
      ai_calls([150, 80, 52, 48, 53, 47, 51, 49, 54, 46]), [80])

check("moving AWAY never requests (DANGER -> WARNING -> CAUTION)",
      ai_calls([150, 20, 35, 70]), [20])
check("no reading never requests", ai_calls([None, None]), [])
check("staying in SAFE never requests", ai_calls([200, 150, 300]), [])

# ==========================================================================
print("\nGemini cooldown is independent of the warning-tone re-arm")
# ==========================================================================
clock = FakeClock()
policy = alerts.AlertPolicy(now=clock)
policy.update(150)
check("first CAUTION entry requests", policy.update(80).request_ai, True)
clock.advance(1.0)
policy.update(200)
check("re-entry after 1s is blocked by cooldown",
      policy.update(80).request_ai, False)
clock.advance(config.GEMINI_COOLDOWN_S + 1)
policy.update(200)
check("re-entry after the cooldown requests again",
      policy.update(80).request_ai, True)

check("the two timers are configured independently",
      config.GEMINI_COOLDOWN_S != config.WARNING_TONE_MIN_GAP_S, True)

# A tone with no description is acceptable; the local alert always wins.
clock = FakeClock()
policy = alerts.AlertPolicy(now=clock)
policy.update(40)                     # tone + AI
clock.advance(4.0)                    # past the 3s tone re-arm, inside 5s AI
policy.update(200)
decision = policy.update(40)
check("local tone still fires while the AI cooldown blocks",
      (decision.play_warning_tone, decision.request_ai), (True, False))


# ==========================================================================
print("\nGemini worker: queue never stacks (maxsize=1)")
# ==========================================================================


class FakeFrame:
    """Stands in for a numpy frame; only .copy() is needed here."""

    def __init__(self, tag="frame"):
        self.tag = tag
        self.copies = 0

    def copy(self):
        self.copies += 1
        return self


worker = vision.GeminiWorker()          # not started: no thread, no network
check("first request is accepted", worker.request(FakeFrame("a"), 80.0), True)
check("second request is DROPPED while one is queued",
      worker.request(FakeFrame("b"), 79.0), False)
check("third request is also dropped",
      worker.request(FakeFrame("c"), 78.0), False)

snap = worker.snapshot()
check("one request counted", snap["request_count"], 1)
check("two drops counted", snap["dropped_count"], 2)
check("no description yet", snap["description"], None)

frame = FakeFrame("d")
worker.request(frame, 80.0)
check("a dropped request does not copy the frame", frame.copies, 0)


# ==========================================================================
print("\nGemini worker: failures are absorbed, Phase 1 continues")
# ==========================================================================
def failing_worker(exc):
    w = vision.GeminiWorker()
    w._encode_jpeg = staticmethod(lambda frame: b"fake-jpeg")

    def boom(jpeg, distance):
        raise exc

    w._call_gemini = boom
    return w


for label, exc in [
    ("connection error", OSError("Temporary failure in name resolution")),
    ("API error", RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded")),
    ("unexpected exception", ValueError("malformed response")),
]:
    w = failing_worker(exc)
    request = vision.AiRequest(FakeFrame(), 80.0, time.monotonic())
    raised = None
    try:
        w._handle(request)
    except Exception as caught:          # must never escape the worker
        raised = caught
    snap = w.snapshot()
    check("{}: nothing propagates to the caller".format(label), raised, None)
    check("{}: recorded as an error".format(label), snap["error_count"], 1)
    check("{}: no description shown".format(label), snap["description"], None)

# An empty reply is a failure, not a description.
w = vision.GeminiWorker()
w._encode_jpeg = staticmethod(lambda frame: b"fake-jpeg")
w._call_gemini = lambda jpeg, distance: "   "
w._handle(vision.AiRequest(FakeFrame(), 80.0, time.monotonic()))
check("empty reply is not shown", w.snapshot()["description"], None)


# ==========================================================================
print("\nGemini worker: stale results are discarded, never displayed")
# ==========================================================================
w = vision.GeminiWorker()
w._encode_jpeg = staticmethod(lambda frame: b"fake-jpeg")
w._call_gemini = lambda jpeg, distance: "Chair directly ahead."

# Fresh reply -> shown.
w._handle(vision.AiRequest(FakeFrame(), 80.0, time.monotonic()))
check("a fresh description is shown",
      w.snapshot()["description"], "Chair directly ahead.")
check("one reply counted", w.snapshot()["reply_count"], 1)

# The same description, once the image it came from has aged out.
w._captured_at = time.monotonic() - (config.GEMINI_RESULT_MAX_AGE_S + 5)
snap = w.snapshot()
check("an aged-out description is hidden", snap["description"], None)
check("and is reported as stale", snap["stale"], True)

# A reply whose image was already too old on arrival is never stored.
w2 = vision.GeminiWorker()
w2._encode_jpeg = staticmethod(lambda frame: b"fake-jpeg")
w2._call_gemini = lambda jpeg, distance: "Person ahead."
old_capture = time.monotonic() - (config.GEMINI_RESULT_MAX_AGE_S + 5)
w2._handle(vision.AiRequest(FakeFrame(), 80.0, old_capture))
snap = w2.snapshot()
check("a reply born stale is discarded", snap["description"], None)
check("and counted as discarded", snap["discarded_count"], 1)
check("and never counted as a reply", snap["reply_count"], 0)


# ==========================================================================
print("\nGemini worker: replies are tidied into one short line")
# ==========================================================================
check("quotes stripped", vision.GeminiWorker._tidy('"Person ahead."'),
      "Person ahead.")
check("only the first line kept",
      vision.GeminiWorker._tidy("Chair ahead.\nAlso a table."), "Chair ahead.")
check("whitespace collapsed",
      vision.GeminiWorker._tidy("  Two   people   ahead.  "),
      "Two people ahead.")
check("over-long replies are truncated",
      len(vision.GeminiWorker._tidy("x" * 500)),
      config.GEMINI_MAX_DESCRIPTION_CHARS)
check("empty reply gives empty string", vision.GeminiWorker._tidy(None), "")


# ==========================================================================
print("\nAPI key handling never leaks the value")
# ==========================================================================
fake_env = {}
loaded = vision.load_env_file(path=config.ENV_FILE_PATH, environ=fake_env)
check("the real .env.local key name is found", loaded, ["GEMINI_API_KEY"])
check("load_env_file returns names, not values",
      all("=" not in name and len(name) < 64 for name in loaded), True)

# An already-set variable must win over the file.
preset = {"GEMINI_API_KEY": "already-set"}
check("existing environment variables are not overwritten",
      vision.load_env_file(path=config.ENV_FILE_PATH, environ=preset), [])
check("and keep their original value", preset["GEMINI_API_KEY"], "already-set")

check("api_key_is_present is False when unset",
      vision.api_key_is_present(environ={}), False)
check("api_key_is_present is False for whitespace",
      vision.api_key_is_present(environ={"GEMINI_API_KEY": "   "}), False)
check("api_key_is_present is True when set",
      vision.api_key_is_present(environ={"GEMINI_API_KEY": "k"}), True)


# ==========================================================================
print("")
if FAILURES:
    print("{} FAILED: {}".format(len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("All alert behaviour tests passed.")
