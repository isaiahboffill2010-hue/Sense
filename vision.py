"""
vision.py
=========

Gemini vision for the navigation headband (Phase 2).

What this does
--------------
When the ultrasonic sensor reports that an obstacle has just moved closer,
the main loop hands a COPY of the camera frame it already has to this
module. A background thread turns that frame into a JPEG, asks Gemini for a
very short navigation phrase, and publishes the answer for the HUD to read.

    "Person ahead."   "Chair directly ahead."   "Stairs descending ahead."

Design rules this module is built around
----------------------------------------
* Gemini is OPTIONAL. The ultrasonic sensor and the local beeps are the
  safety system. Nothing here may delay them, and every failure mode -
  no internet, DNS failure, timeout, API error, quota, malformed reply,
  unexpected exception - is caught and turned into a status line.

* NOTHING BLOCKS THE MAIN THREAD. The main loop only ever calls request()
  (one bounded put_nowait) and snapshot() (one lock-protected dict copy).
  JPEG encoding and the network call both happen on the worker thread.

* NO SECOND CAMERA. This module never touches Picamera2. It only receives
  frames the main loop already captured.

* NEWEST WINS, NOTHING STACKS. The queue holds at most one item. A new
  request replaces whatever was waiting, and an older reply still in
  flight is recognised by its request id and thrown away on arrival.
  There is deliberately no backlog: only the newest request can ever be
  accepted.

* RELEVANCE, NOT A STOPWATCH. A slow reply is not automatically a wrong
  one - "Chair ahead, move right" that took six seconds is still correct
  if the chair is still 45 cm ahead. So a finished reply is compared
  against live sensor state (see _relevance_problem) and rejected if it
  was superseded, the obstacle is gone, or the distance moved materially.
  Age is only the last-resort backstop for a hung socket.

* THE MODEL IS NEVER ASKED TO GUESS DISTANCE. The ultrasonic reading is
  put into the prompt, so Gemini only has to identify what the obstacle is
  and where it sits in the frame.

The accepted description is published for the HUD and for the speech
thread in hardware/audio.py. Everything here stays off the immediate
collision-warning path: the beeps are driven straight from the ultrasonic
reading and never wait on any of this.
"""

import collections
import os
import queue
import threading
import time
from pathlib import Path

import config


class VisionError(RuntimeError):
    """Raised when the Gemini client cannot be set up at all."""


# One pending analysis. Everything needed to judge, when the reply finally
# arrives, whether it still describes the world the user is standing in:
#
#   request_id    monotonic counter - a newer request always wins
#   distance_cm   ultrasonic reading at capture (also sent to the model)
#   status        band at capture (SAFE / CAUTION / WARNING / DANGER)
#   captured_at   when the shutter effectively closed
AiRequest = collections.namedtuple(
    "AiRequest",
    ["frame", "distance_cm", "status", "captured_at", "request_id", "reason"],
)

# Bands in which a navigation description is no longer worth showing.
IRRELEVANT_STATUSES = ("SAFE", "UNKNOWN")

# Gemini worker states, for the HUD.
STATE_IDLE = "IDLE"
STATE_THINKING = "THINKING"
STATE_DISCARDED = "DISCARDED"


# ==========================================================================
# API key loading
# ==========================================================================
def load_env_file(path=None, environ=None):
    """Load KEY=VALUE lines from .env.local into the environment.

    Standard library only - no python-dotenv dependency, which keeps this
    consistent with the rest of the project's "almost nothing from pip"
    approach.

    An already-set environment variable always wins, so exporting the key in
    your shell overrides the file.

    Returns the list of key NAMES that were loaded. Never returns, logs or
    prints a value.
    """
    path = Path(path) if path is not None else config.ENV_FILE_PATH
    environ = os.environ if environ is None else environ

    try:
        # utf-8-sig transparently strips a BOM if the file was written by a
        # Windows editor.
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError:
        return []          # no file is fine - the key may be exported already

    loaded = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()

        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]

        # Credentials never contain whitespace, and this file only ever holds
        # the API key. Removing any internal whitespace defends against a key
        # that was pasted with a line wrap or carried a stray carriage return
        # over from a Windows editor - both of which would otherwise be sent
        # verbatim and rejected as malformed.
        value = "".join(value.split())

        if key and key not in environ:
            environ[key] = value
            loaded.append(key)

    return loaded


def api_key_is_present(environ=None):
    """True if the Gemini API key is set. Never reveals the value."""
    environ = os.environ if environ is None else environ
    return bool(environ.get(config.GEMINI_API_KEY_ENV, "").strip())


def key_fingerprint(environ=None):
    """Describe the key's SHAPE for diagnostics. Never reveals the value.

    Google AI Studio now issues "authorization keys" beginning with `AQ.`,
    replacing the legacy `AIza` API keys. Both are sent the same way - in
    the `x-goog-api-key` header - but a key that is truncated or in an
    unexpected format fails with errors that look like an auth-method
    problem, so it is worth being able to see the shape at a glance.

    Documented format: AQ. followed by 40 or more URL-safe characters.
    """
    environ = os.environ if environ is None else environ
    key = environ.get(config.GEMINI_API_KEY_ENV, "").strip()
    if not key:
        return "not set"

    if key.startswith("AQ."):
        shape = "AQ. authorization key"
        if len(key) < 43:
            shape += " (SHORTER than the documented minimum - truncated?)"
    elif key.startswith("AIza"):
        shape = "AIza legacy API key"
    else:
        shape = "unrecognised format"

    return "{}, {} chars".format(shape, len(key))


def _redact(text, secret):
    """Strip a secret out of text before it is ever printed or stored."""
    rendered = str(text)
    if secret:
        rendered = rendered.replace(secret, "<redacted>")
    return rendered


def describe_api_error(exc, limit=700):
    """Everything useful a google-genai error carries, as one line.

    str(exc) alone is uselessly terse for a rejected request - it says
    "400 INVALID_ARGUMENT. Request contains an invalid argument." and stops,
    which tells you nothing about WHICH argument. The structured attributes
    and the raw response body are where the field violations live, so pull
    those out too.

    The API key is stripped out, in case a future SDK echoes the request.
    """
    parts = ["{}: {}".format(type(exc).__name__, exc)]

    for attribute in ("code", "status", "message"):
        value = getattr(exc, attribute, None)
        if value not in (None, ""):
            text = " ".join(str(value).split())
            if text and text not in parts[0]:
                parts.append("{}={}".format(attribute, text))

    details = getattr(exc, "details", None)
    if details:
        parts.append("details={}".format(" ".join(str(details).split())))

    # The HTTP body usually names the offending field.
    response = getattr(exc, "response", None)
    for attribute in ("text", "content", "body"):
        raw = getattr(response, attribute, None) if response is not None else None
        if raw:
            try:
                body = raw.decode("utf-8", "replace") if isinstance(
                    raw, (bytes, bytearray)) else str(raw)
            except Exception:
                continue
            body = " ".join(body.split())
            if body and body not in parts[0]:
                parts.append("body={}".format(body))
            break

    rendered = "  |  ".join(parts)
    secret = os.environ.get(config.GEMINI_API_KEY_ENV, "").strip()
    rendered = _redact(rendered, secret)
    if len(rendered) > limit:
        rendered = rendered[:limit] + "..."
    return rendered


# ==========================================================================
# The worker
# ==========================================================================
class GeminiWorker(threading.Thread):
    """Background thread that turns camera frames into short descriptions.

    Usage from the main loop:

        worker = GeminiWorker().open()      # raises VisionError if unusable
        worker.start()
        ...
        worker.request(frame, distance_cm)  # returns immediately
        result = worker.snapshot()          # returns immediately
        ...
        worker.stop()
    """

    # How long the run loop waits for work before re-checking the stop flag.
    POLL_S = 0.2

    def __init__(self):
        super().__init__(name="gemini", daemon=True)
        self._queue = queue.Queue(maxsize=1)
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        self._client = None
        self._gen_config = None
        self._gen_config_note = "defaults"
        self._thinking_dropped = False
        self.description = ""      # how the client is configured, for the UI
        self.prewarm_note = None

        # Published state, all guarded by _lock.
        self._text = None
        self._captured_at = None
        self._distance_cm = None
        self._status = None
        self._generation = 0          # bumps on every ACCEPTED result
        self._error = None
        self._busy = False

        # Request identity. The newest request always wins, so an older
        # reply that arrives late can be recognised and thrown away.
        self._next_request_id = 0
        self._latest_request_id = 0

        # Live sensor context, refreshed by the main loop each frame. This
        # is what a finished reply gets compared against.
        self._current_distance_cm = None
        self._current_status = None

        # Timings and outcomes, for the HUD and the terminal log.
        self._last_encode_ms = None
        self._last_api_ms = None
        self._last_age_s = None
        self._last_discard_reason = None
        self._last_discard_at = None

        self._request_count = 0
        self._reply_count = 0
        self._replaced_count = 0      # queued requests superseded before running
        self._discarded_count = 0     # replies rejected as no longer relevant
        self._error_count = 0
        self._last_logged_error = None
        self._last_logged_error_at = None

    # ------------------------------------------------------------ setup
    def open(self):
        """Create the Gemini client. Raises VisionError if that is impossible."""
        if not api_key_is_present():
            raise VisionError(
                "{} is not set. Put it in .env.local as\n"
                "      {}=your-key-here\n"
                "  or export it in your shell. The file is already gitignored."
                .format(config.GEMINI_API_KEY_ENV, config.GEMINI_API_KEY_ENV)
            )

        try:
            from google import genai
        except ImportError as exc:
            raise VisionError(
                "The google-genai package is not installed ({}).\n"
                "  Install it on the Raspberry Pi with:\n"
                "      pip3 install --user --break-system-packages google-genai"
                .format(exc)
            ) from exc

        self._client, self.description = self._make_client(genai)
        self._gen_config, self._gen_config_note = self._make_generate_config()
        self.description += " [{}]".format(self._gen_config_note)
        return self

    @staticmethod
    def _make_generate_config(include_thinking=True):
        """Build the GenerateContentConfig for every request.

        The thinking option is the part that has to be model-aware, and
        getting it wrong is rejected before the model is even reached:

            Gemini 2.5   thinking_budget=0       disables thinking
            Gemini 3     thinking_level="LOW"    thinking CANNOT be
                                                 disabled, and sending
                                                 thinking_budget at all
                                                 returns 400 INVALID_ARGUMENT

        Construction is guarded, but note that construction succeeding
        proves nothing: the SDK happily builds a config the SERVER will
        reject. That is exactly how the 400 got shipped. So callers must
        also handle rejection at request time - see _call_gemini.
        """
        try:
            from google.genai import types
        except Exception:
            return None, "no response config"

        fields = {
            "max_output_tokens": config.GEMINI_MAX_OUTPUT_TOKENS,
            "temperature": 0.0,
        }
        applied = ["max_tokens={}".format(config.GEMINI_MAX_OUTPUT_TOKENS)]

        if include_thinking:
            thinking, note = GeminiWorker._make_thinking_config(types)
            if thinking is not None:
                fields["thinking_config"] = thinking
            applied.append(note)
        else:
            applied.append("thinking option dropped")

        try:
            return types.GenerateContentConfig(**fields), ", ".join(applied)
        except Exception:
            pass

        # Something in there was not accepted; retry with the safe minimum.
        try:
            return (
                types.GenerateContentConfig(
                    max_output_tokens=config.GEMINI_MAX_OUTPUT_TOKENS),
                "max_tokens only",
            )
        except Exception:
            return None, "no response config"

    @staticmethod
    def _make_thinking_config(types):
        """The thinking option this MODEL accepts, or (None, why not).

        Gemini 3 models are thinking-only. They take thinking_level and
        reject thinking_budget outright; Gemini 2.5 models are the reverse.
        Sending both is also an error, so exactly one form is used.
        """
        model = str(config.GEMINI_MODEL).lower()
        is_gemini_3 = model.startswith("gemini-3")

        if is_gemini_3:
            level = config.GEMINI_THINKING_LEVEL
            if not level:
                return None, "thinking at model default"
            try:
                return (types.ThinkingConfig(thinking_level=str(level).upper()),
                        "thinking_level={}".format(str(level).upper()))
            except Exception:
                # Older SDK with no thinking_level field: send nothing
                # rather than the thinking_budget this model would reject.
                return None, "thinking_level unsupported by SDK"

        if config.GEMINI_DISABLE_THINKING:
            try:
                return types.ThinkingConfig(thinking_budget=0), "thinking off"
            except Exception:
                return None, "thinking_budget unsupported by SDK"

        return None, "thinking at model default"

    @staticmethod
    def _effective_timeout_s():
        """The SDK timeout to actually use, in seconds.

        Gemini rejects a deadline below 10 seconds outright:

            400 INVALID_ARGUMENT: Manually set deadline 8s is too short.
                                  Minimum allowed deadline is 10s.

        That failure happens before the model is reached, so it looks like a
        request problem rather than a configuration one. Clamping here means
        a too-low value in config.py degrades to the minimum instead of
        breaking every single call.
        """
        return max(config.GEMINI_REQUEST_TIMEOUT_S, config.GEMINI_MIN_TIMEOUT_S)

    @staticmethod
    def _make_client(genai):
        """Build a Gemini DEVELOPER API client with explicit credentials.

        Everything that decides how we authenticate is passed explicitly
        here. Nothing is left to the SDK's environment auto-detection.

        That matters because an earlier version of this function called a
        bare genai.Client(), and the very first real request came back as

            401 UNAUTHENTICATED / ACCESS_TOKEN_TYPE_UNSUPPORTED
            "Request is missing required authentication credentials."

        which is Google's generic "no usable credential arrived in the
        expected header" response. Our key was loaded and present in the
        environment, so somewhere between os.environ and the wire the SDK
        did not attach it as an API key. Passing it directly removes the
        guesswork: there is now exactly one place the credential can come
        from, and it is the key our own .env.local loader read.

        We also pin the backend off Vertex / Enterprise. Those backends
        authenticate with OAuth and reject API keys outright, and the flag
        that selects them has been renamed across SDK releases - so we pass
        whichever name this installed build actually accepts.

        A note on `AQ.` keys, so nobody "fixes" this the wrong way later:
        Google AI Studio now issues authorization keys beginning with `AQ.`
        in place of the legacy `AIza` keys. They are NOT OAuth tokens and
        must NOT be sent as `Authorization: Bearer`. They travel in exactly
        the same `x-goog-api-key` header as the old keys, which is what
        passing `api_key=` here does. Sending an `AQ.` key as a bearer token
        is a known failure mode that produces misleading 400/401 errors, so
        switching this to Bearer would make things worse, not better.
        """
        import inspect

        # Read OUR key. Deliberately not GOOGLE_API_KEY, which the SDK would
        # otherwise silently prefer over GEMINI_API_KEY.
        api_key = os.environ.get(config.GEMINI_API_KEY_ENV, "").strip()
        if not api_key:
            raise VisionError(
                "{} is empty at client construction time.".format(
                    config.GEMINI_API_KEY_ENV
                )
            )

        # Only pass arguments this build of the SDK actually accepts.
        try:
            parameters = inspect.signature(genai.Client).parameters
            takes_anything = any(
                p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()
            )
        except (TypeError, ValueError):
            parameters, takes_anything = {}, False

        def accepted(name):
            return takes_anything or name in parameters

        kwargs = {"api_key": api_key}

        pinned = []
        for flag in ("vertexai", "enterprise"):
            if accepted(flag):
                kwargs[flag] = False
                pinned.append(flag)

        timeout_ms = int(GeminiWorker._effective_timeout_s() * 1000)
        timeout_note = "no SDK timeout, {:.0f}s worker deadline only".format(
            config.GEMINI_DEADLINE_S
        )
        if accepted("http_options"):
            try:
                from google.genai import types

                kwargs["http_options"] = types.HttpOptions(timeout=timeout_ms)
                timeout_note = "SDK timeout {} ms".format(timeout_ms)
            except Exception:
                kwargs.pop("http_options", None)

        try:
            client = genai.Client(**kwargs)
        except Exception as exc:
            # Never let a secret escape in an exception message.
            raise VisionError(
                "could not build the Gemini client: {}: {}".format(
                    type(exc).__name__, _redact(exc, api_key)
                )
            ) from None

        # Report what we actually got, not what we hoped for. If a future
        # SDK ignores the pin, this says so instead of quietly claiming
        # "Developer API" while talking to an OAuth endpoint.
        if getattr(client, "vertexai", False) or getattr(client, "enterprise", False):
            backend = "WARNING: resolved to Vertex/Enterprise, not Developer API"
        else:
            backend = "Developer API"

        detail = ", ".join([backend, "x-goog-api-key", key_fingerprint(),
                            timeout_note])
        if pinned:
            detail += ", pinned via {}=False".format("/".join(pinned))

        return client, "{} ({})".format(config.GEMINI_MODEL, detail)

    # ---------------------------------------------------------- requests
    def note_current_state(self, distance_cm, status):
        """Tell the worker what the sensor says RIGHT NOW.

        Called from the main loop every frame. Cheap: one lock, two stores.
        This is the reference a finished reply is judged against, which is
        what lets us reject advice that no longer matches the world instead
        of relying on a stopwatch.
        """
        with self._lock:
            self._current_distance_cm = distance_cm
            self._current_status = status

    def request(self, frame, distance_cm, status=None, reason=None):
        """Queue one analysis of `frame`. Returns True if it was accepted.

        Called from the MAIN thread and deliberately cheap: one numpy copy
        and a bounded queue swap. The copy is essential because
        ui.draw_hud() mutates the frame in place immediately afterwards -
        without it Gemini would receive an image with the HUD burned in, and
        the worker would be reading an array the main thread is still
        drawing on.

        NEWEST WINS. If an older request is still sitting in the queue it is
        thrown away and replaced, and if one is already in flight its reply
        will be rejected when it arrives. Stale navigation advice is worse
        than none, so there is deliberately no backlog: the queue holds at
        most one item and only the newest request can ever be accepted.
        """
        if frame is None:
            return False

        try:
            copied = frame.copy()
        except Exception as exc:
            self._record_error("could not copy frame: {}".format(exc))
            return False

        with self._lock:
            self._next_request_id += 1
            request_id = self._next_request_id
            self._latest_request_id = request_id
            self._request_count += 1

        item = AiRequest(
            frame=copied,
            distance_cm=distance_cm,
            status=status,
            captured_at=time.monotonic(),
            request_id=request_id,
            reason=reason,
        )

        # Discard anything still waiting - it is by definition older.
        try:
            self._queue.get_nowait()
            with self._lock:
                self._replaced_count += 1
        except queue.Empty:
            pass

        try:
            self._queue.put_nowait(item)
        except queue.Full:
            # The worker grabbed an item between our get and put. The
            # in-flight request will be rejected on arrival anyway because
            # its id is no longer the latest, so nothing stacks up.
            with self._lock:
                self._replaced_count += 1
            return False

        return True

    # ------------------------------------------------------------ results
    def snapshot(self):
        """Thread-safe view of the latest result. Never raises.

        `description` is None unless there is a CURRENT description. Age is
        measured from when the image was captured, so a reply that took too
        long, or one that has simply been on screen too long, disappears on
        its own without anything else having to remember to clear it.
        """
        now = time.monotonic()
        with self._lock:
            text = self._text
            captured_at = self._captured_at
            distance_cm = self._distance_cm
            status = self._status
            generation = self._generation
            error = self._error
            busy = self._busy
            discard_reason = self._last_discard_reason
            discard_at = self._last_discard_at
            encode_ms = self._last_encode_ms
            api_ms = self._last_api_ms
            counts = (
                self._request_count,
                self._reply_count,
                self._replaced_count,
                self._discarded_count,
                self._error_count,
            )

        age = None if captured_at is None else now - captured_at
        stale = age is not None and age > config.GEMINI_RESULT_MAX_AGE_S

        # What the HUD shows about the worker itself.
        if busy:
            state = STATE_THINKING
        elif discard_at is not None and (now - discard_at) < 5.0:
            state = STATE_DISCARDED
        else:
            state = STATE_IDLE

        return {
            "description": None if (stale or not text) else text,
            "generation": generation,
            "distance_cm": distance_cm,
            "status": status,
            "age_s": age,
            "stale": stale,
            "state": state,
            "error": error,
            "busy": busy,
            "discard_reason": discard_reason,
            "encode_ms": encode_ms,
            "api_ms": api_ms,
            "request_count": counts[0],
            "reply_count": counts[1],
            "replaced_count": counts[2],
            "discarded_count": counts[3],
            "error_count": counts[4],
        }

    # ---------------------------------------------------------- main loop
    def run(self):
        # Warm DNS, TLS and the connection pool on the worker thread, so the
        # handshake is not billed to the first real obstacle and startup is
        # not delayed waiting for it.
        self.prewarm_note = self._prewarm()
        if self.prewarm_note:
            print("Gemini: {}".format(self.prewarm_note), flush=True)

        while not self._stop_event.is_set():
            try:
                request = self._queue.get(timeout=self.POLL_S)
            except queue.Empty:
                continue

            if self._stop_event.is_set():
                break

            # A blanket guard: this thread must survive absolutely anything,
            # because the alternative is Gemini failing silently forever.
            try:
                self._process_request(request)
            except Exception as exc:
                self._record_error("{}: {}".format(type(exc).__name__, exc))
            finally:
                with self._lock:
                    self._busy = False

    def _prewarm(self):
        """One tiny throwaway call so the first real request is not cold."""
        if not config.GEMINI_PREWARM or self._client is None:
            return None
        started = time.monotonic()
        try:
            self._client.models.generate_content(
                model=config.GEMINI_MODEL,
                contents=["ok"],
                config=self._gen_config,
            )
        except Exception as exc:
            # Never fatal - this is an optimisation, not a dependency. It is
            # also the canary: it sends the same generation config as a real
            # request with no image at all, so a failure here points
            # squarely at the request configuration.
            return "prewarm skipped - {}".format(describe_api_error(exc))
        return "connection warm ({:.0f} ms)".format(
            (time.monotonic() - started) * 1000)

    def _process_request(self, request):
        """Encode, ask Gemini, and publish - or record why we could not.

        DO NOT rename this back to `_handle`. This class subclasses
        threading.Thread, and CPython 3.13's Thread.start() assigns an
        instance attribute called `_handle` holding a _thread._ThreadHandle.
        An instance attribute shadows a class method, so a method named
        `_handle` here silently becomes unreachable once the thread starts,
        and calling it raises:

            TypeError: '_thread._ThreadHandle' object is not callable

        Python 3.12 has no such attribute and 3.14 renamed it to
        `_os_thread_handle`, so the bug appears only on 3.13 - which is what
        Raspberry Pi OS ships. test_alerts.py has a guard that fails if any
        of our thread classes reintroduce a name Thread uses internally.
        """
        try:
            self._analyse_request(request)
        finally:
            # Cleared here rather than in run(), so the THINKING state is
            # correct however this method is reached - including from a
            # test that calls it directly.
            with self._lock:
                self._busy = False

    def _analyse_request(self, request):
        """Encode, call Gemini, and publish if the reply is still relevant."""
        with self._lock:
            self._busy = True

        # Bail out before spending anything if this request is already
        # obsolete - the main loop may have superseded it while it queued.
        early = self._relevance_problem(request, age_s=0.0)
        if early:
            self._record_discarded("not sent: " + early)
            return

        encode_started = time.monotonic()
        try:
            jpeg_bytes = self._encode_jpeg(request.frame)
        except Exception as exc:
            self._record_error("JPEG encode failed: {}: {}".format(
                type(exc).__name__, exc))
            return
        encode_ms = (time.monotonic() - encode_started) * 1000.0

        api_started = time.monotonic()
        try:
            raw = self._call_gemini(jpeg_bytes, request.distance_cm)
        except Exception as exc:
            self._record_error(describe_api_error(exc))
            return
        api_ms = (time.monotonic() - api_started) * 1000.0

        with self._lock:
            self._last_encode_ms = encode_ms
            self._last_api_ms = api_ms

        if (api_ms / 1000.0) > config.GEMINI_DEADLINE_S:
            # A hung socket that eventually returned.
            self._record_discarded(
                "abandoned after {:.1f}s (deadline {:.0f}s)".format(
                    api_ms / 1000.0, config.GEMINI_DEADLINE_S
                )
            )
            return

        text = self._tidy(raw)
        if not text:
            self._record_error("Gemini returned an empty description")
            return

        # The real gate: is this still true of the world right now?
        age_s = time.monotonic() - request.captured_at
        problem = self._relevance_problem(request, age_s)
        if problem:
            self._record_discarded(
                "{}  [{:.0f}cm at capture, api {:.0f}ms, age {:.1f}s]".format(
                    problem, request.distance_cm or -1, api_ms, age_s)
            )
            return

        with self._lock:
            self._text = text
            self._captured_at = request.captured_at
            self._distance_cm = request.distance_cm
            self._status = request.status
            self._last_age_s = age_s
            self._generation += 1
            self._error = None
            self._last_discard_reason = None
            self._last_discard_at = None
            self._reply_count += 1

        print(
            "AI: {}   [{}cm {} via {}  enc {:.0f}ms  api {:.0f}ms  age {:.1f}s]"
            .format(
                text,
                "{:.0f}".format(request.distance_cm)
                if request.distance_cm is not None else "?",
                request.status or "?",
                request.reason or "?",
                encode_ms, api_ms, age_s,
            ),
            flush=True,
        )

    def _relevance_problem(self, request, age_s):
        """Why this reply should be thrown away, or None to accept it.

        This is the heart of the staleness handling, and deliberately NOT a
        stopwatch. A six-second-old "Chair ahead, move right" is still
        correct if the chair is still 45 cm ahead; it is only wrong if the
        world moved on. So we compare the request against live sensor state
        and only fall back on age as a last-resort backstop.
        """
        with self._lock:
            latest = self._latest_request_id
            current_distance = self._current_distance_cm
            current_status = self._current_status

        # 1. Superseded. A newer request exists, so this answer describes a
        #    frame the user has already moved past.
        if request.request_id != latest:
            return "superseded by request #{}".format(latest)

        # 2. The obstacle is gone.
        if current_status in IRRELEVANT_STATUSES:
            return "obstacle gone (now {})".format(current_status or "no reading")

        # 3. The scene materially changed - we are a long way from where the
        #    image was taken, so left/right advice may no longer hold.
        if current_distance is not None and request.distance_cm is not None:
            moved = abs(current_distance - request.distance_cm)
            if moved > config.GEMINI_RELEVANCE_DISTANCE_CM:
                return "scene changed ({:.0f}cm -> {:.0f}cm)".format(
                    request.distance_cm, current_distance)

        # 4. Backstop for a socket that hung and then returned.
        if age_s > config.GEMINI_ACCEPT_MAX_AGE_S:
            return "too old on arrival ({:.1f}s > {:.0f}s)".format(
                age_s, config.GEMINI_ACCEPT_MAX_AGE_S)

        return None

    # ------------------------------------------------------------- pieces
    @staticmethod
    def _encode_jpeg(frame):
        """Turn a BGR numpy frame into JPEG bytes. Runs on the WORKER thread.

        Downscaled first when GEMINI_SEND_RESOLUTION is set. Token cost is
        flat - anything up to 768x768 is one 258-token tile, so 512x384 and
        640x480 cost the model exactly the same - but the BYTES ON THE WIRE
        are not flat, and on a Pi 3's Wi-Fi the upload is a real slice of
        the round trip.
        """
        import cv2

        target = config.GEMINI_SEND_RESOLUTION
        if target:
            height, width = frame.shape[:2]
            target_w, target_h = int(target[0]), int(target[1])
            if width > target_w or height > target_h:
                frame = cv2.resize(
                    frame, (target_w, target_h), interpolation=cv2.INTER_AREA)

        ok, buffer = cv2.imencode(
            ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), config.GEMINI_JPEG_QUALITY]
        )
        if not ok:
            raise VisionError("cv2.imencode failed to produce a JPEG")
        return buffer.tobytes()

    def _call_gemini(self, jpeg_bytes, distance_cm):
        """THE ONLY PLACE THAT TALKS TO THE GEMINI API.

        Kept deliberately tiny and isolated: if Google changes the SDK
        surface, this function is the single thing that needs editing.
        Everything else in this file is transport-agnostic.
        """
        from google.genai import types

        prompt = config.GEMINI_PROMPT.format(
            distance_cm=int(distance_cm) if distance_cm is not None else "unknown"
        )

        contents = [
            types.Part.from_bytes(data=jpeg_bytes, mime_type="image/jpeg"),
            prompt,
        ]

        try:
            response = self._client.models.generate_content(
                model=config.GEMINI_MODEL,
                contents=contents,
                config=self._gen_config,
            )
        except Exception as exc:
            # A rejected REQUEST CONFIG is fatal for every future call too,
            # so if the server refuses our generation options we drop the
            # thinking option once, say so plainly, and carry on without it.
            # This is not swallowing the error - the full server message is
            # printed first, including whichever field it objected to.
            if not self._retry_without_thinking(exc):
                raise
            response = self._client.models.generate_content(
                model=config.GEMINI_MODEL,
                contents=contents,
                config=self._gen_config,
            )

        return getattr(response, "text", None)

    def _retry_without_thinking(self, exc):
        """True if we just disabled the thinking option in response to `exc`.

        Only ever fires once, and only for an invalid-argument rejection
        while a thinking option was actually attached.
        """
        if self._thinking_dropped:
            return False
        if getattr(exc, "code", None) != 400 and "400" not in str(exc):
            return False
        if "thinking" not in str(self._gen_config_note).lower():
            return False

        print(
            "GEMINI ERROR: request config rejected: {}".format(
                describe_api_error(exc)),
            flush=True,
        )

        self._thinking_dropped = True
        self._gen_config, note = self._make_generate_config(
            include_thinking=False)
        self._gen_config_note = note
        print(
            "GEMINI: retrying without the thinking option [{}]. "
            "Set GEMINI_THINKING_LEVEL = None in config.py to stop asking."
            .format(note),
            flush=True,
        )
        return True

    @staticmethod
    def _tidy(text):
        """Collapse a model reply into one short, clean line."""
        if not text:
            return ""
        first_line = str(text).strip().splitlines()[0] if str(text).strip() else ""
        cleaned = " ".join(first_line.split()).strip().strip('"').strip("'")
        limit = config.GEMINI_MAX_DESCRIPTION_CHARS
        if len(cleaned) > limit:
            cleaned = cleaned[: limit - 3].rstrip() + "..."
        return cleaned

    # ------------------------------------------------------------- errors
    def _record_error(self, message):
        """Store a failure for the HUD, and log it without spamming."""
        with self._lock:
            self._error = message
            self._error_count += 1
            repeat = (
                self._last_logged_error != message
                or self._last_logged_error_at is None
                or (time.monotonic() - self._last_logged_error_at)
                >= config.GEMINI_ERROR_REPEAT_S
            )
            if repeat:
                self._last_logged_error = message
                self._last_logged_error_at = time.monotonic()

        if repeat:
            print("GEMINI ERROR: {}".format(message), flush=True)

    def _record_discarded(self, message):
        """A reply we deliberately threw away as no longer relevant.

        Deliberately not recorded as an error: rejecting obsolete guidance
        is the system working correctly, not failing.
        """
        with self._lock:
            self._discarded_count += 1
            self._last_discard_reason = message
            self._last_discard_at = time.monotonic()
        print("GEMINI discarded: {}".format(message), flush=True)

    # ------------------------------------------------------------- shutdown
    def stop(self, timeout=2.0):
        """Ask the worker to finish.

        A request already inside the HTTP call cannot be interrupted, so we
        wait only briefly and then move on. The thread is a daemon, so a
        stuck network call can never stop the program from exiting.
        """
        self._stop_event.set()
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass
        if self.is_alive():
            self.join(timeout=timeout)
