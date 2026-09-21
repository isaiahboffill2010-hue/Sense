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

Two tones, generated locally with the standard library:

    TONE_DANGER    1000 Hz, urgent   - repeated while closer than 25 cm
    TONE_WARNING    660 Hz, subtle   - played ONCE on entering 25-50 cm

Three pieces live here:

    generate_all_tones()  writes both tones into assets/ as WAV files
    BeepPlayer            opens an audio backend and plays either tone
                          without blocking the caller
    BeepController        a background thread that repeats the danger beep
                          and/or plays one-shot tones

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


# The two sounds this project makes.
TONE_DANGER = "danger"     # urgent, repeated while closer than 25 cm
TONE_WARNING = "warning"   # subtle, played once on entering 25-50 cm


# ==========================================================================
# Beep generation (standard library only - no numpy, no internet)
# ==========================================================================
def generate_tone_wav(path, frequency_hz, duration_s, volume, force=False):
    """Write a short stereo sine-wave tone to `path` and return the path.

    The same tone goes into the left and the right channel, so it is centred
    in stereo headphones. A 10 ms fade in and fade out removes the click you
    would otherwise hear at the start and end of the tone.
    """
    import wave

    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists() and not force:
        return path

    sample_rate = config.BEEP_SAMPLE_RATE
    total_samples = max(1, int(sample_rate * duration_s))
    fade_samples = max(1, int(sample_rate * 0.010))
    amplitude = 32767.0 * max(0.0, min(1.0, volume))
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
            2.0 * math.pi * frequency_hz * index / sample_rate
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
            "Could not write the tone file {}: {}".format(path, exc)
        ) from exc

    return path


def generate_all_tones(force=False):
    """Create both tones and return {tone name: path}."""
    return {
        TONE_DANGER: generate_tone_wav(
            config.BEEP_WAV_PATH,
            config.BEEP_FREQUENCY_HZ,
            config.BEEP_DURATION_S,
            config.BEEP_VOLUME,
            force=force,
        ),
        TONE_WARNING: generate_tone_wav(
            config.WARNING_TONE_WAV_PATH,
            config.WARNING_TONE_FREQUENCY_HZ,
            config.WARNING_TONE_DURATION_S,
            config.WARNING_TONE_VOLUME,
            force=force,
        ),
    }


# ==========================================================================
# Playback backends
# ==========================================================================
class _PygameBackend:
    """Plays the beep through SDL / pygame.mixer. Non-blocking by design."""

    name = "pygame.mixer"

    def __init__(self, wav_paths):
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

        self._sounds = {
            tone: pygame.mixer.Sound(str(path)) for tone, path in wav_paths.items()
        }
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

    def play(self, tone=TONE_DANGER):
        # Sound.play() returns immediately; the mixer thread does the work.
        self._sounds[tone].play()

    def close(self):
        try:
            self._pygame.mixer.quit()
        except Exception:
            pass


class _AplayBackend:
    """Plays the beep by spawning `aplay`. Non-blocking via Popen."""

    name = "aplay"

    def __init__(self, wav_paths):
        self._wav_paths = {tone: str(path) for tone, path in wav_paths.items()}
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
                ["aplay", "-q"] + self._device_args + [self._wav_paths[TONE_DANGER]],
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

    def play(self, tone=TONE_DANGER):
        # Drop finished processes so we do not leave zombies behind.
        self._processes = [p for p in self._processes if p.poll() is None]
        if len(self._processes) >= 4:
            return          # audio is backing up; skip this beep
        try:
            self._processes.append(
                subprocess.Popen(
                    ["aplay", "-q"] + self._device_args + [self._wav_paths[tone]],
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
        self.tone_paths = {}
        self.wav_path = None          # the danger beep, for the startup report
        self.description = "not opened"

    def open(self):
        """Generate both tones and open an audio backend.

        Raises AudioError, listing what every backend said, if none work.
        """
        self.tone_paths = generate_all_tones()
        self.wav_path = self.tone_paths[TONE_DANGER]

        problems = []
        for factory in (_PygameBackend, _AplayBackend):
            try:
                self._backend = factory(self.tone_paths)
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

    def play(self, tone=TONE_DANGER):
        """Start one tone and return immediately."""
        if self._backend is None:
            raise AudioError("Audio is not open. Call open() first.")
        self._backend.play(tone)

    def close(self):
        if self._backend is not None:
            self._backend.close()
            self._backend = None


# ==========================================================================
# BeepController - repeats the beep on a background thread
# ==========================================================================
class BeepController(threading.Thread):
    """Plays tones on a background thread so the camera loop never waits.

    Two independent ways to make a sound:

        set_interval(None)   -> stop repeating
        set_interval(0.15)   -> repeat the danger beep every 150 ms
        play_once(tone)      -> play one tone, as soon as possible, once

    All of the waiting happens on this thread, so the camera loop never has
    to call time.sleep() for the beep rhythm and never stutters because of it.
    """

    # How often we re-check for work while not repeating a beep. Also the
    # worst-case delay before a one-shot tone is heard.
    IDLE_POLL_S = 0.05

    def __init__(self, player):
        super().__init__(name="beeper", daemon=True)
        self._player = player
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._interval = None
        self._pending = []           # one-shot tones waiting to be played
        self._error = None
        self._beep_count = 0

    def set_interval(self, interval):
        """Set seconds between repeated danger beeps, or None for silence."""
        with self._lock:
            self._interval = interval

    def play_once(self, tone):
        """Queue a single tone. Returns immediately; the thread plays it."""
        with self._lock:
            # Never let a backlog build up if the audio device is struggling.
            if len(self._pending) < 4:
                self._pending.append(tone)

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
            # One-shots first, so a warning tone is never held up waiting
            # for a danger beep interval to elapse.
            with self._lock:
                pending, self._pending = self._pending, []
            for tone in pending:
                self._play(tone)

            with self._lock:
                interval = self._interval

            if interval is None:
                # Not repeating: poll often so we react quickly to new work.
                self._stop_event.wait(self.IDLE_POLL_S)
                continue

            self._play(TONE_DANGER)

            # Event.wait() returns instantly when stop() is called, so Q and
            # Ctrl+C never have to wait out a beep interval.
            self._stop_event.wait(max(0.05, interval))

    def _play(self, tone):
        """Play one tone, recording any failure instead of raising."""
        try:
            self._player.play(tone)
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

    def stop(self, timeout=2.0):
        self.set_interval(None)
        with self._lock:
            self._pending = []
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout=timeout)


def play_test_beep(player):
    """One beep at startup so you can confirm the headphones really work."""
    player.play(TONE_DANGER)
    time.sleep(config.BEEP_DURATION_S + 0.05)
