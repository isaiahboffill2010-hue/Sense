"""Standalone physical warning-beep test for Sense headphones.

This bypasses the ultrasonic sensor, camera, Gemini, and voice assistant while
using the same BeepPlayer and BeepController path as main.py.
"""
import time

import alerts
from hardware.audio import BeepController, BeepPlayer


STAGES = (
    (90, "slow"),
    (60, "medium"),
    (40, "fast"),
    (20, "rapid danger"),
)
STAGE_SECONDS = 5.0


def main():
    player = BeepPlayer().open()
    beeper = BeepController(player)
    beeper.start()
    try:
        for distance, label in STAGES:
            interval = alerts.beep_interval_for(distance)
            print("SIMULATION: {} cm -> {} (interval {:.2f}s)".format(
                distance, label, interval), flush=True)
            beeper.set_interval(interval)
            time.sleep(STAGE_SECONDS)
    finally:
        beeper.stop()
        player.close()
        print("BEEP TEST COMPLETE", flush=True)


if __name__ == "__main__":
    main()
