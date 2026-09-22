#!/usr/bin/env python3
"""
main.py
=======

Phase 1 hardware test for the wearable navigation headband.

Run it ON THE RASPBERRY PI:

    python3 main.py

It brings up three subsystems at once and tells you honestly whether each
one works:

    Camera      live CSI camera preview in an OpenCV window (Picamera2)
    Ultrasonic  SunFounder module on the Robot HAT, read on its own thread
    Audio       warning beeps through the 3.5mm jack into your headphones
    Gemini      short navigation descriptions of what the camera can see

Threading model - why the preview stays smooth
----------------------------------------------
    main thread        camera capture + drawing + cv2.imshow + key handling
    "ultrasonic"       fires pings and waits for echoes
    "beeper"           sleeps between beeps and plays them
    "gemini"           JPEG encoding and the network call to the API

The main thread never calls time.sleep() for the beep rhythm, never waits
for an echo, and never waits for Gemini, so none of them can stall the
video. Gemini is strictly optional: if it fails in any way the device keeps
behaving exactly like Phase 1.

Quit with Q (or Esc, or closing the window, or Ctrl+C). Everything is shut
down and cleaned up in a finally block either way.
"""

import argparse
import collections
import sys
import time

import alerts
import config
import diagnostics
# Tone name constant only; hardware.audio imports no audio library at
# module level, so this is still safe to import on a non-Pi machine.
from hardware.audio import TONE_WARNING

# Subsystem status words shown in the overlay and the startup report.
STATUS_OK = "OK"
STATUS_FAIL = "FAIL"
STATUS_SKIPPED = "SKIPPED"
STATUS_NOT_CONFIGURED = "NOT CONFIGURED"

GREEN = (60, 200, 60)
RED = (40, 40, 255)
YELLOW = (40, 215, 255)
GREY = (170, 170, 170)
WHITE = (255, 255, 255)
CYAN = (255, 255, 120)      # the Gemini description line

# Colours for the AI activity line.
AI_STATE_COLORS = {
    "IDLE": GREY,
    "THINKING": (255, 200, 80),     # amber: a request is in flight
    "DISCARDED": (150, 150, 255),   # pink: a reply was rejected as obsolete
}

STATE_COLORS = {
    STATUS_OK: GREEN,
    STATUS_FAIL: RED,
    STATUS_SKIPPED: GREY,
    STATUS_NOT_CONFIGURED: YELLOW,
}


# ==========================================================================
# Command line
# ==========================================================================
def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Sense: camera + Robot HAT ultrasonic + headphone beeps + Gemini vision."
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="no preview window; print the status to the terminal instead "
             "(useful over plain SSH)",
    )
    parser.add_argument(
        "--skip-camera", action="store_true", help="do not start the camera"
    )
    parser.add_argument(
        "--skip-ultrasonic", action="store_true",
        help="do not start the ultrasonic sensor"
    )
    parser.add_argument(
        "--skip-audio", action="store_true", help="do not start audio / beeps"
    )
    parser.add_argument(
        "--skip-gemini", action="store_true",
        help="do not start Gemini vision (the rest still works normally)"
    )
    parser.add_argument(
        "--skip-speech", action="store_true",
        help="do not speak AI guidance; beeps and the HUD are unaffected"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="run even if this does not look like a Raspberry Pi (expect "
             "real import errors; nothing is simulated)",
    )
    return parser.parse_args(argv)


# ==========================================================================
# Small helpers
# ==========================================================================
class FpsMeter:
    """Rolling frames-per-second measurement over the last N frames."""

    def __init__(self, window=30):
        self._times = collections.deque(maxlen=window)

    def tick(self):
        self._times.append(time.perf_counter())

    @property
    def value(self):
        if len(self._times) < 2:
            return None
        span = self._times[-1] - self._times[0]
        if span <= 0:
            return None
        return (len(self._times) - 1) / span


def format_distance(snapshot):
    """Human readable distance text. Never invents a number."""
    if snapshot is None:
        return "Distance: -- (sensor not running)"

    distance = snapshot["distance_cm"]
    if distance is None:
        return "Distance: -- (no reading)"

    if snapshot["out_of_range"]:
        if distance > config.SENSOR_MAX_DISTANCE_CM:
            return "Distance: >{:.0f} cm (out of range)".format(
                config.SENSOR_MAX_DISTANCE_CM
            )
        return "Distance: <{:.0f} cm (too close to measure)".format(
            config.SENSOR_MIN_DISTANCE_CM
        )

    return "Distance: {:.0f} cm".format(distance)


def apply_alert_policy(policy, snapshot, beeper, frame=None, vision=None,
                       speech=None):
    """Feed the latest reading to the alert policy and act on its decision.

    Returns the status word to display. This is the single place where the
    device decides whether to make a sound or ask Gemini, so the overlay,
    the audio and the AI can never disagree about what band we are in.

    Ordering here is the safety priority, top to bottom:

      1. the local beeps are driven FIRST, straight from the ultrasonic
         reading, and never wait on anything
      2. DANGER silences speech, cutting off any phrase mid-word
      3. only then is Gemini given a frame, via a call that cannot block

    IMPORTANT: the caller must invoke this BEFORE ui.draw_hud(), because
    draw_hud mutates the frame in place. Queueing afterwards would send
    Gemini an image with the HUD text burned into it.
    """
    distance = snapshot["distance_cm"] if snapshot is not None else None
    decision = policy.update(distance)

    # --- 1. immediate collision warning, always first ---------------------
    if beeper is not None:
        # Repeated beeps: only DANGER sets a non-None interval.
        beeper.set_interval(decision.repeat_interval)
        # One subtle tone, exactly on entering the WARNING band.
        if decision.play_warning_tone:
            beeper.play_once(TONE_WARNING)

    # --- 2. danger outranks speech ----------------------------------------
    if speech is not None and config.SPEECH_MUTE_IN_DANGER:
        speech.set_muted(decision.status == alerts.DANGER)

    # --- 3. supplemental scene understanding ------------------------------
    if vision is not None:
        # Keep the worker's idea of "now" fresh so a finished reply can be
        # judged against the current world rather than a stopwatch.
        vision.note_current_state(distance, decision.status)

        if decision.request_ai and frame is not None:
            vision.request(frame, distance, decision.status, decision.ai_reason)

    return decision.status


def speak_new_guidance(vision, speech, spoken_generation):
    """Speak an accepted description once. Returns the generation spoken.

    The worker bumps `generation` only when a result passes the relevance
    check, so comparing generations is what stops the same guidance being
    spoken again on every frame. SpeechController adds its own duplicate
    suppression on top, for the case where two separate requests happen to
    come back with identical wording.
    """
    if vision is None or speech is None:
        return spoken_generation

    ai = vision.snapshot()
    generation = ai["generation"]
    if generation == spoken_generation or not ai["description"]:
        return spoken_generation

    speech.say(ai["description"])
    return generation


def build_hud_lines(snapshot, status, states, audio_error, ai=None):
    """Build the (text, colour) list drawn in the corner of the preview."""
    lines = [
        (format_distance(snapshot), WHITE),
        ("Status: {}".format(status), alerts.color_for(status)),
    ]

    # The description only appears while it is still current - snapshot()
    # returns None for it once it is older than GEMINI_RESULT_MAX_AGE_S.
    if ai is not None and ai["description"]:
        lines.append(("AI: " + _shorten(ai["description"], 44), CYAN))

    for label in ("Camera", "Ultrasonic", "Audio", "Gemini", "Speech"):
        state, _detail = states[label]
        lines.append(
            ("{}: {}".format(label, state), STATE_COLORS.get(state, GREY))
        )

    # What the AI worker is doing right now, and how fast it was last time.
    if ai is not None:
        lines.append((_ai_activity_line(ai), AI_STATE_COLORS.get(
            ai["state"], GREY)))

    # Show live failures underneath, so a mid-run problem is visible on screen.
    if snapshot is not None and snapshot["error"]:
        lines.append(("ULTRASONIC ERROR: " + _shorten(snapshot["error"]), RED))
    if audio_error:
        lines.append(("AUDIO ERROR: " + _shorten(audio_error), RED))
    if ai is not None and ai["error"]:
        lines.append(("GEMINI: " + _shorten(ai["error"]), RED))

    return lines


def _ai_activity_line(ai):
    """One compact debug line: worker state, last latency, rejection counts."""
    parts = ["AI " + ai["state"]]

    if ai["api_ms"] is not None:
        parts.append("{:.1f}s".format(ai["api_ms"] / 1000.0))

    parts.append("req {} ok {} rej {}".format(
        ai["request_count"], ai["reply_count"],
        ai["replaced_count"] + ai["discarded_count"]))

    line = "  ".join(parts)
    if ai["state"] == "DISCARDED" and ai["discard_reason"]:
        line += " - " + _shorten(ai["discard_reason"], 30)
    return line


def _shorten(text, limit=48):
    single_line = " ".join(str(text).split())
    if len(single_line) <= limit:
        return single_line
    return single_line[: limit - 3] + "..."


# ==========================================================================
# Startup
# ==========================================================================
def print_banner():
    print("")
    print("=" * 62)
    print("  AI Navigation Headband - Phase 1 Hardware Test")
    print("=" * 62)


def start_camera(args, states):
    """Open the camera. Returns the Camera object, or None on failure."""
    if args.skip_camera:
        states["Camera"] = (STATUS_SKIPPED, "--skip-camera")
        print("Camera: SKIPPED (--skip-camera)")
        return None

    print("Camera: checking...", flush=True)
    from hardware.camera import Camera, CameraError

    try:
        camera = Camera().open()
    except CameraError as exc:
        states["Camera"] = (STATUS_FAIL, str(exc))
        print("CAMERA ERROR: {}".format(exc))
        return None

    states["Camera"] = (STATUS_OK, camera.info)
    print("Camera: OK - {}".format(camera.info))
    return camera


def start_audio(args, states):
    """Open audio. Returns the BeepPlayer, or None on failure."""
    if args.skip_audio:
        states["Audio"] = (STATUS_SKIPPED, "--skip-audio")
        print("Audio: SKIPPED (--skip-audio)")
        return None

    print("Audio: checking...", flush=True)
    from hardware.audio import AudioError, BeepPlayer, play_test_beep

    try:
        player = BeepPlayer().open()
    except AudioError as exc:
        states["Audio"] = (STATUS_FAIL, str(exc))
        print("AUDIO ERROR: {}".format(exc))
        return None

    states["Audio"] = (STATUS_OK, player.description)
    print("Audio: OK - {}".format(player.description))
    print("       beep file: {}".format(player.wav_path))

    if config.PLAY_STARTUP_TEST_BEEP:
        try:
            play_test_beep(player)
            print("       a test beep was sent to the headphones - did you hear it?")
        except Exception as exc:
            print("AUDIO ERROR: test beep failed: {}".format(exc))

    return player


def start_vision(args, states):
    """Open the Gemini worker. Returns it, or None if unavailable.

    A failure here is never fatal: the device simply runs as Phase 1 did.
    """
    if args.skip_gemini or not config.GEMINI_ENABLED:
        reason = "--skip-gemini" if args.skip_gemini else "GEMINI_ENABLED=False"
        states["Gemini"] = (STATUS_SKIPPED, reason)
        print("Gemini vision: SKIPPED ({})".format(reason))
        return None

    print("Gemini vision: checking...", flush=True)
    import vision

    # Pull .env.local into the environment. Only key NAMES are ever shown.
    loaded = vision.load_env_file()
    if loaded:
        print("       loaded from .env.local: {}".format(", ".join(loaded)))

    if not vision.api_key_is_present():
        message = (
            "{} is not set. Add it to .env.local (already gitignored) or "
            "export it.".format(config.GEMINI_API_KEY_ENV)
        )
        states["Gemini"] = (STATUS_NOT_CONFIGURED, message)
        print("Gemini vision: NOT CONFIGURED")
        print("  " + message)
        return None

    try:
        worker = vision.GeminiWorker().open()
    except vision.VisionError as exc:
        states["Gemini"] = (STATUS_FAIL, str(exc))
        print("GEMINI ERROR: {}".format(exc))
        return None
    except Exception as exc:
        states["Gemini"] = (STATUS_FAIL, "{}: {}".format(type(exc).__name__, exc))
        print("GEMINI ERROR: {}: {}".format(type(exc).__name__, exc))
        return None

    worker.start()
    states["Gemini"] = (STATUS_OK, worker.description)
    print("Gemini vision: OK - {}".format(worker.description))
    return worker


def start_speech(args, states):
    """Open offline text-to-speech. Returns (player, controller) or (None, None).

    Never fatal: if there is no TTS engine installed, the beeps, the HUD and
    everything else carry on exactly as before.
    """
    if args.skip_speech or not config.SPEECH_ENABLED:
        reason = "--skip-speech" if args.skip_speech else "SPEECH_ENABLED=False"
        states["Speech"] = (STATUS_SKIPPED, reason)
        print("Speech: SKIPPED ({})".format(reason))
        return None, None

    print("Speech: checking...", flush=True)
    from hardware.audio import AudioError, SpeechController, SpeechPlayer

    try:
        player = SpeechPlayer().open()
    except AudioError as exc:
        states["Speech"] = (STATUS_FAIL, str(exc))
        print("SPEECH ERROR: {}".format(exc))
        return None, None

    controller = SpeechController(player)
    controller.start()
    states["Speech"] = (STATUS_OK, player.description)
    print("Speech: OK - {}".format(player.description))

    # Say one phrase so you can confirm TTS really reaches the headphones.
    controller.say("Sense ready.")
    print("       a spoken test phrase was sent - did you hear it?")
    return player, controller


def start_ultrasonic(args, states):
    """Open the Robot HAT ultrasonic module and start its monitor thread.

    Returns (sensor, monitor); either may be None.
    """
    from hardware.ultrasonic import (
        UltrasonicError,
        UltrasonicMonitor,
        UltrasonicSensor,
    )

    if args.skip_ultrasonic:
        states["Ultrasonic"] = (STATUS_SKIPPED, "--skip-ultrasonic")
        print("Ultrasonic sensor: SKIPPED (--skip-ultrasonic)")
        return None, None

    if not UltrasonicSensor.pins_are_configured():
        message = (
            "TRIG_PIN / ECHO_PIN are not set in config.py. Set them to the "
            'Robot HAT digital port names you used, e.g. "D2" and "D3".'
        )
        states["Ultrasonic"] = (STATUS_NOT_CONFIGURED, message)
        print("Ultrasonic sensor: NOT CONFIGURED")
        print("  " + message)
        return None, None

    sensor = UltrasonicSensor(config.TRIG_PIN, config.ECHO_PIN)
    print("Ultrasonic sensor: checking... ({})".format(sensor.description),
          flush=True)
    try:
        sensor.open()
    except UltrasonicError as exc:
        states["Ultrasonic"] = (STATUS_FAIL, str(exc))
        print("ULTRASONIC ERROR: {}".format(exc))
        return None, None

    # Take one real reading so a wiring mistake shows up now, not later.
    try:
        first = sensor.measure()
    except UltrasonicError as exc:
        states["Ultrasonic"] = (STATUS_FAIL, str(exc))
        print("ULTRASONIC ERROR: {}".format(exc))
        print("  Starting the monitor anyway in case it recovers - watch the")
        print("  status line while you move your hand in front of the sensor.")
    else:
        states["Ultrasonic"] = (STATUS_OK, sensor.description)
        print("Ultrasonic sensor: OK - {} - first reading {:.1f} cm".format(
            sensor.description, first))

    monitor = UltrasonicMonitor(sensor)
    monitor.start()
    return sensor, monitor


# ==========================================================================
# Loops
# ==========================================================================
def update_states_from_monitor(states, monitor):
    """Keep the Ultrasonic status line honest while the program runs."""
    if monitor is None:
        return
    snapshot = monitor.snapshot()
    if not snapshot["healthy"]:
        states["Ultrasonic"] = (STATUS_FAIL, snapshot["error"] or "no readings")
    elif snapshot["reading_count"] > 0 and states["Ultrasonic"][0] != STATUS_OK:
        # Only claim OK once a real measurement has actually come back.
        states["Ultrasonic"] = (STATUS_OK, "recovered")


def run_preview_loop(camera, monitor, beeper, states, policy, vision=None,
                     speech=None):
    """Live OpenCV preview. Returns True if it ran, False to fall back."""
    import cv2

    import ui
    from hardware.camera import CameraError

    try:
        cv2.namedWindow(config.WINDOW_NAME, cv2.WINDOW_AUTOSIZE)
    except cv2.error as exc:
        print("")
        print("DISPLAY ERROR: could not open a preview window: {}".format(exc))
        print("  You are probably on a text-only SSH session. Either run this")
        print("  from the Pi's desktop (or VNC), or use:  python3 main.py --headless")
        print("  Falling back to headless mode now.")
        return False

    print("")
    print("Live preview running. Press Q in the window to quit.")
    print("")

    fps = FpsMeter()
    spoken_generation = 0
    while True:
        try:
            frame = camera.read()
        except CameraError as exc:
            print("CAMERA ERROR: {}".format(exc))
            break

        fps.tick()

        snapshot = monitor.snapshot() if monitor is not None else None

        # Must run BEFORE draw_hud: it may hand a copy of this still-clean
        # frame to the Gemini worker, and draw_hud mutates the frame.
        status = apply_alert_policy(
            policy, snapshot, beeper, frame, vision, speech)
        update_states_from_monitor(states, monitor)
        spoken_generation = speak_new_guidance(vision, speech, spoken_generation)

        audio_error = beeper.error if beeper is not None else None
        ai = vision.snapshot() if vision is not None else None
        ui.draw_hud(
            frame,
            build_hud_lines(snapshot, status, states, audio_error, ai),
            fps=fps.value,
        )

        cv2.imshow(config.WINDOW_NAME, frame)

        key = cv2.waitKey(1) & 0xFF
        if key in (ord("q"), ord("Q"), 27):     # Q or Esc
            print("Q pressed - shutting down...")
            break

        # Also quit if the user closes the window with the X button.
        try:
            if cv2.getWindowProperty(config.WINDOW_NAME, cv2.WND_PROP_VISIBLE) < 1:
                print("Window closed - shutting down...")
                break
        except cv2.error:
            break

    try:
        cv2.destroyAllWindows()
        cv2.waitKey(1)      # let the window manager actually close it
    except cv2.error:
        pass
    return True


def run_headless_loop(camera, monitor, beeper, states, policy, vision=None,
                      speech=None):
    """No window: print the same information to the terminal."""
    print("")
    print("Headless mode. Press Ctrl+C to quit.")
    print("")

    camera_error_reported = False
    fps = FpsMeter()
    next_print = 0.0
    spoken_generation = 0

    while True:
        frame = None
        if camera is not None:
            from hardware.camera import CameraError
            try:
                # Keep the frame: Gemini needs it, even with no window.
                frame = camera.read()
                fps.tick()
            except CameraError as exc:
                if not camera_error_reported:
                    print("CAMERA ERROR: {}".format(exc))
                    camera_error_reported = True
                    states["Camera"] = (STATUS_FAIL, str(exc))

        snapshot = monitor.snapshot() if monitor is not None else None
        status = apply_alert_policy(
            policy, snapshot, beeper, frame, vision, speech)
        update_states_from_monitor(states, monitor)
        spoken_generation = speak_new_guidance(vision, speech, spoken_generation)

        now = time.monotonic()
        if now >= next_print:
            next_print = now + 0.5
            fps_text = "" if fps.value is None else "  fps {:.1f}".format(fps.value)
            print("{:<34} Status: {:<8} Cam {:<3} US {:<3} Aud {}{}".format(
                format_distance(snapshot),
                status,
                _short_state(states["Camera"][0]),
                _short_state(states["Ultrasonic"][0]),
                _short_state(states["Audio"][0]),
                fps_text,
            ))
            if vision is not None:
                ai = vision.snapshot()
                if ai["state"] != "IDLE" or ai["description"]:
                    print("   {}".format(_ai_activity_line(ai)))
            if snapshot is not None and snapshot["error"]:
                print("   ULTRASONIC ERROR: {}".format(snapshot["error"]))
            if beeper is not None and beeper.error:
                print("   AUDIO ERROR: {}".format(beeper.error))

        # Nothing to draw, so pace the loop politely instead of spinning.
        if camera is None:
            time.sleep(0.05)

    return True


def _short_state(state):
    return {"OK": "OK", "FAIL": "ERR", "SKIPPED": "--", "NOT CONFIGURED": "CFG"}.get(
        state, "?"
    )


# ==========================================================================
# Shutdown
# ==========================================================================
def shutdown(camera, sensor, monitor, player, beeper, vision=None,
             speech=None, speech_player=None):
    """Stop everything, in the safe order, and never raise while doing it."""
    print("")
    print("Cleaning up...")

    if beeper is not None:
        beeper.stop()
        print("  beeper thread stopped")

    if speech is not None:
        # Cuts off any phrase in progress rather than waiting it out.
        speech.stop()
        print("  speech thread stopped")

    if speech_player is not None:
        speech_player.close()
        print("  speech engine closed")

    if vision is not None:
        # Stopped early so it makes no further network calls. A request
        # already inside the HTTP call cannot be interrupted, so stop()
        # waits only briefly - the thread is a daemon, so a stuck socket
        # can never prevent the program from exiting.
        vision.stop()
        print("  gemini thread stopped")

    if monitor is not None:
        monitor.stop()
        print("  ultrasonic thread stopped")

    if sensor is not None:
        sensor.close()
        print("  ultrasonic sensor released")

    if player is not None:
        player.close()
        print("  audio closed")

    if camera is not None:
        camera.close()
        print("  camera stopped")

    try:
        import cv2
        cv2.destroyAllWindows()
        cv2.waitKey(1)
    except Exception:
        pass

    print("Done. Goodbye.")


# ==========================================================================
# Entry point
# ==========================================================================
def main(argv=None):
    args = parse_args(argv)

    print_banner()
    info = diagnostics.describe_platform()
    diagnostics.print_platform_report(info)

    if not info["is_linux"]:
        diagnostics.print_wrong_os_warning(info)
        if not args.force:
            return 2

    print("")
    print("-" * 62)

    states = {
        "Camera": (STATUS_SKIPPED, ""),
        "Ultrasonic": (STATUS_SKIPPED, ""),
        "Audio": (STATUS_SKIPPED, ""),
        "Gemini": (STATUS_SKIPPED, ""),
        "Speech": (STATUS_SKIPPED, ""),
    }

    camera = None
    sensor = None
    monitor = None
    player = None
    beeper = None
    vision = None
    speech = None
    speech_player = None
    exit_code = 0

    try:
        camera = start_camera(args, states)
        sensor, monitor = start_ultrasonic(args, states)
        player = start_audio(args, states)

        if player is not None:
            from hardware.audio import BeepController
            beeper = BeepController(player)
            beeper.start()

        # Gemini last: it is the only optional subsystem, and it needs the
        # camera to be useful.
        if camera is None and not args.skip_gemini:
            states["Gemini"] = (STATUS_SKIPPED, "no camera")
            print("Gemini vision: SKIPPED (no camera to send images from)")
        else:
            vision = start_vision(args, states)

        speech_player, speech = start_speech(args, states)

        print("-" * 62)
        print("Camera     : {}".format(states["Camera"][0]))
        print("Ultrasonic : {}".format(states["Ultrasonic"][0]))
        print("Audio      : {}".format(states["Audio"][0]))
        print("Gemini     : {}".format(states["Gemini"][0]))
        print("Speech     : {}".format(states["Speech"][0]))
        print("-" * 62)

        if all(state == STATUS_FAIL for state, _ in states.values()):
            print("")
            print("Every subsystem failed. Fix the errors above and try again.")
            return 1

        if camera is None and monitor is None and player is None:
            print("")
            print("Nothing to test: camera, ultrasonic and audio are all")
            print("unavailable or skipped. Fix the items above and try again.")
            return 1

        # One policy shared by both loops: it owns all the alert state.
        policy = alerts.AlertPolicy()

        ran_preview = False
        if camera is not None and not args.headless:
            ran_preview = run_preview_loop(
                camera, monitor, beeper, states, policy, vision, speech
            )
        if not ran_preview:
            run_headless_loop(
                camera, monitor, beeper, states, policy, vision, speech)

    except KeyboardInterrupt:
        print("")
        print("Ctrl+C received - shutting down...")
    finally:
        shutdown(camera, sensor, monitor, player, beeper, vision,
                 speech, speech_player)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
