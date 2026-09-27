#!/usr/bin/env python3
"""Audition natural Gemini TTS voices without changing Sense's TTS path.

Run on the Raspberry Pi from the project directory:

    cd ~/Sense
    AUDIO_DEVICE=plughw:1,0 python3 test_voice.py

This script only calls Gemini and plays the returned WAV.  It does not import
or modify SpeechPlayer, so Sense continues to use eSpeak NG.
"""

import os
import shutil
import subprocess
import sys
import tempfile

import config  # Loads GEMINI_API_KEY from .env.local.
from gemini_tts import GeminiTTSProvider


MODEL = "gemini-3.8-flash-tts"
SAMPLE_TEXT = "Yes, sir. How can I help?"
DEFAULT_AUDIO_DEVICE = "plughw:1,0"
STYLE = (
    "Warm, calm, and reassuring female accessibility assistant. Speak in a "
    "slightly lower, warm register with smooth natural phrasing, clear "
    "pronunciation, and a confident but friendly conversational delivery. "
    "Do not sound like an announcer."
)

# Curated from Google's documented prebuilt Gemini TTS voices.  These labels
# describe the voice's documented character; STYLE supplies the Sense-specific
# delivery direction consistently for a fair audition.
VOICES = (
    ("Sulafat", "Warm"),
    ("Gacrux", "Mature"),
    ("Despina", "Smooth"),
    ("Vindemiatrix", "Gentle"),
    ("Erinome", "Clear"),
    ("Kore", "Firm"),
)


class VoiceTestError(RuntimeError):
    """A concise, user-facing voice test failure."""


def _audio_device():
    """Honor Sense's AUDIO_DEVICE, with its known headphone device as default."""
    return os.environ.get("AUDIO_DEVICE", "").strip() or \
        config.AUDIO_DEVICE or DEFAULT_AUDIO_DEVICE


def _provider():
    try:
        return GeminiTTSProvider(
            model=MODEL,
            voice="Despina",
            style=STYLE,
            timeout_s=config.GEMINI_TTS_TIMEOUT_S,
            volume_boost=config.GEMINI_TTS_VOLUME_BOOST,
            peak_ceiling=config.GEMINI_TTS_PEAK_CEILING,
        ).open()
    except Exception as exc:
        raise VoiceTestError(
            "Could not open Gemini TTS: {}: {}\nInstall/upgrade with:\n  "
            "pip3 install --user --break-system-packages "
            "'google-genai>=2.25.0'".format(type(exc).__name__, exc)
        ) from exc


def play_wav(wav, voice):
    """Play WAV bytes synchronously through Sense's explicitly selected ALSA device."""
    if shutil.which("aplay") is None:
        raise VoiceTestError(
            "aplay is not installed. Install it with: sudo apt install -y alsa-utils"
        )

    path = None
    try:
        with tempfile.NamedTemporaryFile(
                prefix="sense-{}-".format(voice.lower()),
                suffix=".wav", delete=False) as sample:
            sample.write(wav)
            path = sample.name

        command = ["aplay", "-q", "-D", _audio_device(), path]
        result = subprocess.run(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            timeout=30,
        )
        if result.returncode:
            detail = result.stderr.decode("utf-8", "replace").strip()
            raise VoiceTestError(
                "aplay exited {} on {}: {}".format(
                    result.returncode, _audio_device(),
                    " ".join(detail.split())[:240] or "no detail",
                )
            )
    except subprocess.TimeoutExpired as exc:
        raise VoiceTestError("Audio playback timed out.") from exc
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


def audition(provider, voice, character):
    print("\nGenerating {} ({})...".format(voice, character), flush=True)
    wav = provider.synthesize(SAMPLE_TEXT, voice=voice)
    print("Playing through {}...".format(_audio_device()), flush=True)
    play_wav(wav, voice)
    print("Finished {}.".format(voice), flush=True)


def _print_menu():
    print("\nSense Gemini voice tester")
    print("Model: {}".format(MODEL))
    print("Phrase: {!r}".format(SAMPLE_TEXT))
    print("Audio device: {}\n".format(_audio_device()))
    for number, (voice, character) in enumerate(VOICES, 1):
        print("  {}. {:13} {}".format(number, voice, character))
    print("  a. Play all voices")
    print("  q. Quit")


def main():
    try:
        provider = _provider()
    except VoiceTestError as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 1

    while True:
        _print_menu()
        try:
            choice = input("Choose a voice: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nDone.")
            return 0

        if choice in ("q", "quit", "exit"):
            print("Done.")
            return 0
        if choice in ("a", "all"):
            selected = VOICES
        else:
            try:
                selected = (VOICES[int(choice) - 1],)
                if int(choice) < 1:
                    raise IndexError
            except (ValueError, IndexError):
                print("Please enter 1-{}, a, or q.".format(len(VOICES)))
                continue

        for voice, character in selected:
            try:
                audition(provider, voice, character)
            except Exception as exc:
                print(
                    "ERROR testing {}: {}: {}".format(
                        voice, type(exc).__name__, exc),
                    file=sys.stderr,
                )
                if len(selected) == 1:
                    break


if __name__ == "__main__":
    sys.exit(main())
