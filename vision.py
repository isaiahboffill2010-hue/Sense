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

* NO STALE ADVICE. Every request is timestamped at capture. A description
  older than GEMINI_RESULT_MAX_AGE_S is discarded rather than shown, so a
  late reply can never describe a scene the user has already walked past.

* NO STACKING. The request queue holds exactly one item. If a request is
  already waiting or in flight, a new one is dropped rather than queued.

Phase 2 is display only - the description goes to the terminal and the HUD.
Speech comes later.
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


# One pending analysis: the frame copy, the distance that triggered it, and
# the moment the image was captured (used for every staleness decision).
AiRequest = collections.namedtuple(
    "AiRequest", ["frame", "distance_cm", "captured_at"]
)


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
        self.description = ""      # how the client is configured, for the UI

        # Published state, all guarded by _lock.
        self._text = None
        self._captured_at = None
        self._distance_cm = None
        self._error = None
        self._busy = False
        self._request_count = 0
        self._reply_count = 0
        self._dropped_count = 0
        self._discarded_count = 0     # replies that arrived already stale
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
        return self

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
    def request(self, frame, distance_cm):
        """Queue one analysis of `frame`. Returns True if it was accepted.

        Called from the MAIN thread, and deliberately cheap: a bounded
        put_nowait plus one numpy copy. The copy is essential because
        ui.draw_hud() mutates the frame in place immediately afterwards -
        without it Gemini would receive an image with the HUD burned in, and
        the worker would be reading an array the main thread is still
        drawing on.

        If an analysis is already queued or in flight, this DROPS the new
        request rather than letting work stack up.
        """
        if frame is None:
            return False

        # Cheap pre-check so we do not pay for a copy we are about to throw
        # away. put_nowait below is the actual guarantee.
        if self._queue.full():
            with self._lock:
                self._dropped_count += 1
            return False

        try:
            item = AiRequest(
                frame=frame.copy(),
                distance_cm=distance_cm,
                captured_at=time.monotonic(),
            )
            self._queue.put_nowait(item)
        except queue.Full:
            with self._lock:
                self._dropped_count += 1
            return False
        except Exception as exc:
            self._record_error("could not queue frame: {}".format(exc))
            return False

        with self._lock:
            self._request_count += 1
        return True

    # ------------------------------------------------------------ results
    def snapshot(self):
        """Thread-safe view of the latest result. Never raises.

        `description` is None unless there is a CURRENT description. Age is
        measured from when the image was captured, so a reply that took too
        long, or one that has simply been on screen too long, disappears on
        its own without anything else having to remember to clear it.
        """
        with self._lock:
            text = self._text
            captured_at = self._captured_at
            distance_cm = self._distance_cm
            error = self._error
            busy = self._busy
            counts = (
                self._request_count,
                self._reply_count,
                self._dropped_count,
                self._discarded_count,
                self._error_count,
            )

        age = None if captured_at is None else time.monotonic() - captured_at
        stale = age is not None and age > config.GEMINI_RESULT_MAX_AGE_S

        return {
            "description": None if (stale or not text) else text,
            "distance_cm": distance_cm,
            "age_s": age,
            "stale": stale,
            "error": error,
            "busy": busy,
            "request_count": counts[0],
            "reply_count": counts[1],
            "dropped_count": counts[2],
            "discarded_count": counts[3],
            "error_count": counts[4],
        }

    # ---------------------------------------------------------- main loop
    def run(self):
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
        with self._lock:
            self._busy = True

        started = time.monotonic()
        try:
            jpeg_bytes = self._encode_jpeg(request.frame)
            text = self._call_gemini(jpeg_bytes, request.distance_cm)
        except Exception as exc:
            self._record_error("{}: {}".format(type(exc).__name__, exc))
            return

        elapsed = time.monotonic() - started
        if elapsed > config.GEMINI_DEADLINE_S:
            # A hung socket that eventually returned. Treat the answer as
            # worthless rather than describing a scene from 10+ seconds ago.
            self._record_discarded(
                "reply abandoned after {:.1f}s (deadline {:.0f}s)".format(
                    elapsed, config.GEMINI_DEADLINE_S
                )
            )
            return

        text = self._tidy(text)
        if not text:
            self._record_error("Gemini returned an empty description")
            return

        age = time.monotonic() - request.captured_at
        if age > config.GEMINI_RESULT_MAX_AGE_S:
            # Born stale: the round trip outlived the usefulness of the
            # image. Never show this.
            self._record_discarded(
                "reply was {:.1f}s old on arrival (max {:.0f}s)".format(
                    age, config.GEMINI_RESULT_MAX_AGE_S
                )
            )
            return

        with self._lock:
            self._text = text
            self._captured_at = request.captured_at
            self._distance_cm = request.distance_cm
            self._error = None
            self._reply_count += 1

        print("AI: {}".format(text), flush=True)

    # ------------------------------------------------------------- pieces
    @staticmethod
    def _encode_jpeg(frame):
        """Turn a BGR numpy frame into JPEG bytes. Runs on the WORKER thread."""
        import cv2

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

        response = self._client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=[
                types.Part.from_bytes(data=jpeg_bytes, mime_type="image/jpeg"),
                prompt,
            ],
        )
        return getattr(response, "text", None)

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
        """A reply we deliberately threw away for being too old."""
        with self._lock:
            self._discarded_count += 1
            self._error = message
        print("GEMINI: {}".format(message), flush=True)

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
