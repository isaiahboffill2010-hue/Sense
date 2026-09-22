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
from hardware.audio import TONE_DANGER, TONE_WARNING, BeepController
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
        w._process_request(request)
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
w._process_request(vision.AiRequest(FakeFrame(), 80.0, time.monotonic()))
check("empty reply is not shown", w.snapshot()["description"], None)


# ==========================================================================
print("\nGemini worker: stale results are discarded, never displayed")
# ==========================================================================
w = vision.GeminiWorker()
w._encode_jpeg = staticmethod(lambda frame: b"fake-jpeg")
w._call_gemini = lambda jpeg, distance: "Chair directly ahead."

# Fresh reply -> shown.
w._process_request(vision.AiRequest(FakeFrame(), 80.0, time.monotonic()))
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
w2._process_request(vision.AiRequest(FakeFrame(), 80.0, old_capture))
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
_started._encode_jpeg = staticmethod(lambda frame: b"fake-jpeg")
_started._call_gemini = lambda jpeg, distance: "Wall ahead."
_started.start()
try:
    _started._process_request(
        vision.AiRequest(FakeFrame(), 80.0, time.monotonic()))
    check("_process_request works on a running thread",
          _started.snapshot()["description"], "Wall ahead.")
finally:
    _started.stop()


# ==========================================================================
print("")
if FAILURES:
    print("{} FAILED: {}".format(len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("All alert behaviour tests passed.")
