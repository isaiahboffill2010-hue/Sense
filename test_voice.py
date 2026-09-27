#!/usr/bin/env python3
"""Audition natural Gemini TTS voices without changing Sense's TTS path.

Run on the Raspberry Pi from the project directory:

    cd ~/Sense
    AUDIO_DEVICE=plughw:1,0 python3 test_voice.py

This script only calls Gemini and plays the returned WAV.  It does not import
or modify SpeechPlayer, so Sense continues to use eSpeak NG.
"""

import base64
import os
import shutil
import subprocess
import sys
import tempfile

import config  # Loads GEMINI_API_KEY from .env.local.


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


def _api_key():
    key = os.environ.get(config.GEMINI_API_KEY_ENV, "").strip()
    if not key:
        raise VoiceTestError(
            "GEMINI_API_KEY is missing. Add it to ~/Sense/.env.local."
        )
    return key


def _client():
    try:
        from google import genai
    except ImportError as exc:
        raise VoiceTestError(
            "The current Google Gen AI SDK is required. Install/upgrade it "
            "with:\n  pip3 install --user --break-system-packages "
            "'google-genai>=2.25.0'"
        ) from exc

    client = genai.Client(api_key=_api_key())
    if not hasattr(client, "interactions"):
        raise VoiceTestError(
            "google-genai is too old for the Gemini TTS Interactions API. "
            "Upgrade it with:\n  pip3 install --user --break-system-packages "
            "'google-genai>=2.25.0'"
        )
    return client


def generate_wav(client, voice):
    """Generate the fixed audition phrase and return complete WAV bytes."""
    interaction = client.interactions.create(
        model=MODEL,
        input=[{
            "type": "user_input",
            "content": [{
                "type": "text",
                "text": SAMPLE_TEXT,
                "annotations": [{
                    "type": "speech_metadata",
                    "style": STYLE,
                }],
            }],
        }],
        response_format={"type": "audio"},
        generation_config={
            "speech_config": [{"voice": voice}],
        },
    )

    output_audio = getattr(interaction, "output_audio", None)
    encoded = getattr(output_audio, "data", None)
    if not encoded:
        raise VoiceTestError("Gemini returned no audio for {}.".format(voice))

    try:
        wav = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise VoiceTestError(
            "Gemini returned invalid audio for {}.".format(voice)
        ) from exc

    if not wav.startswith(b"RIFF") or wav[8:12] != b"WAVE":
        raise VoiceTestError(
            "Gemini did not return the expected WAV audio for {}.".format(voice)
        )
    return wav


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


def audition(client, voice, character):
    print("\nGenerating {} ({})...".format(voice, character), flush=True)
    wav = generate_wav(client, voice)
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
        client = _client()
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
                audition(client, voice, character)
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
