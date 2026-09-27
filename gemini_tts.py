"""Reusable Gemini text-to-speech provider for Sense.

This module owns the Gemini API request and WAV gain adjustment.  Playback,
ALSA routing, beep coordination, and the offline fallback stay in
``hardware.audio`` so the provider can be replaced independently later.
"""

import array
import base64
import inspect
import io
import os
import sys
import wave

import config


class GeminiTTSError(RuntimeError):
    pass


class GeminiTTSProvider:
    """Generate complete WAV files with Gemini's Interactions API."""

    def __init__(self, model, voice, style, timeout_s=10.0,
                 volume_boost=1.4, peak_ceiling=0.92):
        self.model = model
        self.voice = voice
        self.style = style
        self.timeout_s = float(timeout_s)
        self.volume_boost = max(1.0, float(volume_boost))
        self.peak_ceiling = min(0.99, max(0.1, float(peak_ceiling)))
        self._client = None

    @property
    def description(self):
        return "Gemini {} / {}".format(self.model, self.voice)

    def open(self):
        try:
            from google import genai
        except ImportError as exc:
            raise GeminiTTSError(
                "google-genai>=2.25.0 is not installed"
            ) from exc

        key = os.environ.get(config.GEMINI_API_KEY_ENV, "").strip()
        if not key:
            raise GeminiTTSError("{} is missing".format(
                config.GEMINI_API_KEY_ENV))

        kwargs = {"api_key": key}
        try:
            parameters = inspect.signature(genai.Client).parameters
            takes_anything = any(
                item.kind is inspect.Parameter.VAR_KEYWORD
                for item in parameters.values()
            )
        except (TypeError, ValueError):
            parameters, takes_anything = {}, False

        def accepted(name):
            return takes_anything or name in parameters

        for flag in ("vertexai", "enterprise"):
            if accepted(flag):
                kwargs[flag] = False
        if accepted("http_options"):
            try:
                from google.genai import types
                kwargs["http_options"] = types.HttpOptions(
                    timeout=int(self.timeout_s * 1000))
            except Exception:
                pass

        try:
            self._client = genai.Client(**kwargs)
        except Exception as exc:
            raise GeminiTTSError("could not create Gemini client: {}: {}".format(
                type(exc).__name__, _redact(exc, key))) from None
        if not hasattr(self._client, "interactions"):
            self.close()
            raise GeminiTTSError(
                "google-genai is too old for the Interactions API")
        return self

    def synthesize(self, text, voice=None):
        if self._client is None:
            raise GeminiTTSError("Gemini TTS provider is not open")
        selected_voice = voice or self.voice
        interaction = self._client.interactions.create(
            model=self.model,
            input=[{
                "type": "user_input",
                "content": [{
                    "type": "text",
                    "text": text,
                    "annotations": [{
                        "type": "speech_metadata",
                        "style": self.style,
                    }],
                }],
            }],
            response_format={"type": "audio"},
            generation_config={
                "speech_config": [{"voice": selected_voice}],
            },
        )

        output_audio = getattr(interaction, "output_audio", None)
        encoded = getattr(output_audio, "data", None)
        if not encoded:
            raise GeminiTTSError("Gemini returned no audio")
        try:
            wav_bytes = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise GeminiTTSError("Gemini returned invalid base64 audio") from exc
        if not wav_bytes.startswith(b"RIFF") or wav_bytes[8:12] != b"WAVE":
            raise GeminiTTSError("Gemini returned audio that is not WAV")
        return boost_pcm_wav(
            wav_bytes, self.volume_boost, self.peak_ceiling)

    def close(self):
        client, self._client = self._client, None
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


def boost_pcm_wav(wav_bytes, desired_gain=1.4, peak_ceiling=0.92):
    """Raise 16-bit PCM volume while retaining headroom and never clipping.

    Gemini normally returns mono 16-bit PCM WAV.  Unknown WAV encodings are
    returned unchanged rather than risking damaged audio.
    """
    try:
        source = io.BytesIO(wav_bytes)
        with wave.open(source, "rb") as reader:
            params = reader.getparams()
            frames = reader.readframes(reader.getnframes())
        if params.sampwidth != 2 or not frames:
            return wav_bytes

        samples = array.array("h")
        samples.frombytes(frames)
        if sys.byteorder != "little":
            samples.byteswap()
        peak = max(abs(sample) for sample in samples)
        if peak <= 0:
            return wav_bytes

        safe_gain = (float(peak_ceiling) * 32767.0) / peak
        gain = min(max(1.0, float(desired_gain)), safe_gain)
        if gain <= 1.001:
            return wav_bytes

        for index, sample in enumerate(samples):
            samples[index] = max(-32768, min(32767, int(round(sample * gain))))
        if sys.byteorder != "little":
            samples.byteswap()

        output = io.BytesIO()
        with wave.open(output, "wb") as writer:
            writer.setparams(params)
            writer.writeframes(samples.tobytes())
        return output.getvalue()
    except (EOFError, ValueError, wave.Error):
        return wav_bytes


def _redact(value, secret):
    text = " ".join(str(value).split())
    if secret:
        text = text.replace(secret, "[REDACTED]")
    return text[:300]
