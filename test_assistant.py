import os
import tempfile
import unittest
from unittest import mock

import assistant


class FakeSpeech:
    def __init__(self): self.messages = []; self._reads = 0
    def say(self, text): self.messages.append(text); self._reads = 0; return True
    @property
    def speaking(self): self._reads += 1; return self._reads == 2


class FakeMic:
    def __init__(self, chunks): self.chunks = iter(chunks)
    def read(self): return next(self.chunks)


class AssistantTests(unittest.TestCase):
    def setUp(self): self.voice = assistant.VoiceAssistant(FakeSpeech())

    def test_state_transition_and_speech(self):
        self.voice._say_and_wait("Paris")
        self.assertEqual(self.voice.state, "SPEAKING")
        self.assertEqual(self.voice.speech.messages, ["Paris"])

    def test_microphone_failure_is_named(self):
        with mock.patch("assistant.shutil.which", return_value=None):
            with self.assertRaises(assistant.MicrophoneError):
                assistant.MicrophoneStream().open()

    def test_silence_returns_no_audio(self):
        quiet = b"\0\0" * 1600
        with mock.patch.object(assistant.config, "ASSISTANT_SPEECH_START_TIMEOUT_S", 0):
            self.assertEqual(self.voice._record_utterance(FakeMic([quiet])), b"")

    def test_rejects_obvious_timestamp_noise_from_stt(self):
        self.assertEqual(self.voice._clean_transcript("00:00"), "")
        self.assertEqual(self.voice._clean_transcript("What is the capital of France?"),
                         "What is the capital of France?")

    def test_post_wake_capture_keeps_a_pre_roll_buffer(self):
        with mock.patch.object(assistant.config, "ASSISTANT_PRE_SPEECH_BUFFER_S", 0.2):
            with mock.patch.object(assistant.config, "ASSISTANT_SILENCE_TIMEOUT_S", 0.2):
                with mock.patch.object(assistant.config, "ASSISTANT_ENERGY_THRESHOLD", 10):
                    packets = [b"\x00\x00" * 160, b"\x20\x00" * 160,
                               b"\x20\x00" * 160, b"\x00\x00" * 160]
                    data = self.voice._record_utterance(FakeMic(packets))
                    self.assertGreater(len(data), 0)

    def _run_failure(self, answer_error=False):
        self.voice._record_utterance = mock.Mock(return_value=b"audio")
        self.voice._transcribe = mock.Mock(return_value="hello" if answer_error else "")
        self.voice._answer = mock.Mock(side_effect=RuntimeError("offline"))
        with mock.patch("assistant.pcm_to_wav") as make:
            fd, path = tempfile.mkstemp(); os.close(fd); make.return_value = path
            self.voice._handle_request(FakeMic([]))

    def test_transcription_failure_fallback(self):
        self._run_failure(); self.assertIn("I didn't catch that.", self.voice.speech.messages)

    def test_gemini_failure_fallback(self):
        self._run_failure(True)
        self.assertIn("I can't reach the assistant right now.", self.voice.speech.messages)

    def test_shutdown(self):
        self.voice.stop(); self.assertTrue(self.voice._stop_event.is_set())


if __name__ == "__main__": unittest.main()
