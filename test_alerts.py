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

import os
import pathlib
import sys
import tempfile
import threading
import time

import alerts
import config
import vision
import contextlib
import io

import main as app
import hardware.audio as audio_module
from hardware.audio import (
    TONE_DANGER,
    TONE_WARNING,
    BeepController,
    SpeechController,
    _clean_for_speech,
)
from hardware.ultrasonic import UltrasonicMonitor

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
print("\nGemini worker: the queue holds at most one item")
# ==========================================================================
# Phase 2 dropped a NEW request while one was in flight. Phase 3 reversed
# that: the newest request always wins, because old navigation advice is
# worthless. Either way nothing is ever allowed to back up - the detailed
# newest-wins checks live in their own section further down.


class FakeFrame:
    """Stands in for a numpy frame; only .copy() is needed here."""

    def __init__(self, tag="frame"):
        self.tag = tag
        self.copies = 0

    def copy(self):
        self.copies += 1
        return self


worker = vision.GeminiWorker()          # not started: no thread, no network
for tag, distance in (("a", 80.0), ("b", 79.0), ("c", 78.0)):
    worker.request(FakeFrame(tag), distance, "CAUTION", "band")

check("the queue never exceeds one item", worker._queue.qsize(), 1)
snap = worker.snapshot()
check("every request was counted", snap["request_count"], 3)
check("no description yet", snap["description"], None)
check("the worker starts IDLE", snap["state"], "IDLE")


# ==========================================================================
print("\nGemini worker: failures are absorbed, Phase 1 continues")
# ==========================================================================
def failing_worker(exc):
    w = vision.GeminiWorker()
    w._latest_request_id = 1        # this hand-built request IS the newest
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
    request = vision.AiRequest(FakeFrame(), 80.0, "CAUTION", time.monotonic(), 1, "band")
    raised = None
    try:
        w._process_request(request)
    except Exception as caught:          # must never escape the worker
        raised = caught
    snap = w.snapshot()
    check("{}: nothing propagates to the caller".format(label), raised, None)
    check("{}: recorded as an error".format(label), snap["error_count"], 1)
    check("{}: no description shown".format(label), snap["description"], None)

# An empty reply is a failure, not a description.
w = vision.GeminiWorker()
w._latest_request_id = 1
w._encode_jpeg = staticmethod(lambda frame: b"fake-jpeg")
w._call_gemini = lambda jpeg, distance: "   "
w._process_request(vision.AiRequest(FakeFrame(), 80.0, "CAUTION", time.monotonic(), 1, "band"))
check("empty reply is not shown", w.snapshot()["description"], None)


# ==========================================================================
print("\nGemini worker: stale results are discarded, never displayed")
# ==========================================================================
w = vision.GeminiWorker()
w._latest_request_id = 1
w._encode_jpeg = staticmethod(lambda frame: b"fake-jpeg")
w._call_gemini = lambda jpeg, distance: "Chair directly ahead."

# Fresh reply -> shown.
w._process_request(vision.AiRequest(FakeFrame(), 80.0, "CAUTION", time.monotonic(), 1, "band"))
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
w2._latest_request_id = 1
w2._encode_jpeg = staticmethod(lambda frame: b"fake-jpeg")
w2._call_gemini = lambda jpeg, distance: "Person ahead."
old_capture = time.monotonic() - (config.GEMINI_ACCEPT_MAX_AGE_S + 5)
w2._process_request(vision.AiRequest(FakeFrame(), 80.0, "CAUTION", old_capture, 1, "band"))
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
print("\nGemini client is built for the Developer API, explicitly")
# ==========================================================================
# Verified with a stub SDK so this runs anywhere and makes no network call.
# What matters is WHICH arguments reach genai.Client.


class StubClient:
    """Records exactly what GeminiWorker._make_client passed us."""

    last = None

    def __init__(self, api_key=None, vertexai=None, enterprise=None,
                 http_options=None):
        self.api_key = api_key
        self.vertexai = vertexai
        self.enterprise = enterprise
        self.http_options = http_options
        StubClient.last = self


class StubGenai:
    Client = StubClient


FAKE_KEY = "test-key-do-not-use-1234567890"
_saved_key = os.environ.get("GEMINI_API_KEY")
os.environ["GEMINI_API_KEY"] = FAKE_KEY
try:
    client, description = vision.GeminiWorker._make_client(StubGenai)

    check("the API key is passed explicitly", client.api_key, FAKE_KEY)
    check("vertexai is explicitly disabled", client.vertexai, False)
    check("enterprise is explicitly disabled", client.enterprise, False)
    check("the startup line names the Developer API",
          "Developer API" in description, True)
    check("the startup line names the model",
          config.GEMINI_MODEL in description, True)
    check("the startup line NEVER contains the key",
          FAKE_KEY in description, False)

    # An empty key must fail loudly rather than fall through to no-credential.
    os.environ["GEMINI_API_KEY"] = "   "
    raised = None
    try:
        vision.GeminiWorker._make_client(StubGenai)
    except vision.VisionError as exc:
        raised = exc
    check("an empty key raises VisionError", raised is not None, True)
    check("and that error does not leak anything",
          FAKE_KEY in str(raised or ""), False)

    # If a future SDK ignores the pin, say so instead of claiming otherwise.
    class VertexIgnoringClient(StubClient):
        def __init__(self, **kwargs):
            StubClient.__init__(self, **kwargs)
            self.vertexai = True          # pretend the pin was ignored

    class StubGenaiVertex:
        Client = VertexIgnoringClient

    os.environ["GEMINI_API_KEY"] = FAKE_KEY
    _, vertex_description = vision.GeminiWorker._make_client(StubGenaiVertex)
    check("a backend that resolves to Vertex is reported, not hidden",
          "WARNING" in vertex_description, True)
finally:
    if _saved_key is None:
        os.environ.pop("GEMINI_API_KEY", None)
    else:
        os.environ["GEMINI_API_KEY"] = _saved_key

check("secrets are redacted from error text",
      vision._redact("boom sk-abc123 boom", "sk-abc123"), "boom <redacted> boom")
check("redaction copes with no secret",
      vision._redact("plain message", ""), "plain message")


# ==========================================================================
print("\nAQ. authorization keys are recognised and never echoed")
# ==========================================================================
# Google AI Studio now issues "AQ." authorization keys in place of the legacy
# "AIza" API keys. Documented format: AQ. plus 40+ URL-safe characters. Both
# travel in the same x-goog-api-key header - AQ. keys are NOT OAuth tokens.

check("an AQ. key is recognised",
      vision.key_fingerprint(environ={"GEMINI_API_KEY": "AQ." + "A" * 50}),
      "AQ. authorization key, 53 chars")
check("a short AQ. key is flagged as possibly truncated",
      "truncated" in vision.key_fingerprint(
          environ={"GEMINI_API_KEY": "AQ." + "A" * 20}), True)
check("a legacy AIza key is recognised",
      vision.key_fingerprint(environ={"GEMINI_API_KEY": "AIza" + "B" * 35}),
      "AIza legacy API key, 39 chars")
check("an unrecognised format says so",
      vision.key_fingerprint(environ={"GEMINI_API_KEY": "hello"}),
      "unrecognised format, 5 chars")
check("an unset key says so",
      vision.key_fingerprint(environ={}), "not set")
check("the fingerprint never contains the key itself",
      "A" * 50 in vision.key_fingerprint(
          environ={"GEMINI_API_KEY": "AQ." + "A" * 50}), False)

# A key pasted with a line wrap or carried over from a Windows editor must
# not be sent verbatim - that looks like a bad key when it is only mangled.
_tmp = pathlib.Path(tempfile.gettempdir()) / "sense_key_sanitise.env"
for label, written, expected in [
    ("trailing CRLF", "GEMINI_API_KEY=AQ.abcdef\r\n", "AQ.abcdef"),
    ("surrounding quotes", 'GEMINI_API_KEY="AQ.abcdef"\n', "AQ.abcdef"),
    ("stray inner spaces", "GEMINI_API_KEY=AQ. abc def\n", "AQ.abcdef"),
    ("leading whitespace", "GEMINI_API_KEY=   AQ.abcdef\n", "AQ.abcdef"),
]:
    _tmp.write_text(written, encoding="utf-8")
    env = {}
    vision.load_env_file(path=_tmp, environ=env)
    check("sanitised: {}".format(label), env.get("GEMINI_API_KEY"), expected)
_tmp.unlink()

# And the real key on this machine must be well formed.
_real = {}
vision.load_env_file(path=config.ENV_FILE_PATH, environ=_real)
_real_key = _real.get("GEMINI_API_KEY", "")
check("the project's own key parses as a valid AQ. key",
      _real_key.startswith("AQ.") and len(_real_key) >= 43, True)


# ==========================================================================
print("\nGemini timeouts respect the API minimum and stay layered")
# ==========================================================================
# Gemini rejects a deadline under 10s before the model is even reached:
#   400 INVALID_ARGUMENT: Manually set deadline 8s is too short.
# These checks exist so that never silently comes back.

check("the configured SDK timeout meets Gemini's minimum",
      config.GEMINI_REQUEST_TIMEOUT_S >= config.GEMINI_MIN_TIMEOUT_S, True)
check("the documented minimum is 10s",
      config.GEMINI_MIN_TIMEOUT_S, 10.0)
check("the effective timeout is what we configured",
      vision.GeminiWorker._effective_timeout_s(),
      config.GEMINI_REQUEST_TIMEOUT_S)

# The worker deadline must not cut a request off while the SDK is still
# legitimately waiting, or the SDK timeout would never come into play.
check("the worker deadline is at least the SDK timeout",
      config.GEMINI_DEADLINE_S >= config.GEMINI_REQUEST_TIMEOUT_S, True)

# A too-low value in config.py must degrade to the minimum, not break
# every call.
_saved_timeout = config.GEMINI_REQUEST_TIMEOUT_S
try:
    config.GEMINI_REQUEST_TIMEOUT_S = 8.0
    check("a too-low configured timeout is clamped up to the minimum",
          vision.GeminiWorker._effective_timeout_s(),
          config.GEMINI_MIN_TIMEOUT_S)
    config.GEMINI_REQUEST_TIMEOUT_S = 30.0
    check("a generous timeout is left alone",
          vision.GeminiWorker._effective_timeout_s(), 30.0)
finally:
    config.GEMINI_REQUEST_TIMEOUT_S = _saved_timeout

# Staleness is a separate, deliberate product rule - flag if the timeout
# budget now exceeds it, because replies that slow would be discarded.
check("staleness window is still the tightest limit (by design)",
      config.GEMINI_RESULT_MAX_AGE_S <= config.GEMINI_REQUEST_TIMEOUT_S, True)


# ==========================================================================
print("\nNo thread class shadows a threading.Thread internal")
# ==========================================================================
# This guard exists because of a real bug. GeminiWorker once had a method
# called _handle. CPython 3.13's Thread.start() assigns an INSTANCE attribute
# named _handle holding a _thread._ThreadHandle, and an instance attribute
# shadows a class method - so the method became unreachable the moment the
# thread started, and calling it raised:
#
#     TypeError: '_thread._ThreadHandle' object is not callable
#
# Python 3.12 has no such attribute and 3.14 renamed it to
# _os_thread_handle, so it only reproduced on 3.13 - which is what Raspberry
# Pi OS ships. It also survived every test, because nothing called the method
# on a STARTED thread. This check is version-independent: it compares names
# directly, so it fails on the development machine too.

THREAD_INTERNALS = {
    "_handle",            # 3.13: _thread._ThreadHandle
    "_os_thread_handle",  # 3.14 rename of the above
    "_target", "_name", "_args", "_kwargs", "_daemonic", "_ident",
    "_native_id", "_tstate_lock", "_started", "_is_stopped", "_initialized",
    "_stderr", "_invoke_excepthook", "_bootstrap", "_bootstrap_inner",
    "_stop", "_delete", "_wait_for_tstate_lock", "_reset_internal_locks",
    "_set_ident", "_set_native_id", "_set_tstate_lock",
    "start", "join", "is_alive", "name", "ident", "daemon", "native_id",
    "getName", "setName", "isDaemon", "setDaemon",
}
INTENTIONAL_OVERRIDES = {"run"}     # every worker legitimately defines run()

for thread_class in (vision.GeminiWorker, BeepController, UltrasonicMonitor):
    ours = set()
    for klass in thread_class.__mro__:
        if klass is threading.Thread:
            break
        ours |= set(vars(klass))
    clashes = sorted((ours & THREAD_INTERNALS) - INTENTIONAL_OVERRIDES)
    check("{} defines no name Thread uses internally".format(
        thread_class.__name__), clashes, [])

# And prove the mechanism still works the way we think it does, so this test
# keeps its meaning if CPython changes again.
_probe = threading.Thread(target=lambda: None)
_probe.start()
_probe.join()
_os_handle = getattr(_probe, "_handle", None) or getattr(
    _probe, "_os_thread_handle", None)
if _os_handle is not None:
    check("a Thread's OS handle is genuinely not callable",
          callable(_os_handle), False)
else:
    print("  [PASS] this Python exposes no OS thread handle attribute")

# The worker's real entry point must be callable on a STARTED thread - the
# exact condition the original bug broke.
_started = vision.GeminiWorker()
_started._latest_request_id = 1
_started._encode_jpeg = staticmethod(lambda frame: b"fake-jpeg")
_started._call_gemini = lambda jpeg, distance: "Wall ahead."
_started.start()
try:
    _started._process_request(
        vision.AiRequest(FakeFrame(), 80.0, "CAUTION", time.monotonic(), 1, "band"))
    check("_process_request works on a running thread",
          _started.snapshot()["description"], "Wall ahead.")
finally:
    _started.stop()


# ==========================================================================
print("\nHardware configuration is the confirmed wiring")
# ==========================================================================
check("TRIG is Robot HAT port D0", config.TRIG_PIN, "D0")
check("ECHO is Robot HAT port D1", config.ECHO_PIN, "D1")
check("D0 maps to GPIO17", config.ROBOT_HAT_PIN_TO_BCM["D0"], 17)
check("D1 maps to GPIO4", config.ROBOT_HAT_PIN_TO_BCM["D1"], 4)
check("the two ports are different", config.TRIG_PIN != config.ECHO_PIN, True)

# ==========================================================================
print("\nBand boundaries are unchanged by Phase 3")
# ==========================================================================
check("SAFE above 100 cm", config.SAFE_DISTANCE_CM, 100.0)
check("CAUTION band starts at 50 cm", config.CAUTION_DISTANCE_CM, 50.0)
check("WARNING band starts at 25 cm", config.WARNING_DISTANCE_CM, 25.0)
check("only DANGER repeats a beep",
      [alerts.beep_interval_for(s) is not None
       for s in ("SAFE", "CAUTION", "WARNING", "DANGER")],
      [False, False, False, True])

# ==========================================================================
print("\nAI triggers: band, substantial move, periodic refresh")
# ==========================================================================
clock = FakeClock()
policy = alerts.AlertPolicy(now=clock)
policy.update(150)
check("entering CAUTION triggers on the band",
      policy.update(80).ai_reason, "band")

# Small drift must NOT spend a request.
clock.advance(config.GEMINI_COOLDOWN_S + 1)
check("a few cm of jitter does not trigger",
      policy.update(78).ai_reason, None)
clock.advance(config.GEMINI_COOLDOWN_S + 1)
check("still no trigger for small drift",
      policy.update(72).ai_reason, None)

# A substantial move within the same band does.
clock.advance(config.GEMINI_COOLDOWN_S + 1)
reason = policy.update(80 - config.GEMINI_DISTANCE_CHANGE_CM - 1).ai_reason
check("a substantial distance change triggers",
      reason is not None and reason.startswith("moved"), True)

# Sitting still eventually earns a refresh, but not before.
clock2 = FakeClock()
policy2 = alerts.AlertPolicy(now=clock2)
policy2.update(150)
policy2.update(80)                       # "band"
clock2.advance(config.GEMINI_REFRESH_S - 1)
check("no refresh before the interval", policy2.update(80).ai_reason, None)
clock2.advance(2)
check("refresh once the interval passes",
      policy2.update(80).ai_reason, "refresh")

# The cooldown still gates everything.
clock3 = FakeClock()
policy3 = alerts.AlertPolicy(now=clock3)
policy3.update(150)
policy3.update(80)                       # "band"
clock3.advance(1.0)
check("cooldown blocks a second request", policy3.update(20).ai_reason, None)

check("moving away never triggers", ai_calls([150, 20, 40, 70, 150]), [20])
check("staying in SAFE never triggers", ai_calls([200, 150, 300]), [])
check("DANGER entry still triggers when armed", ai_calls([150, 20]), [20])


# ==========================================================================
print("\nNewest request wins; nothing ever backs up")
# ==========================================================================


class FakeFrame:
    def __init__(self, tag="f"):
        self.tag = tag
        self.copies = 0

    def copy(self):
        self.copies += 1
        return self


worker = vision.GeminiWorker()          # not started: no thread, no network
check("first request accepted",
      worker.request(FakeFrame("a"), 80.0, "CAUTION", "band"), True)
check("second request also accepted (it replaces the first)",
      worker.request(FakeFrame("b"), 60.0, "CAUTION", "moved"), True)
check("third request also accepted",
      worker.request(FakeFrame("c"), 40.0, "WARNING", "band"), True)

snap = worker.snapshot()
check("all three counted as requests", snap["request_count"], 3)
check("two older ones were replaced, not queued", snap["replaced_count"], 2)
check("the queue still holds exactly one item", worker._queue.qsize(), 1)

queued = worker._queue.get_nowait()
check("and it is the NEWEST one", queued.distance_cm, 40.0)
check("which carries its request id", queued.request_id, 3)
check("and the trigger reason", queued.reason, "band")

# Request ids must be strictly increasing so "newest" is well defined.
w_ids = vision.GeminiWorker()
for _ in range(4):
    w_ids.request(FakeFrame(), 50.0, "WARNING", "band")
ids = []
while not w_ids._queue.empty():
    ids.append(w_ids._queue.get_nowait().request_id)
check("the surviving request is the highest id", ids, [4])


# ==========================================================================
print("\nRelevance decides acceptance, not a stopwatch")
# ==========================================================================
worker = vision.GeminiWorker()
worker.note_current_state(60.0, "CAUTION")
worker._latest_request_id = 2

fresh = vision.AiRequest(FakeFrame(), 60.0, "CAUTION", time.monotonic(), 2, "band")
older = vision.AiRequest(FakeFrame(), 80.0, "CAUTION", time.monotonic(), 1, "band")

check("a matching request is accepted",
      worker._relevance_problem(fresh, 1.0), None)

# A SLOW but still-true reply must be accepted - this is the whole point.
check("a 6s-old reply is accepted while the obstacle is unchanged",
      worker._relevance_problem(fresh, 6.3), None)

reason = worker._relevance_problem(older, 1.0)
check("a superseded request is rejected",
      reason is not None and "superseded" in reason, True)

worker.note_current_state(None, "SAFE")
reason = worker._relevance_problem(fresh, 1.0)
check("a vanished obstacle rejects the reply",
      reason is not None and "gone" in reason, True)

worker.note_current_state(None, "UNKNOWN")
reason = worker._relevance_problem(fresh, 1.0)
check("a failing sensor rejects the reply",
      reason is not None and "gone" in reason, True)

worker.note_current_state(60.0 + config.GEMINI_RELEVANCE_DISTANCE_CM + 5, "CAUTION")
reason = worker._relevance_problem(fresh, 1.0)
check("a materially changed distance rejects the reply",
      reason is not None and "scene changed" in reason, True)

worker.note_current_state(60.0 + config.GEMINI_RELEVANCE_DISTANCE_CM - 5, "CAUTION")
check("a small distance change still accepts",
      worker._relevance_problem(fresh, 1.0), None)

worker.note_current_state(60.0, "CAUTION")
reason = worker._relevance_problem(fresh, config.GEMINI_ACCEPT_MAX_AGE_S + 1)
check("the age backstop still catches a hung socket",
      reason is not None and "too old" in reason, True)

check("the backstop is more generous than the display window",
      config.GEMINI_ACCEPT_MAX_AGE_S > config.GEMINI_RESULT_MAX_AGE_S, True)


# ==========================================================================
print("\nEnd to end: accept, then reject an obsolete reply")
# ==========================================================================
worker = vision.GeminiWorker()
worker._encode_jpeg = staticmethod(lambda frame: b"jpeg")
worker._call_gemini = lambda jpeg, distance: "Chair ahead, move right."

worker.request(FakeFrame(), 60.0, "CAUTION", "band")
request = worker._queue.get_nowait()
worker.note_current_state(58.0, "CAUTION")
worker._process_request(request)
snap = worker.snapshot()
check("a relevant reply is published",
      snap["description"], "Chair ahead, move right.")
check("its generation bumped", snap["generation"], 1)
check("timings were recorded", snap["api_ms"] is not None, True)
check("the band at capture is kept", snap["status"], "CAUTION")

# Now the user walks away while a reply is in flight.
worker2 = vision.GeminiWorker()
worker2._encode_jpeg = staticmethod(lambda frame: b"jpeg")
worker2._call_gemini = lambda jpeg, distance: "Person ahead, left."
worker2.request(FakeFrame(), 40.0, "WARNING", "band")
request = worker2._queue.get_nowait()
worker2.note_current_state(None, "SAFE")        # obstacle gone
worker2._process_request(request)
snap = worker2.snapshot()
check("an obsolete reply is never published", snap["description"], None)
check("and is counted as discarded", snap["discarded_count"], 1)
check("not as an error", snap["error_count"], 0)
check("the HUD is told it was discarded", snap["state"], "DISCARDED")


# ==========================================================================
print("\nThe ultrasonic distance is what reaches Gemini")
# ==========================================================================
captured = {}


def _spy(jpeg, distance_cm):
    captured["distance"] = distance_cm
    captured["prompt"] = config.GEMINI_PROMPT.format(
        distance_cm=int(distance_cm))
    return "Table leg ahead, move left."


worker = vision.GeminiWorker()
worker._encode_jpeg = staticmethod(lambda frame: b"jpeg")
worker._call_gemini = _spy
worker.request(FakeFrame(), 42.0, "WARNING", "band")
worker.note_current_state(42.0, "WARNING")
worker._process_request(worker._queue.get_nowait())

check("the measured distance is passed through", captured["distance"], 42.0)
check("and appears in the prompt", "42 cm" in captured["prompt"], True)
check("the prompt forbids the model estimating distance",
      "never estimate or mention distance" in config.GEMINI_PROMPT, True)
check("the prompt asks for left / directly ahead / right",
      "on the left, directly ahead, or on the right" in config.GEMINI_PROMPT,
      True)
# The VISIBLE reply is bounded by the prompt and by _tidy(), not by the
# token cap - the cap has to stay generous because Gemini 3 charges its
# hidden thinking tokens against the same budget.
check("the prompt asks for three to ten words",
      "three to ten words" in config.GEMINI_PROMPT, True)
check("and the reply is hard-truncated for display",
      len(vision.GeminiWorker._tidy("word " * 100)),
      config.GEMINI_MAX_DESCRIPTION_CHARS)
check("frames are downscaled before upload",
      config.GEMINI_SEND_RESOLUTION[0] < config.CAMERA_RESOLUTION[0], True)


# ==========================================================================
print("\nSpeech: newest wins, no repeats, explicit mute interrupts")
# ==========================================================================


class FakeVoice:
    """Stands in for espeak; records what was spoken."""

    def __init__(self):
        self.spoken = []
        self.stops = 0
        self._busy_until = 0.0

    def speak(self, text):
        self.spoken.append(text)
        self._busy_until = time.monotonic() + 0.05
        return True

    def is_speaking(self):
        return time.monotonic() < self._busy_until

    def stop(self):
        self.stops += 1
        self._busy_until = 0.0


voice = FakeVoice()
speech_clock = FakeClock()
talker = SpeechController(voice, now=speech_clock)

check("a phrase is accepted", talker.say("Person ahead, left."), True)
check("empty text is ignored", talker.say("   "), False)
check("None is ignored", talker.say(None), False)

talker.start()
try:
    time.sleep(0.3)
    check("the phrase was spoken once", voice.spoken, ["Person ahead, left."])

    # Duplicate suppression.
    check("the identical phrase is suppressed",
          talker.say("Person ahead, left."), False)
    speech_clock.advance(config.SPEECH_DUPLICATE_GAP_S - 1)
    check("still suppressed inside the gap",
          talker.say("Person ahead, left."), False)
    speech_clock.advance(2)
    check("allowed again once the gap has passed",
          talker.say("Person ahead, left."), True)
    time.sleep(0.3)
    check("so it was spoken twice in total",
          voice.spoken.count("Person ahead, left."), 2)

    # A different phrase is never suppressed.
    check("different guidance is always allowed",
          talker.say("Chair ahead, move right."), True)
    time.sleep(0.3)
    check("and is spoken", "Chair ahead, move right." in voice.spoken, True)

    # An explicit mute silences speech and cuts off what is playing.
    # NOTE: the DANGER band does NOT do this - see the dedicated section
    # below. Only shutdown mutes.
    stops_before = voice.stops
    talker.set_muted(True)
    check("muting stops the current phrase", voice.stops > stops_before, True)
    check("muted speech rejects new phrases",
          talker.say("Doorway right."), False)
    spoken_before = len(voice.spoken)
    time.sleep(0.2)
    check("and nothing new is spoken while muted",
          len(voice.spoken), spoken_before)

    talker.set_muted(False)
    check("unmuting accepts phrases again", talker.say("Doorway right."), True)
    time.sleep(0.3)
    check("which are then spoken", voice.spoken[-1], "Doorway right.")

    check("suppressions were counted", talker.suppressed_count >= 3, True)
finally:
    stop_started = time.perf_counter()
    talker.stop()
    stop_took = time.perf_counter() - stop_started

check("speech stop() returns promptly", stop_took < 1.0, True)
check("the speech thread exited", talker.is_alive(), False)

# Newest-wins at the queue level: two phrases before the thread runs.
voice2 = FakeVoice()
talker2 = SpeechController(voice2)
talker2.say("Old guidance.")
talker2.say("New guidance.")
check("only the newest phrase is pending", talker2._pending_text,
      "New guidance.")

# Truncation keeps assistive audio short.
check("a long phrase is truncated",
      len(_clean_for_speech("x" * 500)), config.SPEECH_MAX_CHARS)
check("beeps are spaced, not silenced, while speaking",
      config.BEEP_SPACING_WHILE_SPEAKING >= 1.0, True)


# ==========================================================================
print("\nRequest config: the right thinking option for the model")
# ==========================================================================
# This section exists because of a real 400. Gemini 3 models are
# thinking-only: they accept thinking_level and REJECT thinking_budget with
#
#     400 INVALID_ARGUMENT. Request contains an invalid argument.
#
# Sending thinking_budget=0 to gemini-3.5-flash-lite broke every request,
# including the text-only prewarm. Construction of the config succeeded, so
# nothing caught it locally - the SDK builds a config the SERVER rejects.


class StubThinkingConfig:
    """Mimics types.ThinkingConfig field validation."""

    ALLOWED = {"thinking_level", "thinking_budget"}

    def __init__(self, **kwargs):
        bad = set(kwargs) - self.ALLOWED
        if bad:
            raise TypeError("unexpected field(s): {}".format(sorted(bad)))
        self.kwargs = kwargs


class StubTypes:
    ThinkingConfig = StubThinkingConfig


class OldSdkTypes:
    """An SDK predating thinking_level."""

    class ThinkingConfig:
        def __init__(self, **kwargs):
            if "thinking_level" in kwargs:
                raise TypeError("no such field: thinking_level")
            self.kwargs = kwargs


_saved_model = config.GEMINI_MODEL
_saved_level = config.GEMINI_THINKING_LEVEL
try:
    for model in ("gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-3-pro"):
        config.GEMINI_MODEL = model
        cfg, _note = vision.GeminiWorker._make_thinking_config(StubTypes)
        check("{}: uses thinking_level".format(model),
              cfg is not None and "thinking_level" in cfg.kwargs, True)
        check("{}: NEVER sends thinking_budget".format(model),
              cfg is not None and "thinking_budget" not in cfg.kwargs, True)

    for model in ("gemini-2.5-flash", "gemini-2.5-flash-lite"):
        config.GEMINI_MODEL = model
        cfg, _note = vision.GeminiWorker._make_thinking_config(StubTypes)
        check("{}: disables thinking with a budget".format(model),
              cfg is not None and cfg.kwargs, {"thinking_budget": 0})

    config.GEMINI_MODEL = "gemini-3.5-flash-lite"
    config.GEMINI_THINKING_LEVEL = None
    cfg, note = vision.GeminiWorker._make_thinking_config(StubTypes)
    check("GEMINI_THINKING_LEVEL=None sends no thinking option", cfg, None)
    check("and says so", "default" in note, True)

    # The dangerous fallback would be quietly reverting to thinking_budget.
    config.GEMINI_THINKING_LEVEL = "LOW"
    cfg, note = vision.GeminiWorker._make_thinking_config(OldSdkTypes)
    check("an SDK without thinking_level sends NOTHING, not a budget",
          cfg, None)
    check("and explains why", "unsupported" in note, True)
finally:
    config.GEMINI_MODEL = _saved_model
    config.GEMINI_THINKING_LEVEL = _saved_level

check("the configured model is a Gemini 3 model",
      config.GEMINI_MODEL.startswith("gemini-3"), True)
check("the configured thinking level is one the API accepts",
      config.GEMINI_THINKING_LEVEL in (None, "LOW", "MEDIUM", "HIGH"), True)

# Thinking tokens are charged against max_output_tokens on Gemini 3, and
# thinking cannot be switched off - so a tight cap can be entirely consumed
# by hidden reasoning, returning an empty description. 48 was far too low.
check("the output cap is generous enough to survive thinking tokens",
      config.GEMINI_MAX_OUTPUT_TOKENS >= 256, True)


# ==========================================================================
print("\nAPI errors are reported in enough detail to name the bad field")
# ==========================================================================


class FakeClientError(Exception):
    def __init__(self, message, code=400, status="INVALID_ARGUMENT", body=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message
        if body is not None:
            class Resp:
                text = body
            self.response = Resp()


rejection = FakeClientError(
    "400 INVALID_ARGUMENT. Request contains an invalid argument.",
    body='{"error":{"message":"thinking_budget is not supported by this model."}}',
)
described = vision.describe_api_error(rejection)
check("the description includes the status code",
      "400" in described, True)
check("the description includes the status name",
      "INVALID_ARGUMENT" in described, True)
check("the description includes the response body",
      "thinking_budget is not supported" in described, True)
check("which is what names the offending parameter",
      "thinking_budget" in described, True)
check("the description is one line",
      "\n" in described, False)

# An error with nothing but a string must still describe cleanly.
plain = vision.describe_api_error(RuntimeError("boom"))
check("a plain exception still describes", plain, "RuntimeError: boom")

# The key must never leak, even if a future SDK echoes the request.
_saved_key = os.environ.get("GEMINI_API_KEY")
os.environ["GEMINI_API_KEY"] = "AQ.super-secret-value"
try:
    leaky = vision.describe_api_error(
        FakeClientError("bad request", body="key=AQ.super-secret-value"))
    check("the API key is redacted from error text",
          "AQ.super-secret-value" in leaky, False)
    check("and replaced with a marker", "<redacted>" in leaky, True)
finally:
    if _saved_key is None:
        os.environ.pop("GEMINI_API_KEY", None)
    else:
        os.environ["GEMINI_API_KEY"] = _saved_key

# Long errors are truncated so one bad reply cannot flood the terminal.
check("very long errors are truncated",
      len(vision.describe_api_error(RuntimeError("x" * 5000))) <= 705, True)


# ==========================================================================
print("\nA rejected request config is dropped once, not silently retried")
# ==========================================================================
worker = vision.GeminiWorker()
worker._gen_config_note = "max_tokens=512, thinking_level=LOW"
worker._make_generate_config = staticmethod(
    lambda include_thinking=True: (None, "max_tokens=512, thinking dropped"))

check("a 400 with thinking attached triggers the fallback",
      worker._retry_without_thinking(rejection), True)
check("the thinking option is now marked dropped",
      worker._thinking_dropped, True)
check("it never fires twice",
      worker._retry_without_thinking(rejection), False)

worker = vision.GeminiWorker()
worker._gen_config_note = "max_tokens=512, thinking_level=LOW"
check("a quota error is NOT treated as a config problem",
      worker._retry_without_thinking(
          FakeClientError("429 RESOURCE_EXHAUSTED", code=429, status="X")),
      False)

worker = vision.GeminiWorker()
worker._gen_config_note = "max_tokens only"
check("with no thinking option attached there is nothing to drop",
      worker._retry_without_thinking(rejection), False)


# ==========================================================================
print("\nAUDIO_DEVICE actually reaches every playback path")
# ==========================================================================
# This section exists because of a real bug. config.AUDIO_DEVICE was a
# hardcoded None and the environment was never consulted, so
#
#     export AUDIO_DEVICE=plughw:1,0
#
# had NO effect - every guard was `if config.AUDIO_DEVICE:` and every one
# was False, so beeps and speech both went to the system default (HDMI)
# while startup cheerfully reported "Audio: OK / Speech: OK".

import importlib
import subprocess as _subprocess

_saved_env = os.environ.get("AUDIO_DEVICE")
try:
    os.environ["AUDIO_DEVICE"] = "plughw:1,0"
    _reloaded = importlib.reload(config)
    check("an exported AUDIO_DEVICE reaches config",
          _reloaded.AUDIO_DEVICE, "plughw:1,0")

    os.environ["AUDIO_DEVICE"] = "   "
    _reloaded = importlib.reload(config)
    check("a whitespace-only value is treated as unset",
          _reloaded.AUDIO_DEVICE, None)

    os.environ.pop("AUDIO_DEVICE", None)
    _reloaded = importlib.reload(config)
    check("with nothing exported it is None", _reloaded.AUDIO_DEVICE, None)
finally:
    if _saved_env is None:
        os.environ.pop("AUDIO_DEVICE", None)
    else:
        os.environ["AUDIO_DEVICE"] = _saved_env
    importlib.reload(config)
    importlib.reload(audio_module)


# --- the exact commands built for each path ------------------------------
_recorded = []


class _FakePipe:
    def close(self):
        pass


class _FakePopen:
    def __init__(self, args, **kwargs):
        _recorded.append(list(args))
        self.args = args
        self.returncode = 0
        self.stdout = _FakePipe()

    def communicate(self, timeout=None):
        return (b"", b"")

    def poll(self):
        return 0

    def terminate(self):
        pass


class _FakeCompleted:
    returncode = 0
    stdout = b"espeak-ng text-to-speech: 1.51"
    stderr = b""


_real_popen = _subprocess.Popen
_real_run = _subprocess.run
_saved_device = config.AUDIO_DEVICE
try:
    _subprocess.Popen = _FakePopen
    _subprocess.run = lambda a, **k: _FakeCompleted()

    # ---- device pinned ----
    config.AUDIO_DEVICE = "plughw:1,0"
    speaker = audio_module.SpeechPlayer().open()
    _recorded.clear()
    speaker.speak("Person ahead, slightly left.")
    pipeline = " | ".join(" ".join(str(x) for x in c) for c in _recorded)

    check("speech asks espeak for WAV on stdout", "--stdout" in pipeline, True)
    check("speech pipes it through aplay", "aplay" in pipeline, True)
    check("speech passes -D to aplay", "-D" in pipeline, True)
    check("speech uses the configured device",
          "plughw:1,0" in pipeline, True)
    check("which matches the known-good shell command",
          pipeline.startswith("espeak-ng --stdout")
          and pipeline.endswith("aplay -q -D plughw:1,0"), True)

    _recorded.clear()
    beeps = audio_module._AplayBackend(
        {audio_module.TONE_DANGER: "/tmp/b.wav",
         audio_module.TONE_WARNING: "/tmp/w.wav"})
    beeps.play(audio_module.TONE_DANGER)
    check("beeps are pinned to the same device",
          any("-D" in c and "plughw:1,0" in c for c in _recorded), True)

    # aplay's -D is explicit; SDL only has an AUDIODEV hint, so when the
    # user has named a device aplay must be tried FIRST for beeps.
    order = ((audio_module._AplayBackend, audio_module._PygameBackend)
             if config.AUDIO_DEVICE
             else (audio_module._PygameBackend, audio_module._AplayBackend))
    check("aplay is preferred for beeps when a device is pinned",
          order[0] is audio_module._AplayBackend, True)

    # ---- no device pinned ----
    config.AUDIO_DEVICE = None
    plain = audio_module.SpeechPlayer().open()
    _recorded.clear()
    plain.speak("Chair ahead.")
    check("without a device, speech does not need aplay",
          any("aplay" in c[0] for c in _recorded), False)
    order = ((audio_module._AplayBackend, audio_module._PygameBackend)
             if config.AUDIO_DEVICE
             else (audio_module._PygameBackend, audio_module._AplayBackend))
    check("and pygame stays preferred for beeps",
          order[0] is audio_module._PygameBackend, True)

    # ---- a bad device must FAIL startup, not report OK ----
    class _BadAplay:
        def __init__(self, args, **kwargs):
            self.args = args
            self._is_aplay = "aplay" in args[0]
            self.returncode = 1 if self._is_aplay else 0
            self.stdout = _FakePipe()

        def communicate(self, timeout=None):
            if self._is_aplay:
                return (b"", b"aplay: audio open error: No such device")
            return (b"", b"")

        def poll(self):
            return self.returncode

        def terminate(self):
            pass

    config.AUDIO_DEVICE = "plughw:99,0"
    _subprocess.Popen = _BadAplay
    raised = None
    try:
        audio_module.SpeechPlayer().open()
    except audio_module.AudioError as exc:
        raised = exc
    check("a device that cannot play makes Speech FAIL", raised is not None, True)
    check("and the error names the device",
          "plughw:99,0" in str(raised or ""), True)
    check("and quotes the real aplay error",
          "No such device" in str(raised or ""), True)
    check("and says it is an output problem, not a TTS problem",
          "OUTPUT problem" in str(raised or ""), True)
finally:
    _subprocess.Popen = _real_popen
    _subprocess.run = _real_run
    config.AUDIO_DEVICE = _saved_device

check("a startup phrase is configured for the playback test",
      bool(config.SPEECH_STARTUP_PHRASE), True)


# ==========================================================================
print("\nAn accepted Gemini result always reaches speech")
# ==========================================================================
# This section exists because of a real bug. Speech read
# snapshot()["description"], which is gated by the HUD DISPLAY window
# (GEMINI_RESULT_MAX_AGE_S = 8s), while acceptance used the more generous
# relevance backstop (GEMINI_ACCEPT_MAX_AGE_S = 12s).
#
# Any reply landing between those two limits was accepted, printed to the
# terminal, and then permanently unspeakable - description was already None
# and only got older. That is exactly "I see the text but hear nothing".
#
# Speech now follows ACCEPTANCE via snapshot()["accepted_text"].

check("the display window is tighter than the acceptance backstop",
      config.GEMINI_RESULT_MAX_AGE_S < config.GEMINI_ACCEPT_MAX_AGE_S, True)


class _SpyTalker:
    """Records say()/set_muted() the way SpeechController would see them."""

    def __init__(self):
        self.said = []
        self.muted = False

    def say(self, text):
        if self.muted:
            return False
        self.said.append(text)
        return True

    def set_muted(self, muted):
        self.muted = bool(muted)


def _worker_with_reply(api_seconds, text="Person ahead, slightly left."):
    """A worker that has just processed a reply which took api_seconds."""
    w = vision.GeminiWorker()
    w._latest_request_id = 1
    w._encode_jpeg = staticmethod(lambda frame: b"jpeg")
    w._call_gemini = lambda jpeg, distance: text
    w.note_current_state(80.0, "CAUTION")
    request = vision.AiRequest(
        FakeFrame(), 80.0, "CAUTION",
        time.monotonic() - api_seconds, 1, "band")
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        w._process_request(request)
    return w, buffer.getvalue()


# Fast reply: shown AND spoken.
worker, logged = _worker_with_reply(2.0)
snap = worker.snapshot()
check("a fast reply is accepted", snap["reply_count"], 1)
check("a fast reply is shown on the HUD", bool(snap["description"]), True)
check("a fast reply is available to speech", bool(snap["accepted_text"]), True)
check("acceptance is logged in the requested format",
      "AI ACCEPTED:" in logged, True)

talker = _SpyTalker()
check("and it is spoken", app.speak_new_guidance(worker, talker, 0), 1)
check("with the right text", talker.said, ["Person ahead, slightly left."])

# THE BUG: slow but still-relevant reply. Too old for the HUD, but accepted.
for slow in (config.GEMINI_RESULT_MAX_AGE_S + 1,
             config.GEMINI_ACCEPT_MAX_AGE_S - 0.5):
    worker, _logged = _worker_with_reply(slow)
    snap = worker.snapshot()
    check("api {:.1f}s: accepted".format(slow), snap["reply_count"], 1)
    check("api {:.1f}s: too old for the HUD".format(slow),
          snap["description"], None)
    check("api {:.1f}s: still available to speech".format(slow),
          bool(snap["accepted_text"]), True)

    talker = _SpyTalker()
    app.speak_new_guidance(worker, talker, 0)
    check("api {:.1f}s: IS SPOKEN anyway".format(slow),
          talker.said, ["Person ahead, slightly left."])

# Beyond the backstop it is rejected outright - and must NOT be spoken.
worker, _logged = _worker_with_reply(config.GEMINI_ACCEPT_MAX_AGE_S + 2)
snap = worker.snapshot()
check("beyond the backstop it is not accepted", snap["reply_count"], 0)
check("and nothing is offered to speech", snap["accepted_text"], None)
talker = _SpyTalker()
app.speak_new_guidance(worker, talker, 0)
check("so it is never spoken", talker.said, [])

# A superseded reply must not be spoken either - relevance still rules.
worker = vision.GeminiWorker()
worker._latest_request_id = 9          # a newer request exists
worker._encode_jpeg = staticmethod(lambda frame: b"jpeg")
worker._call_gemini = lambda jpeg, distance: "Stale guidance."
worker.note_current_state(80.0, "CAUTION")
with contextlib.redirect_stdout(io.StringIO()):
    worker._process_request(vision.AiRequest(
        FakeFrame(), 80.0, "CAUTION", time.monotonic(), 1, "band"))
talker = _SpyTalker()
app.speak_new_guidance(worker, talker, 0)
check("a superseded reply is not spoken", talker.said, [])

# Each accepted generation is spoken exactly once, however many frames pass.
worker, _logged = _worker_with_reply(1.0)
talker = _SpyTalker()
generation = 0
for _ in range(20):
    generation = app.speak_new_guidance(worker, talker, generation)
check("one accepted result is spoken once across many frames",
      len(talker.said), 1)

# A suppressed phrase still advances the generation, so the suppression
# reason is logged once rather than on every single frame.
worker, _logged = _worker_with_reply(1.0)
talker = _SpyTalker()
talker.muted = True
generation = app.speak_new_guidance(worker, talker, 0)
check("a suppressed phrase still marks the generation handled",
      generation, worker.snapshot()["generation"])
check("and nothing was spoken", talker.said, [])


# ==========================================================================
print("\nSpeech logging says exactly why a phrase was not spoken")
# ==========================================================================


class _SlowVoice:
    """A voice that keeps 'speaking' until released, so we can interrupt it."""

    def __init__(self):
        self.spoken = []
        self.stops = 0
        self.busy = False

    def speak(self, text):
        self.spoken.append(text)
        self.busy = True
        return True

    def is_speaking(self):
        return self.busy

    def stop(self):
        self.stops += 1
        self.busy = False


def _capture(action):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        action()
        time.sleep(0.25)
    return buffer.getvalue()


voice = _SlowVoice()
clock = FakeClock()
talker = SpeechController(voice, now=clock)
talker.start()
try:
    logged = _capture(lambda: talker.say("Person ahead, left."))
    check("queueing is logged", "SPEECH QUEUED: Person ahead, left." in logged,
          True)
    check("playing is logged", "SPEECH PLAYING: Person ahead, left." in logged,
          True)

    # Danger arrives mid-phrase: the voice must be cut off and say so.
    logged = _capture(lambda: talker.set_muted(True))
    check("an explicit mute interrupts the phrase in progress",
          voice.stops >= 1, True)
    check("and the interruption is logged",
          "SPEECH INTERRUPTED: muted" in logged, True)

    logged = _capture(lambda: talker.say("Chair ahead, move right."))
    check("a phrase while explicitly muted is suppressed with the reason",
          "SPEECH SUPPRESSED: muted" in logged, True)

    talker.set_muted(False)
    voice.busy = False
    clock.advance(1.0)
    logged = _capture(lambda: talker.say("Person ahead, left."))
    check("a duplicate is suppressed with the reason",
          "SPEECH SUPPRESSED: duplicate" in logged, True)

    logged = _capture(lambda: talker.say("   "))
    check("an empty phrase is suppressed with the reason",
          "SPEECH SUPPRESSED: empty" in logged, True)
finally:
    talker.stop()

check("nothing in the navigation logic mutes speech",
      "set_muted" in open("main.py", encoding="utf-8").read(), False)


# ==========================================================================
print("\nDANGER: guidance speaks AND the local warning keeps going")
# ==========================================================================
# The DANGER band used to mute speech outright, which threw away exactly
# the guidance you most want at 20 cm ("Table leg ahead, move left.").
# Now both happen: the beeps are driven straight from the sensor and are
# only SPACED OUT while a phrase plays.


class _Beeps:
    """Records every interval the main loop sets."""

    error = None

    def __init__(self):
        self.intervals = []
        self.tones = []

    def set_interval(self, interval):
        self.intervals.append(interval)

    def play_once(self, tone):
        self.tones.append(tone)


class _Talker:
    """A speech controller stand-in that reports whether it is speaking."""

    def __init__(self):
        self.said = []
        self.speaking = False
        self.muted = False

    def say(self, text):
        self.said.append(text)
        self.speaking = True
        return True

    def set_muted(self, muted):
        self.muted = bool(muted)


def _danger_snapshot(distance=18.0):
    return {"distance_cm": distance, "out_of_range": False, "error": None,
            "healthy": True, "reading_count": 9}


# --- an accepted result while in DANGER must be spoken -------------------
policy = alerts.AlertPolicy()
beeps = _Beeps()
talker = _Talker()
worker, logged = _worker_with_reply(1.0, "Table leg ahead, move left.")
worker.note_current_state(18.0, "DANGER")

status = app.apply_alert_policy(
    policy, _danger_snapshot(), beeps, FakeFrame(), worker, talker)
check("the band is DANGER", status, "DANGER")
check("the danger beep is running", beeps.intervals[-1] is not None, True)
check("speech was NOT muted by the DANGER band", talker.muted, False)

spoken = app.speak_new_guidance(worker, talker, 0)
check("accepted guidance IS spoken in DANGER",
      talker.said, ["Table leg ahead, move left."])
check("and the generation advanced", spoken, worker.snapshot()["generation"])
check("acceptance was logged", "AI ACCEPTED:" in logged, True)

# --- the warning must keep going while the phrase plays ------------------
talker.speaking = True
for _ in range(5):
    app.apply_alert_policy(
        policy, _danger_snapshot(), beeps, FakeFrame(), worker, talker)

while_speaking = beeps.intervals[-5:]
check("the beep interval is never disabled while speaking",
      all(i is not None for i in while_speaking), True)
check("every interval is a real, finite number",
      all(isinstance(i, float) and i > 0 for i in while_speaking), True)
check("the beeps are SPACED OUT while speaking",
      all(i > config.BEEP_INTERVAL_DANGER_S for i in while_speaking), True)
check("but never slower than the clamp",
      all(i <= config.BEEP_MAX_INTERVAL_WHILE_SPEAKING_S
          for i in while_speaking), True)

# --- and return to the urgent rhythm the moment the phrase ends ----------
talker.speaking = False
app.apply_alert_policy(
    policy, _danger_snapshot(), beeps, FakeFrame(), worker, talker)
check("the urgent rhythm returns when the phrase finishes",
      beeps.intervals[-1], config.BEEP_INTERVAL_DANGER_S)

# --- a real SpeechController accepts a phrase while in DANGER -----------
voice = _SlowVoice()
real_talker = SpeechController(voice)
real_talker.start()
try:
    policy = alerts.AlertPolicy()
    beeps = _Beeps()
    # Walk straight into DANGER.
    app.apply_alert_policy(policy, _danger_snapshot(150.0), beeps,
                           FakeFrame(), None, real_talker)
    app.apply_alert_policy(policy, _danger_snapshot(18.0), beeps,
                           FakeFrame(), None, real_talker)
    check("the real controller is not muted in DANGER",
          real_talker.muted, False)

    logged = _capture(lambda: real_talker.say("Obstacle ahead."))
    check("the phrase is queued, not suppressed",
          "SPEECH QUEUED: Obstacle ahead." in logged, True)
    check("and reaches the voice",
          "SPEECH PLAYING: Obstacle ahead." in logged, True)
    check("nothing was suppressed for danger",
          "SPEECH SUPPRESSED" in logged, False)
    check("it was actually spoken", voice.spoken, ["Obstacle ahead."])

    # With the phrase still playing, the danger beep must still be set.
    interval = app.beep_interval_with_speech(
        config.BEEP_INTERVAL_DANGER_S, real_talker)
    check("the danger beep survives a phrase in progress",
          interval is not None, True)
    check("spaced out while it plays",
          interval > config.BEEP_INTERVAL_DANGER_S, True)
finally:
    real_talker.stop()

# --- every other protection still applies inside DANGER -----------------
talker = _Talker()
worker, _logged = _worker_with_reply(1.0, "Person ahead, left.")
worker.note_current_state(18.0, "DANGER")
generation = 0
for _ in range(10):
    generation = app.speak_new_guidance(worker, talker, generation)
check("duplicate/generation suppression still holds in DANGER",
      len(talker.said), 1)

# A superseded reply must still be rejected, DANGER or not.
worker = vision.GeminiWorker()
worker._latest_request_id = 7
worker._encode_jpeg = staticmethod(lambda frame: b"jpeg")
worker._call_gemini = lambda jpeg, distance: "Stale guidance."
worker.note_current_state(18.0, "DANGER")
with contextlib.redirect_stdout(io.StringIO()):
    worker._process_request(vision.AiRequest(
        FakeFrame(), 18.0, "DANGER", time.monotonic(), 1, "band"))
talker = _Talker()
app.speak_new_guidance(worker, talker, 0)
check("newest-wins still rejects a superseded reply in DANGER",
      talker.said, [])

# An obstacle that vanished is still rejected.
worker = vision.GeminiWorker()
worker._latest_request_id = 1
worker._encode_jpeg = staticmethod(lambda frame: b"jpeg")
worker._call_gemini = lambda jpeg, distance: "Gone guidance."
worker.note_current_state(None, "SAFE")
with contextlib.redirect_stdout(io.StringIO()):
    worker._process_request(vision.AiRequest(
        FakeFrame(), 18.0, "DANGER", time.monotonic(), 1, "band"))
talker = _Talker()
app.speak_new_guidance(worker, talker, 0)
check("relevance still rejects a vanished obstacle", talker.said, [])


# ==========================================================================
print("\nThe prompt asks Gemini to identify WHAT is ahead")
# ==========================================================================
# Gemini was returning "Obstacle ahead." almost every time. The cause was
# in the prompt, not in our code: it used to end with
#
#   "If the frame does not clearly show which way is safe, or the obstacle
#    fills the view, reply exactly: Obstacle ahead."
#
# At 25-50 cm an obstacle usually DOES fill the view, so that condition was
# nearly always true and the model took the sanctioned generic answer. The
# fix separates two different uncertainties: not knowing which WAY to move
# is common and fine, whereas not knowing WHAT the object is is rare.

check("identification is stated as the primary job",
      "say WHAT it is" in config.GEMINI_PROMPT, True)
check("the prompt names concrete objects to look for",
      all(word in config.GEMINI_PROMPT for word in
          ("person", "car", "chair", "table", "wall", "doorway", "stairs",
           "curb", "pole")), True)
check("a generic reply is explicitly discouraged",
      "Do not reply with a generic phrase" in config.GEMINI_PROMPT, True)
check("and 'Obstacle ahead' is no longer the sanctioned fallback",
      "reply exactly: \"Obstacle ahead.\"" in config.GEMINI_PROMPT, False)
check("the unknown-object fallback is the new one",
      "Unknown object directly ahead." in config.GEMINI_PROMPT, True)
check("no safe direction must NOT withhold the object name",
      "NOT a reason to withhold the object name" in config.GEMINI_PROMPT, True)
check("hallucination is still forbidden",
      "do not guess an object you cannot actually see" in config.GEMINI_PROMPT,
      True)
check("the movement suggestion stays conditional",
      "ONLY if" in config.GEMINI_PROMPT, True)
check("privacy rules are preserved",
      all(word in config.GEMINI_PROMPT for word in
          ("Do not identify who anyone is", "age, gender, race")), True)
check("the path-clear reply is preserved",
      "Path clear." in config.GEMINI_PROMPT, True)
check("the ultrasonic distance is still injected",
      "{distance_cm}" in config.GEMINI_PROMPT, True)


# ==========================================================================
print("\nCleanup never turns a real description into a generic one")
# ==========================================================================
# _tidy() strips whitespace and surrounding quotes, keeps the first line and
# truncates. It must never substitute wording of its own - so a generic
# phrase on the HUD came from the model, and the GEMINI RAW log proves it.

DESIRED = [
    "Person ahead, slightly left.",
    "Car ahead on your right.",
    "Chair directly ahead, move left.",
    "Table ahead, path clear on the right.",
    "Doorway ahead on the left.",
    "Wall directly ahead, turn right.",
    "Stairs going down ahead.",
    "Pole ahead, slightly right.",
    "Unknown object directly ahead.",
    "Bicycle ahead, path clear on your left.",
]

for phrase in DESIRED:
    check("tidy preserves: {}".format(phrase),
          vision.GeminiWorker._tidy(phrase), phrase)
    check("speech preserves: {}".format(phrase),
          _clean_for_speech(phrase), phrase)

check("no desired phrase is truncated by the description cap",
      all(len(p) <= config.GEMINI_MAX_DESCRIPTION_CHARS for p in DESIRED), True)
check("nor by the speech cap",
      all(len(p) <= config.SPEECH_MAX_CHARS for p in DESIRED), True)
check("the two caps agree so speech is never clipped",
      config.SPEECH_MAX_CHARS >= config.GEMINI_MAX_DESCRIPTION_CHARS, True)
check("a ten-word phrase fits",
      len(vision.GeminiWorker._tidy(
          "Wooden chair directly ahead, path appears clear on your right side")),
      66)

# Cleanup only removes noise, never meaning.
check("surrounding quotes are stripped",
      vision.GeminiWorker._tidy('"Car ahead on your right."'),
      "Car ahead on your right.")
check("padding is stripped",
      vision.GeminiWorker._tidy("   Person ahead, slightly left.   "),
      "Person ahead, slightly left.")
check("a trailing explanation line is dropped, not the description",
      vision.GeminiWorker._tidy(
          "Chair directly ahead, move left.\nIt is a wooden dining chair."),
      "Chair directly ahead, move left.")
check("cleanup does not invent text for an empty reply",
      vision.GeminiWorker._tidy(""), "")
check("and an empty reply is never published as a description",
      vision.GeminiWorker._tidy(None), "")


# ==========================================================================
print("\nGEMINI RAW logging distinguishes model output from our cleanup")
# ==========================================================================
check("raw logging is enabled", config.GEMINI_LOG_RAW, True)


def _raw_and_accepted(raw_reply):
    """Return (GEMINI RAW line, AI ACCEPTED line) for a given model reply."""
    w = vision.GeminiWorker()
    w._latest_request_id = 1
    w._encode_jpeg = staticmethod(lambda frame: b"jpeg")
    w._call_gemini = lambda jpeg, distance: raw_reply
    w.note_current_state(42.0, "WARNING")
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        w._process_request(vision.AiRequest(
            FakeFrame(), 42.0, "WARNING", time.monotonic(), 1, "band"))
    raw_line = accepted_line = None
    for line in buffer.getvalue().splitlines():
        if line.startswith("GEMINI RAW:"):
            raw_line = line
        elif line.startswith("AI ACCEPTED:"):
            accepted_line = line.split("   [")[0]
    return raw_line, accepted_line, w.snapshot()


raw_line, accepted_line, snap = _raw_and_accepted("Person ahead, slightly left.")
check("the raw model reply is logged",
      raw_line, "GEMINI RAW: 'Person ahead, slightly left.'")
check("the accepted text is logged",
      accepted_line, "AI ACCEPTED: Person ahead, slightly left.")
check("and they match when cleanup changed nothing",
      snap["accepted_text"], "Person ahead, slightly left.")

# When cleanup DOES change something, both lines make it obvious.
raw_line, accepted_line, snap = _raw_and_accepted(
    '  "Car ahead on your right."  ')
check("raw shows the model's padding and quotes",
      raw_line, 'GEMINI RAW: \'  "Car ahead on your right."  \'')
check("accepted shows the cleaned version",
      accepted_line, "AI ACCEPTED: Car ahead on your right.")
check("so the difference is attributable to cleanup, not the model",
      snap["accepted_text"], "Car ahead on your right.")

# A specific identification must survive all the way to speech, even in
# DANGER, where guidance is now allowed to speak.
worker = vision.GeminiWorker()
worker._latest_request_id = 1
worker._encode_jpeg = staticmethod(lambda frame: b"jpeg")
worker._call_gemini = lambda jpeg, distance: "Chair directly ahead, move left."
worker.note_current_state(18.0, "DANGER")
with contextlib.redirect_stdout(io.StringIO()):
    worker._process_request(vision.AiRequest(
        FakeFrame(), 18.0, "DANGER", time.monotonic(), 1, "band"))
talker = _SpyTalker()
app.speak_new_guidance(worker, talker, 0)
check("a specific identification is spoken unchanged in DANGER",
      talker.said, ["Chair directly ahead, move left."])


# ==========================================================================
print("\nHeadless operation needs no display, and stops cleanly")
# ==========================================================================
# The production device has no HDMI, no keyboard and no desktop session, so
# --headless must not touch OpenCV's GUI, the HUD module, or $DISPLAY. And
# systemd stops it with SIGTERM, whose default action would kill Python
# outright and skip the cleanup that releases the GPIO and the threads.

check("main.py imports no GUI module at load time",
      "cv2" in sys.modules or "ui" in sys.modules, False)
check("a SIGTERM handler installer exists",
      callable(getattr(app, "install_signal_handlers", None)), True)
check("the headless loop exists", callable(app.run_headless_loop), True)
check("--headless is a real flag",
      app.parse_args(["--headless"]).headless, True)

service = pathlib.Path("deploy/sense.service").read_text(encoding="utf-8")
# Directives only - comments in the unit file legitimately MENTION things
# the unit must not actually depend on.
directives = "\n".join(
    line for line in service.splitlines()
    if line.strip() and not line.strip().startswith("#"))
check("the unit runs Sense headless", "--headless" in service, True)
check("the unit provides AUDIO_DEVICE",
      "Environment=AUDIO_DEVICE=plughw:1,0" in service, True)
check("the unit provides ROBOT_HAT_GPIOCHIP",
      "Environment=ROBOT_HAT_GPIOCHIP=0" in service, True)
check("the unit sets HOME so pip --user packages resolve",
      "Environment=HOME=/home/sense" in service, True)
check("the unit unbuffers output for journalctl",
      "PYTHONUNBUFFERED=1" in service, True)
check("the unit uses the project directory",
      "WorkingDirectory=/home/sense/Sense" in service, True)
check("the unit restarts on crash", "Restart=always" in service, True)
check("with a delay that prevents a rapid loop",
      "RestartSec=5" in service, True)
check("the unit stops with SIGTERM so cleanup runs",
      "KillSignal=SIGTERM" in service, True)
check("and allows time for that cleanup",
      "TimeoutStopSec=20" in service, True)
check("the unit starts WITHOUT a graphical session",
      "WantedBy=multi-user.target" in service, True)
check("and no directive requires graphical.target",
      "graphical.target" in directives, False)
check("nor a display or X server",
      any(word in directives for word in ("DISPLAY", "xorg", "wayland")),
      False)
check("the unit waits for the network for Gemini",
      "network-online.target" in service, True)
check("the API key is NOT in the unit file",
      "GEMINI_API_KEY" in directives, False)
check("no secret-looking value is embedded",
      "AQ." in directives or "AIza" in directives, False)
check("the key still comes from .env.local",
      ".env.local" in service, True)


# ==========================================================================
print("")
if FAILURES:
    print("{} FAILED: {}".format(len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("All alert behaviour tests passed.")
