"""
hardware/audio.py
=================

Warning beeps through the Raspberry Pi's normal audio output, i.e. through
the Pi 3's own 3.5mm jack into your stereo headphones. No GPIO buzzer, no
internet.

Note on the Robot HAT: the HAT has its own onboard MONO I2S speaker but no
headphone socket, so the headphones stay in the Pi's jack. If SunFounder's
i2samp.sh has made the HAT speaker the default ALSA output, set
AUDIO_DEVICE in config.py to force the beeps back to the headphones - both
backends below honour it.

Three pieces live here:

    generate_beep_wav()   writes a short stereo beep to assets/beep.wav
                          using only the Python standard library
    BeepPlayer            opens an audio backend and plays that WAV
                          without blocking the caller
    BeepController        a background thread that repeats the beep at a
                          chosen interval

Backends, tried in order (both are real audio - nothing is simulated):

    1. pygame.mixer   sudo apt install -y python3-pygame
    2. aplay          part of alsa-utils, normally already installed

Honesty policy
--------------
If neither backend works, open() raises AudioError with the real message
from the backend. The program keeps running so you can still test the
camera and the sensor, and the overlay says "Audio: FAIL".
"""

import array
import math
import os
import subprocess
import sys
import threading
import time

import config


class AudioError(RuntimeError):
    """Raised when audio cannot be initialised or played."""


# ==========================================================================
# Beep generation (standard library only - no numpy, no internet)
# ==========================================================================
def generate_beep_wav(path=None, force=False):
    """Write a short stereo sine-wave beep to `path` and return the path.

    The same tone goes into the left and the right channel, so the beep is
    centred in stereo headphones. A 10 ms fade in and fade out removes the
    click you would otherwise hear at the start and end of the tone.
    """
    import wave

    path = path or config.BEEP_WAV_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists() and not force:
        return path

    sample_rate = config.BEEP_SAMPLE_RATE
    total_samples = max(1, int(sample_rate * config.BEEP_DURATION_S))
    fade_samples = max(1, int(sample_rate * 0.010))
    amplitude = 32767.0 * max(0.0, min(1.0, config.BEEP_VOLUME))
    channels = 2 if config.BEEP_CHANNELS >= 2 else 1

    samples = array.array("h")
    for index in range(total_samples):
        # Envelope: fade in, hold, fade out.
        if index < fade_samples:
            envelope = index / fade_samples
        elif index > total_samples - fade_samples:
            envelope = max(0.0, (total_samples - index) / fade_samples)
        else:
            envelope = 1.0

        value = amplitude * envelope * math.sin(
            2.0 * math.pi * config.BEEP_FREQUENCY_HZ * index / sample_rate
        )
        sample = max(-32768, min(32767, int(value)))
        for _ in range(channels):
            samples.append(sample)

    # WAV data must be little-endian; the Pi already is, but be correct.
    if sys.byteorder == "big":
        samples.byteswap()

    try:
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(channels)
            wav.setsampwidth(2)          # 16-bit
            wav.setframerate(sample_rate)
            wav.writeframes(samples.tobytes())
    except OSError as exc:
        raise AudioError(
            "Could not write the beep file {}: {}".format(path, exc)
        ) from exc

    return path


# ==========================================================================
# Playback backends
# ==========================================================================
class _PygameBackend:
    """Plays the beep through SDL / pygame.mixer. Non-blocking by design."""

    name = "pygame.mixer"

    def __init__(self, wav_path):
        # Stops pygame printing its "Hello from the pygame community" banner.
        os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

        # SDL's ALSA backend takes its device from AUDIODEV. This is how we
        # keep the beeps in the headphones when a Robot HAT I2S speaker has
        # taken over as the system default output.
        if config.AUDIO_DEVICE:
            os.environ["SDL_AUDIODRIVER"] = "alsa"
            os.environ["AUDIODEV"] = config.AUDIO_DEVICE

        import pygame

        self._pygame = pygame
        pygame.mixer.init(
            frequency=config.BEEP_SAMPLE_RATE,
            size=-16,
            channels=2,
            buffer=512,          # small buffer -> low beep latency
        )
        # SDL can silently fall back to its "dummy" driver, which accepts
        # every sound and plays nothing. That would look like working audio
        # while your headphones stay silent, so reject it outright.
        driver = self._sdl_driver(pygame)
        if driver and "dummy" in driver.lower():
            pygame.mixer.quit()
            raise AudioError(
                "SDL selected the 'dummy' (silent) audio driver, so nothing "
                "would actually reach your headphones."
            )

        self._sound = pygame.mixer.Sound(str(wav_path))
        self.description = "pygame.mixer / SDL ({})".format(driver or "auto")

    @staticmethod
    def _sdl_driver(pygame):
        """Name of the SDL audio driver in use, or None if we cannot tell."""
        getter = getattr(pygame.mixer, "get_driver", None)
        if callable(getter):
            try:
                return str(getter())
            except Exception:
                pass
        return os.environ.get("SDL_AUDIODRIVER")

    def play(self):
        # Sound.play() returns immediately; the mixer thread does the work.
        self._sound.play()

    def close(self):
        try:
            self._pygame.mixer.quit()
        except Exception:
            pass


class _AplayBackend:
    """Plays the beep by spawning `aplay`. Non-blocking via Popen."""

    name = "aplay"

    def __init__(self, wav_path):
        self._wav_path = str(wav_path)
        self._processes = []

        # "-D <device>" pins playback to one ALSA device, so a Robot HAT I2S
        # speaker that has become the system default cannot steal the beeps.
        self._device_args = (
            ["-D", config.AUDIO_DEVICE] if config.AUDIO_DEVICE else []
        )

        try:
            probe = subprocess.run(
                ["aplay", "--version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=5,
            )
        except FileNotFoundError as exc:
            raise AudioError(
                "aplay was not found. Install it with: "
                "sudo apt install -y alsa-utils"
            ) from exc
        except Exception as exc:
            raise AudioError(
                "aplay could not be started: {}: {}".format(type(exc).__name__, exc)
            ) from exc

        version = probe.stdout.decode("utf-8", "replace").strip().splitlines()
        self.description = "aplay ({}{})".format(
            version[0] if version else "alsa-utils",
            " -> " + config.AUDIO_DEVICE if config.AUDIO_DEVICE else "",
        )

        # Actually play the file once, synchronously, so an unusable audio
        # device is reported now rather than silently swallowed later.
        try:
            result = subprocess.run(
                ["aplay", "-q"] + self._device_args + [self._wav_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=10,
            )
        except Exception as exc:
            raise AudioError(
                "aplay failed to play the test beep: {}: {}".format(
                    type(exc).__name__, exc
                )
            ) from exc

        if result.returncode != 0:
            raise AudioError(
                "aplay could not play to the audio device (exit {}): {}".format(
                    result.returncode,
                    result.stderr.decode("utf-8", "replace").strip() or "no detail",
                )
            )

    def play(self):
        # Drop finished processes so we do not leave zombies behind.
        self._processes = [p for p in self._processes if p.poll() is None]
        if len(self._processes) >= 4:
            return          # audio is backing up; skip this beep
        try:
            self._processes.append(
                subprocess.Popen(
                    ["aplay", "-q"] + self._device_args + [self._wav_path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
        except Exception as exc:
            raise AudioError(
                "aplay failed: {}: {}".format(type(exc).__name__, exc)
            ) from exc

    def close(self):
        for process in self._processes:
            try:
                if process.poll() is None:
                    process.terminate()
            except Exception:
                pass
        self._processes = []


# ==========================================================================
# BeepPlayer - picks a backend and plays the beep
# ==========================================================================
class BeepPlayer:
    """Opens the first working audio backend and plays the beep on demand."""

    def __init__(self):
        self._backend = None
        self.wav_path = None
        self.description = "not opened"

    def open(self):
        """Generate the beep and open an audio backend.

        Raises AudioError, listing what every backend said, if none work.
        """
        self.wav_path = generate_beep_wav()

        problems = []
        for factory in (_PygameBackend, _AplayBackend):
            try:
                self._backend = factory(self.wav_path)
            except ImportError as exc:
                problems.append("{}: not installed ({})".format(factory.name, exc))
            except AudioError as exc:
                problems.append("{}: {}".format(factory.name, exc))
            except Exception as exc:
                problems.append(
                    "{}: {}: {}".format(factory.name, type(exc).__name__, exc)
                )
            else:
                self.description = self._backend.description
                return self

        raise AudioError(
            "Headphone/audio output unavailable. Tried:\n  "
            + "\n  ".join(problems)
            + "\n  Check that the headphones are in the Raspberry Pi's own 3.5mm jack"
            "\n  (the Robot HAT has no headphone socket) and that the analogue jack,"
            "\n  not HDMI or the HAT's I2S speaker, is the selected output."
            "\n  List devices with:       aplay -l   and   aplay -L"
            "\n  Choose the output with:  sudo raspi-config -> System Options -> Audio"
            "\n  Or pin it explicitly by setting AUDIO_DEVICE in config.py."
        )

    def play(self):
        """Start one beep and return immediately."""
        if self._backend is None:
            raise AudioError("Audio is not open. Call open() first.")
        self._backend.play()

    def close(self):
        if self._backend is not None:
            self._backend.close()
            self._backend = None


# ==========================================================================
# BeepController - repeats the beep on a background thread
# ==========================================================================
class BeepController(threading.Thread):
    """Repeats the beep at an interval that the main loop can change freely.

    All of the waiting happens on this thread, so the camera loop never has
    to call time.sleep() for the beep rhythm and never stutters because of it.

    set_interval(None)  -> silence
    set_interval(0.15)  -> a beep every 150 ms
    """

    # How often we re-check the interval while silent.
    IDLE_POLL_S = 0.05

    def __init__(self, player):
        super().__init__(name="beeper", daemon=True)
        self._player = player
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._interval = None
        self._error = None
        self._beep_count = 0

    def set_interval(self, interval):
        """Set seconds between beeps, or None for silence."""
        with self._lock:
            self._interval = interval

    @property
    def error(self):
        with self._lock:
            return self._error

    @property
    def beep_count(self):
        with self._lock:
            return self._beep_count

    def run(self):
        while not self._stop_event.is_set():
            with self._lock:
                interval = self._interval

            if interval is None:
                # Silent: poll often so we react quickly when danger appears.
                self._stop_event.wait(self.IDLE_POLL_S)
                continue

            try:
                self._player.play()
            except AudioError as exc:
                with self._lock:
                    self._error = str(exc)
            except Exception as exc:
                with self._lock:
                    self._error = "{}: {}".format(type(exc).__name__, exc)
            else:
                with self._lock:
                    self._error = None
                    self._beep_count += 1

            # Event.wait() returns instantly when stop() is called, so Q and
            # Ctrl+C never have to wait out a beep interval.
            self._stop_event.wait(max(0.05, interval))

    def stop(self, timeout=2.0):
        self.set_interval(None)
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout=timeout)


def play_test_beep(player):
    """One beep at startup so you can confirm the headphones really work."""
    player.play()
    time.sleep(config.BEEP_DURATION_S + 0.05)
