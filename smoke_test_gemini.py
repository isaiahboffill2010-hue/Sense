#!/usr/bin/env python3
"""
smoke_test_gemini.py
====================

Run this ON THE RASPBERRY PI, once, BEFORE launching the full application:

    python3 smoke_test_gemini.py

It answers one question: can this Pi actually talk to Gemini?

It deliberately calls the SAME GeminiWorker._call_gemini() that the real
application uses, rather than a parallel copy. So if this passes, the app's
Gemini path works; if it fails, the fix is in that one function and nothing
else needs to change.

Checks, in order:

    1. .env.local loads and GEMINI_API_KEY is set   (the value is never shown)
    2. google-genai is installed and importable
    3. a client can be built
    4. an image is obtained - from your real camera if it is free,
       otherwise a generated test pattern
    5. one real request to Gemini, timed
    6. the reply is tidied exactly as the app would tidy it

This makes ONE API call. At current gemini-3.5-flash-lite prices that is
roughly $0.0001.
"""

import sys
import time

import config
import vision


def step(number, title):
    print("")
    print("[{}] {}".format(number, title))


def fail(message, hint=None):
    print("    FAILED: {}".format(message))
    if hint:
        for line in hint.strip().splitlines():
            print("    " + line.strip())
    print("")
    print("Smoke test FAILED - do not run main.py until this passes.")
    sys.exit(1)


print("=" * 62)
print("  Gemini smoke test")
print("=" * 62)

# -------------------------------------------------------------------------
step(1, "API key")
# -------------------------------------------------------------------------
loaded = vision.load_env_file()
if loaded:
    print("    loaded from {}: {}".format(config.ENV_FILE_PATH.name,
                                          ", ".join(loaded)))
else:
    print("    nothing loaded from .env.local (already exported, or no file)")

if not vision.api_key_is_present():
    fail(
        "{} is not set".format(config.GEMINI_API_KEY_ENV),
        """
        Create .env.local in the project folder containing:
            GEMINI_API_KEY=your-key-here
        That file is already gitignored.
        """,
    )
print("    {} is set (value not shown)".format(config.GEMINI_API_KEY_ENV))

# -------------------------------------------------------------------------
step(2, "google-genai package")
# -------------------------------------------------------------------------
try:
    import google.genai as genai_module
except ImportError as exc:
    fail(
        "google-genai is not importable ({})".format(exc),
        """
        Install it with:
            pip3 install --user --break-system-packages google-genai
        """,
    )
print("    imported, version {}".format(
    getattr(genai_module, "__version__", "unknown")))

# -------------------------------------------------------------------------
step(3, "Gemini client")
# -------------------------------------------------------------------------
try:
    worker = vision.GeminiWorker().open()
except Exception as exc:
    fail("{}: {}".format(type(exc).__name__, exc))
print("    {}".format(worker.description))

# -------------------------------------------------------------------------
step(4, "test image")
# -------------------------------------------------------------------------
frame = None
try:
    from hardware.camera import Camera

    camera = Camera().open()
    try:
        frame = camera.read()
        print("    captured a real frame from the camera: {}".format(
            "x".join(str(n) for n in frame.shape[:2][::-1])))
    finally:
        camera.close()
except Exception as exc:
    print("    camera unavailable ({}: {})".format(type(exc).__name__, exc))
    print("    falling back to a generated test pattern")

if frame is None:
    try:
        import numpy
        import cv2

        frame = numpy.zeros((480, 640, 3), dtype=numpy.uint8)
        frame[:] = (60, 60, 60)
        cv2.rectangle(frame, (220, 140), (420, 400), (200, 200, 200), -1)
        cv2.putText(frame, "TEST", (250, 280), cv2.FONT_HERSHEY_SIMPLEX,
                    2.0, (0, 0, 0), 4)
    except Exception as exc:
        fail("could not build a test image: {}: {}".format(
            type(exc).__name__, exc))

try:
    jpeg_bytes = vision.GeminiWorker._encode_jpeg(frame)
except Exception as exc:
    fail("JPEG encoding failed: {}: {}".format(type(exc).__name__, exc))
print("    encoded to JPEG: {:,} bytes".format(len(jpeg_bytes)))

# -------------------------------------------------------------------------
step(5, "one real request to Gemini")
# -------------------------------------------------------------------------
print("    model: {}".format(config.GEMINI_MODEL))
print("    sending...", flush=True)

started = time.monotonic()
try:
    raw = worker._call_gemini(jpeg_bytes, 80.0)
except Exception as exc:
    elapsed = time.monotonic() - started
    fail(
        "{}: {}  (after {:.1f}s)".format(type(exc).__name__, exc, elapsed),
        """
        Common causes:
          - no internet          check with:  ping -c1 generativelanguage.googleapis.com
          - bad or expired key   regenerate at https://aistudio.google.com/apikey
          - model name wrong     check GEMINI_MODEL in config.py
          - SDK surface changed  fix GeminiWorker._call_gemini() in vision.py
        """,
    )
elapsed = time.monotonic() - started
print("    round trip: {:.2f}s".format(elapsed))

# -------------------------------------------------------------------------
step(6, "reply")
# -------------------------------------------------------------------------
print("    raw      : {!r}".format(raw))
tidied = vision.GeminiWorker._tidy(raw)
print("    as shown : AI: {}".format(tidied))

if not tidied:
    fail("Gemini returned an empty description")

print("")
print("=" * 62)
if elapsed > config.GEMINI_RESULT_MAX_AGE_S:
    print("  PASSED - but note the round trip took {:.1f}s, which is longer".format(
        elapsed))
    print("  than GEMINI_RESULT_MAX_AGE_S ({:.0f}s), so replies will be".format(
        config.GEMINI_RESULT_MAX_AGE_S))
    print("  discarded as stale. Raise that value in config.py if your")
    print("  connection is consistently this slow.")
else:
    print("  PASSED - Gemini is reachable and the reply arrived in time.")
print("=" * 62)
print("")
print("You can now run:  python3 main.py")
