"""
hardware/camera.py
==================

Raspberry Pi CSI camera, through Picamera2 / libcamera.

This is the CURRENT Raspberry Pi camera stack. The old `picamera` library
(and the "legacy camera" option in raspi-config) is obsolete and is not used
here.

Install with apt, not pip:

    sudo apt install -y python3-picamera2

Honesty policy
--------------
There is no synthetic frame source, no test pattern, no USB fallback. If the
camera will not start you get a CameraError carrying the real exception text.
"""

import threading
import time

import config


class CameraError(RuntimeError):
    """Raised when the camera cannot be initialised or cannot deliver frames."""


class Camera:
    """Thin wrapper around Picamera2 that hands OpenCV-ready frames back."""

    def __init__(self, resolution=None, pixel_format=None):
        self.resolution = tuple(resolution or config.CAMERA_RESOLUTION)
        self.pixel_format = pixel_format or config.CAMERA_FORMAT
        self._picam2 = None
        self._started = False
        self.info = ""          # human readable description of the sensor

    # ---------------------------------------------------------------- setup
    def open(self):
        """Start the camera. Raises CameraError with the real reason on failure."""
        try:
            from picamera2 import Picamera2
        except ImportError as exc:
            raise CameraError(
                "Picamera2 is not installed ({}).\n"
                "  Install it with:  sudo apt install -y python3-picamera2\n"
                "  Note: install via apt, NOT pip. If you are using a virtual\n"
                "  environment, create it with --system-site-packages so it can\n"
                "  see the apt-installed Picamera2.".format(exc)
            ) from exc

        # Ask libcamera what it can actually see before we try to open it.
        # An empty list here is the clearest possible "no camera" diagnosis.
        try:
            cameras = Picamera2.global_camera_info()
        except Exception as exc:
            raise CameraError(
                "libcamera could not enumerate cameras: {}: {}\n"
                "  Try:  rpicam-hello --list-cameras".format(type(exc).__name__, exc)
            ) from exc

        if not cameras:
            raise CameraError(
                "No camera detected by libcamera.\n"
                "  - Is the CSI ribbon cable seated at BOTH ends, contacts\n"
                "    facing the right way, with the Pi powered off while you\n"
                "    reseat it?\n"
                "  - Check /boot/firmware/config.txt contains: camera_auto_detect=1\n"
                "  - Verify from the shell with:  rpicam-hello --list-cameras"
            )

        try:
            self._picam2 = Picamera2()
            camera_config = self._picam2.create_preview_configuration(
                main={"size": self.resolution, "format": self.pixel_format}
            )
            self._picam2.configure(camera_config)
            self._picam2.start()
            self._started = True
        except Exception as exc:
            self.close()
            raise CameraError(
                "Camera could not be initialized: {}: {}".format(
                    type(exc).__name__, exc
                )
            ) from exc

        model = cameras[0].get("Model", "unknown sensor")
        self.info = "{} @ {}x{} ({})".format(
            model, self.resolution[0], self.resolution[1], self.pixel_format
        )

        # Give auto-exposure / auto-white-balance a moment. This blocks, but
        # only once during startup - never inside the live loop.
        time.sleep(config.CAMERA_WARMUP_S)

        # Prove it really delivers pixels before we report "Camera: OK".
        try:
            self.read()
        except CameraError:
            self.close()
            raise

        return self

    # --------------------------------------------------------------- frames
    def read(self):
        """Return the newest frame as a numpy array laid out for OpenCV (BGR)."""
        if not self._started or self._picam2 is None:
            raise CameraError("Camera is not open. Call open() first.")

        try:
            frame = self._picam2.capture_array()
        except Exception as exc:
            raise CameraError(
                "Failed to capture a frame: {}: {}".format(type(exc).__name__, exc)
            ) from exc

        if frame is None or getattr(frame, "size", 0) == 0:
            raise CameraError("Camera returned an empty frame.")

        # Picamera2's "RGB888" hands back bytes in B, G, R order, which is
        # already what OpenCV wants - so normally no conversion is needed.
        # CAMERA_SWAP_RED_BLUE in config.py is the escape hatch if your
        # preview shows blue faces and orange sky.
        if config.CAMERA_SWAP_RED_BLUE:
            frame = frame[:, :, ::-1].copy()

        return frame

    # -------------------------------------------------------------- cleanup
    def close(self):
        """Stop the camera and release it. Safe to call more than once."""
        if self._picam2 is not None:
            try:
                if self._started:
                    self._picam2.stop()
            except Exception:
                pass
            try:
                self._picam2.close()
            except Exception:
                pass
        self._picam2 = None
        self._started = False


# ==========================================================================
# CameraReader - capture on its own thread
# ==========================================================================
class CameraReader(threading.Thread):
    """Keeps the newest frame available without ever blocking the caller.

    Why this exists
    ---------------
    Picamera2's capture_array() blocks until a frame is ready. When that
    call sat at the top of the main loop, anything that stopped the camera
    delivering frames also stopped the ultrasonic reading being consulted
    and the danger beep being scheduled - the sensor was fine, but nobody
    was listening to it.

    That is not an acceptable shape for a collision warning. Capture now
    runs here, on its own thread, alongside the ultrasonic monitor, the
    beeper, the speech controller and the Gemini worker. The main loop
    calls latest(), which takes a lock and returns immediately.

    The Camera class itself is untouched: this only changes WHERE read()
    is called from.
    """

    def __init__(self, camera):
        super().__init__(name="camera", daemon=True)
        self._camera = camera
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        self._frame = None
        self._captured_at = None
        self._frame_count = 0
        self._error = None
        self._error_count = 0
        self._started_at = time.monotonic()
        self._stall_reported = False
        self._times = []            # recent capture timestamps, for FPS

    # ------------------------------------------------------------ capture
    def run(self):
        while not self._stop_event.is_set():
            try:
                frame = self._camera.read()
            except CameraError as exc:
                with self._lock:
                    self._error = str(exc)
                    self._error_count += 1
                self._stop_event.wait(config.CAMERA_RETRY_S)
                continue
            except Exception as exc:
                with self._lock:
                    self._error = "{}: {}".format(type(exc).__name__, exc)
                    self._error_count += 1
                self._stop_event.wait(config.CAMERA_RETRY_S)
                continue

            now = time.monotonic()
            with self._lock:
                self._frame = frame
                self._captured_at = now
                self._frame_count += 1
                self._error = None
                self._stall_reported = False
                self._times.append(now)
                if len(self._times) > 30:
                    del self._times[:-30]

    # ------------------------------------------------------------- read out
    def latest(self):
        """The newest frame and its age in seconds, or (None, None).

        Never blocks and never raises. Returns the frame object itself -
        callers that keep it must copy it, exactly as before.
        """
        with self._lock:
            frame = self._frame
            captured_at = self._captured_at
        if frame is None or captured_at is None:
            return None, None
        return frame, time.monotonic() - captured_at

    def fresh_frame(self):
        """The newest frame only if it is recent enough to act on."""
        frame, age = self.latest()
        if frame is None or age is None:
            return None
        if age > config.CAMERA_FRAME_MAX_AGE_S:
            return None
        return frame

    def snapshot(self):
        """Everything worth printing about the capture thread."""
        now = time.monotonic()
        with self._lock:
            captured_at = self._captured_at
            count = self._frame_count
            error = self._error
            error_count = self._error_count
            started_at = self._started_at
            times = list(self._times)

        age = None if captured_at is None else now - captured_at
        since_start = now - started_at

        fps = None
        if len(times) >= 2:
            span = times[-1] - times[0]
            if span > 0:
                fps = (len(times) - 1) / span

        # A stall is "we have had no frame for a long time", which covers
        # both "stopped delivering" and "never delivered one at all".
        quiet_for = age if age is not None else since_start
        stalled = quiet_for > config.CAMERA_STALL_WARN_S

        return {
            "frame_count": count,
            "age_s": age,
            "fps": fps,
            "error": error,
            "error_count": error_count,
            "stalled": stalled,
            "quiet_for_s": quiet_for,
            "alive": self.is_alive(),
        }

    def report_stall_once(self):
        """Print a loud one-off warning if the camera has gone quiet.

        Called from the main loop. This is the line that turns "nothing
        responds and I do not know why" into a diagnosis.
        """
        state = self.snapshot()
        if not state["stalled"]:
            return False
        with self._lock:
            if self._stall_reported:
                return False
            self._stall_reported = True
        print(
            "CAMERA STALLED: no frame for {:.1f}s (frames so far: {}). "
            "Ultrasonic sensing, beeps and speech are UNAFFECTED - only the "
            "preview and Gemini need frames.{}".format(
                state["quiet_for_s"], state["frame_count"],
                "  Last error: " + state["error"] if state["error"] else ""),
            flush=True,
        )
        return True

    def stop(self, timeout=2.0):
        """Ask the capture thread to finish.

        capture_array() cannot be interrupted, so if the camera is wedged
        this returns without the thread having exited. That is fine: it is
        a daemon thread, so it can never hold up shutdown.
        """
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout=timeout)
