import time
import unittest

from hardware.audio import BeepController


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


class BeepControllerTests(unittest.TestCase):
    def test_persistent_repeat_path_owns_the_cadence(self):
        player = PersistentFakePlayer()
        beeper = BeepController(player)
        beeper.start()
        try:
            beeper.set_interval(0.15)
            time.sleep(0.12)
            self.assertEqual(player.started, [0.15])
            self.assertEqual(beeper.beep_count, 0)
            beeper.set_interval(None)
            time.sleep(0.12)
            self.assertGreaterEqual(player.stopped, 1)
        finally:
            beeper.stop()


if __name__ == "__main__":
    unittest.main()
