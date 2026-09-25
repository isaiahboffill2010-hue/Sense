import time
import unittest

import alerts
import main
from hardware.audio import BeepController, SPEECH_ACTIVE


class PersistentFakePlayer:
    def __init__(self):
        self.started = []
        self.stopped = 0

    def start_repeating(self, interval):
        self.started.append(interval)
        return True

    def stop_repeating(self):
        self.stopped += 1

    def play(self, tone):
        raise AssertionError("persistent path should not spawn per-beep playback")


class IdleSpeech:
    speaking = False


class FakeBeeper:
    def __init__(self): self.intervals = []
    def set_interval(self, interval): self.intervals.append(interval)
    def set_speech_active(self, active): self.speech_active = active
    def snapshot(self):
        return {"requested_interval": self.intervals[-1],
                "persistent_interval": None}
    def play_once(self, tone): pass


class BeepControllerTests(unittest.TestCase):
    def test_persistent_repeat_path_owns_the_cadence(self):
        player = PersistentFakePlayer()
        beeper = BeepController(player)
        beeper.start()
        try:
            SPEECH_ACTIVE.set()
            beeper.set_speech_active(False)
            beeper.set_interval(0.15)
            time.sleep(0.12)
            self.assertEqual(player.started, [0.15])
            self.assertEqual(beeper.beep_count, 0)
            beeper.set_interval(None)
            time.sleep(0.12)
            self.assertGreaterEqual(player.stopped, 1)
        finally:
            SPEECH_ACTIVE.clear()
            beeper.stop()

    def test_main_danger_command_is_not_silenced_by_stale_speech_flag(self):
        policy = alerts.AlertPolicy()
        beeper = FakeBeeper()
        SPEECH_ACTIVE.set()
        try:
            status = main.apply_alert_policy(
                policy, {"distance_cm": 20.0}, beeper, speech=IdleSpeech())
        finally:
            SPEECH_ACTIVE.clear()
        self.assertEqual(status, alerts.DANGER)
        self.assertEqual(beeper.intervals[-1], 0.15)
        self.assertFalse(beeper.speech_active)


if __name__ == "__main__":
    unittest.main()
