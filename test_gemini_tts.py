import base64
import io
import threading
import time
import unittest
import wave
from array import array
from unittest import mock

import config
import gemini_tts
from hardware import audio


def make_wav(samples=(1000, -1000, 2000, -2000)):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(array("h", samples).tobytes())
    return output.getvalue()


class FakeInteractions:
    def __init__(self, wav_bytes):
        self.wav_bytes = wav_bytes
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        encoded = base64.b64encode(self.wav_bytes).decode("ascii")
        output_audio = type("Audio", (), {"data": encoded})()
        return type("Interaction", (), {"output_audio": output_audio})()


class FakeFallback:
    description = "espeak-ng test fallback"

    def __init__(self):
        self.spoken = []
        self.failure = None
        self.stopped = False

    def speak(self, text):
        self.spoken.append(text)
        return True

    def is_speaking(self):
        return False

    def playback_failure(self):
        return self.failure

    def stop(self):
        self.stopped = True

    def close(self):
        self.stop()


class FakeProvider:
    model = "gemini-3.8-flash-tts"
    voice = "Despina"
    description = "Gemini test"

    def __init__(self, result=None, error=None):
        self.result = result or make_wav()
        self.error = error
        self.requests = []

    def synthesize(self, text):
        self.requests.append(text)
        if self.error:
            raise self.error
        return self.result

    def close(self):
        pass


class FakeProcess:
    def __init__(self, args, **kwargs):
        self.args = args
        self.stderr = io.BytesIO(b"")
        self.returncode = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15


class GeminiTTSProviderTests(unittest.TestCase):
    def test_despina_model_and_audio_are_selected(self):
        provider = gemini_tts.GeminiTTSProvider(
            config.GEMINI_TTS_MODEL, config.GEMINI_TTS_VOICE,
            config.GEMINI_TTS_STYLE, volume_boost=1.0)
        interactions = FakeInteractions(make_wav())
        provider._client = type("Client", (), {"interactions": interactions})()

        result = provider.synthesize("Sense ready.")

        self.assertTrue(result.startswith(b"RIFF"))
        request = interactions.calls[0]
        self.assertEqual(request["model"], "gemini-3.8-flash-tts")
        self.assertEqual(
            request["generation_config"]["speech_config"],
            [{"voice": "Despina"}],
        )

    def test_volume_boost_retains_safe_peak_headroom(self):
        result = gemini_tts.boost_pcm_wav(
            make_wav((10000, -10000)), desired_gain=3.0, peak_ceiling=0.92)
        with wave.open(io.BytesIO(result), "rb") as wav:
            samples = array("h", wav.readframes(wav.getnframes()))
        self.assertGreater(max(abs(value) for value in samples), 10000)
        self.assertLessEqual(max(abs(value) for value in samples),
                             int(32767 * 0.92) + 1)


class GeminiSpeechPlayerTests(unittest.TestCase):
    def setUp(self):
        audio.SPEECH_ACTIVE.clear()

    def tearDown(self):
        audio.SPEECH_ACTIVE.clear()

    def test_gemini_success_uses_pinned_audio_device(self):
        provider = FakeProvider()
        fallback = FakeFallback()
        recorded = []

        def popen(args, **kwargs):
            recorded.append(args)
            return FakeProcess(args, **kwargs)

        with mock.patch.object(audio.config, "AUDIO_DEVICE", "plughw:1,0"), \
                mock.patch.object(audio.subprocess, "Popen", side_effect=popen):
            player = audio.GeminiSpeechPlayer(provider, fallback)
            player.speak("Yes, sir?")
            self.assertFalse(player.is_speaking())

        self.assertEqual(provider.requests, ["Yes, sir?"])
        self.assertEqual(fallback.spoken, [])
        self.assertEqual(recorded[0][0:4],
                         ["aplay", "-q", "-D", "plughw:1,0"])
        self.assertIsNone(player.playback_failure())

    def test_generation_failure_uses_same_text_with_espeak_fallback(self):
        provider = FakeProvider(error=TimeoutError("offline"))
        fallback = FakeFallback()
        player = audio.GeminiSpeechPlayer(provider, fallback)

        player.speak("Doorway ahead.")

        self.assertEqual(fallback.spoken, ["Doorway ahead."])
        self.assertFalse(player.is_speaking())
        self.assertIsNone(player.playback_failure())

    def test_gemini_playback_failure_also_falls_back(self):
        provider = FakeProvider()
        fallback = FakeFallback()

        class FailedProcess(FakeProcess):
            def __init__(self, args, **kwargs):
                super().__init__(args, **kwargs)
                self.returncode = 1
                self.stderr = io.BytesIO(b"audio open error")

        with mock.patch.object(audio.subprocess, "Popen", FailedProcess):
            player = audio.GeminiSpeechPlayer(provider, fallback)
            player.speak("Person ahead.")
            self.assertFalse(player.is_speaking())

        self.assertEqual(fallback.spoken, ["Person ahead."])
        self.assertIsNone(player.playback_failure())

    def test_generation_stays_on_speech_thread_and_does_not_pause_beeps(self):
        entered = threading.Event()
        release = threading.Event()

        class BlockingProvider(FakeProvider):
            def synthesize(self, text):
                self.requests.append(text)
                entered.set()
                release.wait(2)
                return self.result

        provider = BlockingProvider()
        fallback = FakeFallback()
        with mock.patch.object(audio.subprocess, "Popen", FakeProcess):
            player = audio.GeminiSpeechPlayer(provider, fallback)
            controller = audio.SpeechController(player)
            controller.start()
            started = time.monotonic()
            try:
                self.assertTrue(controller.say("Chair ahead."))
                self.assertLess(time.monotonic() - started, 0.1)
                self.assertTrue(entered.wait(1))
                self.assertTrue(controller.busy)
                self.assertFalse(controller.speaking)
                self.assertFalse(audio.SPEECH_ACTIVE.is_set())
            finally:
                release.set()
                controller.wait_until_idle(2)
                controller.stop()

    def test_beep_coordination_covers_playback_then_releases(self):
        process = FakeProcess([])
        process.returncode = None
        provider = FakeProvider()
        fallback = FakeFallback()
        with mock.patch.object(audio.subprocess, "Popen", return_value=process):
            player = audio.GeminiSpeechPlayer(provider, fallback)
            player.speak("Table ahead.")
            self.assertTrue(audio.SPEECH_ACTIVE.is_set())
            self.assertTrue(player.is_speaking())
            process.returncode = 0
            self.assertFalse(player.is_speaking())
            self.assertFalse(audio.SPEECH_ACTIVE.is_set())


if __name__ == "__main__":
    unittest.main()
