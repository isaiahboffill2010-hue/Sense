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
