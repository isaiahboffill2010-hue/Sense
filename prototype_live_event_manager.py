#!/usr/bin/env python3
"""Live Event Manager plus the independent ultrasonic safety system.

The person-awareness entry point continues to own the only CameraReader and
SSD inference worker. This wrapper only adds the repository's existing
ultrasonic monitor, alert policy, and beep controller on background threads.
"""

from __future__ import annotations

import sys
import threading

import alerts
import config
from hardware.audio import BeepController, BeepPlayer, TONE_WARNING
from hardware.ultrasonic import UltrasonicMonitor, UltrasonicSensor
from prototype_person_awareness import main as person_awareness_main


class UltrasonicSafety(threading.Thread):
    """Drive progressive warning audio from ``UltrasonicMonitor`` snapshots."""

    POLL_S = 0.02

    def __init__(self, monitor, beeper):
        super().__init__(name="ultrasonic-safety", daemon=True)
        self.monitor = monitor
        self.beeper = beeper
        self.policy = alerts.AlertPolicy()
        self._stop_event = threading.Event()
        self._last_snapshot = None

    def run(self):
        while not self._stop_event.is_set():
            snapshot = self.monitor.snapshot()
            signature = (
                snapshot.get("reading_count"),
                snapshot.get("healthy"),
                snapshot.get("distance_cm"),
                snapshot.get("out_of_range"),
            )
            if signature != self._last_snapshot:
                self._last_snapshot = signature
                distance = (snapshot.get("distance_cm")
                            if snapshot.get("healthy", True) else None)
                decision = self.policy.update(distance)
                if self.beeper is not None:
                    self.beeper.set_interval(decision.repeat_interval)
                    if decision.play_warning_tone:
                        self.beeper.play_once(TONE_WARNING)
            self._stop_event.wait(self.POLL_S)

    def stop(self, timeout=2.0):
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout=timeout)


class UltrasonicResources:
    """Owned resources, kept together so partial startup is easy to clean up."""

    def __init__(self, sensor, monitor, player=None, beeper=None, safety=None):
        self.sensor = sensor
        self.monitor = monitor
        self.player = player
        self.beeper = beeper
        self.safety = safety

    def stop(self):
        # Stop producers before closing the resources they use.
        for resource, method in (
            (self.safety, "stop"),
            (self.beeper, "stop"),
            (self.monitor, "stop"),
            (self.player, "close"),
            (self.sensor, "close"),
        ):
            if resource is not None:
                try:
                    getattr(resource, method)()
                except Exception as exc:
                    print("ULTRASONIC SHUTDOWN WARNING: {}: {}".format(
                        type(exc).__name__, exc), flush=True)


def start_ultrasonic():
    """Start ultrasonic safety, returning resources or ``None`` on failure."""
    sensor = monitor = None
    try:
        sensor = UltrasonicSensor(config.TRIG_PIN, config.ECHO_PIN)
        sensor.open()
        monitor = UltrasonicMonitor(sensor)
        monitor.start()
    except Exception as exc:
        if monitor is not None:
            monitor.stop()
        if sensor is not None:
            sensor.close()
        print("ULTRASONIC WARNING: unavailable ({}: {}). Camera/person "
              "awareness will continue.".format(type(exc).__name__, exc),
              flush=True)
        return None

    player = beeper = None
    try:
        player = BeepPlayer().open()
        beeper = BeepController(player)
        beeper.start()
    except Exception as exc:
        if player is not None:
            player.close()
        player = beeper = None
        print("ULTRASONIC AUDIO WARNING: beeps unavailable ({}: {}). "
              "Distance monitoring remains active.".format(
                  type(exc).__name__, exc), flush=True)

    safety = UltrasonicSafety(monitor, beeper)
    try:
        safety.start()
    except Exception as exc:
        UltrasonicResources(sensor, monitor, player, beeper).stop()
        print("ULTRASONIC WARNING: safety worker failed ({}: {}). "
              "Camera/person awareness will continue.".format(
                  type(exc).__name__, exc), flush=True)
        return None
    print("ULTRASONIC: ACTIVE ({}/{})".format(
        config.TRIG_PIN, config.ECHO_PIN), flush=True)
    return UltrasonicResources(sensor, monitor, player, beeper, safety)


def main(argv=None):
    """Run the existing Event Manager pipeline with ultrasonic on by default."""
    forwarded = list(sys.argv[1:] if argv is None else argv)
    ultrasonic_enabled = "--no-ultrasonic" not in forwarded
    forwarded = [arg for arg in forwarded if arg != "--no-ultrasonic"]

    resources = start_ultrasonic() if ultrasonic_enabled else None
    if not ultrasonic_enabled:
        print("ULTRASONIC: DISABLED (--no-ultrasonic)", flush=True)

    try:
        return person_awareness_main(["--event-manager", *forwarded])
    finally:
        if resources is not None:
            resources.stop()


if __name__ == "__main__":
    raise SystemExit(main())
