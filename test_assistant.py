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


class WakeAckSpeech:
    def __init__(self):
        self.messages = []
        self._reads = 0
        self.ack_finished = False

    def say(self, text):
        self.messages.append(text)
        self._reads = 0
        return True

    @property
    def speaking(self):
        if self.messages[-1] != "Yes, sir?":
            return False
        self._reads += 1
        if self._reads >= 2:
            self.ack_finished = True
            return False
        return True


class WakeAckMic:
    def __init__(self, events): self.events = events
    def discard_pending(self): self.events.append("discard")


class FakeCameraReader:
    def __init__(self, frame=None): self.frame = frame; self.calls = 0
    def fresh_frame(self): self.calls += 1; return self.frame


class FakeUltrasonicMonitor:
    def __init__(self, distance=80.0, age=0.1):
        self.distance = distance; self.age = age
    def snapshot(self):
        return {"distance_cm": self.distance, "age_s": self.age}


class AssistantTests(unittest.TestCase):
    def setUp(self): self.voice = assistant.VoiceAssistant(FakeSpeech())

    def test_state_transition_and_speech(self):
        self.voice._say_and_wait("Paris")
        self.assertEqual(self.voice.state, "SPEAKING")
        self.assertEqual(self.voice.speech.messages, ["Paris"])

    def test_wake_ack_finishes_before_request_listening(self):
        speech = WakeAckSpeech()
        events = []
        voice = assistant.VoiceAssistant(speech)
        voice._handle_request = mock.Mock(
            side_effect=lambda mic: events.append(
                ("record", speech.ack_finished)))

        voice._handle_wake_detected(WakeAckMic(events))

        self.assertEqual(speech.messages, ["Yes, sir?"])
        self.assertEqual(events, ["discard", ("record", True)])
        voice._handle_request.assert_called_once()

    def test_wake_ack_failure_still_enters_request_listening(self):
        speech = mock.Mock()
        speech.say.return_value = False
        voice = assistant.VoiceAssistant(speech)
        voice._handle_request = mock.Mock()

        voice._handle_wake_detected(WakeAckMic([]))

        voice._handle_request.assert_called_once()

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

    def test_speech_start_timeout_is_separate_from_recording_timeout(self):
        with mock.patch.object(assistant.config, "ASSISTANT_SPEECH_START_TIMEOUT_S", 0):
            with mock.patch.object(assistant.config, "ASSISTANT_MAX_RECORDING_S", 99):
                self.assertEqual(self.voice._record_utterance(
                    FakeMic([b"\0\0" * 160])), b"")

    def test_end_of_speech_uses_continuous_silence(self):
        packets = [b"\x20\x00" * 160] + [b"\0\0" * 160] * 11
        with mock.patch.object(assistant.config, "ASSISTANT_ENERGY_THRESHOLD", 10):
            with mock.patch.object(assistant.config, "ASSISTANT_SILENCE_TIMEOUT_S", 1.0):
                data = self.voice._record_utterance(FakeMic(packets))
        self.assertGreater(len(data), 0)
        self.assertIsNotNone(self.voice._end_of_speech_at)

    def test_local_routes_cover_normal_visual_distance_and_combined(self):
        self.assertEqual(self.voice._route("What's the capital of France?"), "normal")
        self.assertEqual(self.voice._route("Where am I?"), "vision")
        self.assertEqual(self.voice._route("How far away is the thing?"), "distance")
        self.assertEqual(self.voice._route(
            "What is that object and how far away is it?"), "vision+distance")

    def test_where_am_i_uses_a_fresh_camera_frame(self):
        camera = FakeCameraReader(frame=object())
        voice = assistant.VoiceAssistant(
            FakeSpeech(), camera_reader=camera,
            gemini_worker=mock.Mock())
        voice._vision_answer = mock.Mock(return_value="You appear to be indoors.")
        self.assertEqual(voice._answer_routed("Where am I?", "vision"),
                         "You appear to be indoors.")
        self.assertEqual(camera.calls, 1)

    def test_distance_and_combined_routes_require_fresh_ultrasonic(self):
        monitor = FakeUltrasonicMonitor(distance=40.0)
        voice = assistant.VoiceAssistant(FakeSpeech(), ultrasonic_monitor=monitor)
        self.assertIn("40 centimeters", voice._answer_routed(
            "How far away is it?", "distance"))
        monitor.age = 2.0
        self.assertIn("can't determine", voice._answer_routed(
            "How far away is it?", "distance"))

    def test_visual_unavailable_and_visual_followup_keep_context(self):
        voice = assistant.VoiceAssistant(
            FakeSpeech(), camera_reader=FakeCameraReader(frame=None))
        self.assertIn("can't currently see", voice._answer_routed(
            "What is in front of me?", "vision"))
        voice._history.extend((("user", "What's in front of me?"),
                               ("model", "A red chair.")))
        self.assertIn("What's in front", voice._history_text())
        self.assertEqual(voice._route("What color did you say it was?"), "vision")

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
