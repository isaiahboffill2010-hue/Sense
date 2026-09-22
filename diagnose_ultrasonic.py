#!/usr/bin/env python3
"""
diagnose_ultrasonic.py
======================

Run this ON THE RASPBERRY PI when the ultrasonic reading looks wrong -
especially when it behaves differently under systemd than it does by hand.

It prints the runtime environment, the GPIO numbers robot_hat ACTUALLY
resolved, and a burst of raw pings with per-ping timing, then summarises.
It touches nothing else: no camera, no Gemini, no audio.

Use it to compare the two situations directly:

    # by hand, the way that works
    cd /home/sense/Sense
    export AUDIO_DEVICE=plughw:1,0
    export ROBOT_HAT_GPIOCHIP=0
    sudo -E python3 diagnose_ultrasonic.py

    # in the service's exact environment
    sudo systemctl stop sense
    sudo systemd-run --pty --same-dir --wait \
        --property=Environment=ROBOT_HAT_GPIOCHIP=0 \
        --property=Environment=AUDIO_DEVICE=plughw:1,0 \
        --property=Environment=HOME=/home/sense \
        /usr/bin/python3 -u /home/sense/Sense/diagnose_ultrasonic.py

Wave your hand in front of the sensor during the burst. If the readings
track your hand in one case and sit at a fixed value in the other, the
summary tells you which of the two is which.

    --load    also run a busy Python thread during the burst, to reproduce
              the GIL starvation that a spinning main loop causes
"""

import os
import statistics
import sys
import threading
import time

import config

PINGS = 40


def show_environment():
    print("=" * 66)
    print("  Ultrasonic diagnostic")
    print("=" * 66)
    print("")
    print("Runtime")
    print("  python            : {}".format(sys.executable))
    print("  version           : {}".format(sys.version.split()[0]))
    print("  uid / euid        : {} / {}".format(os.getuid(), os.geteuid()))
    print("  cwd               : {}".format(os.getcwd()))
    print("  HOME              : {}".format(os.environ.get("HOME")))
    print("  GIL switch interval: {} s".format(sys.getswitchinterval()))
    print("")
    print("Environment variables that matter")
    for name in ("ROBOT_HAT_GPIOCHIP", "AUDIO_DEVICE", "DISPLAY",
                 "XDG_RUNTIME_DIR", "PYTHONPATH", "PATH"):
        value = os.environ.get(name)
        shown = value if name != "PATH" else (value or "")[:60] + "..."
        print("  {:<19}: {}".format(name, shown if value else "(not set)"))
    print("")
    print("Configured wiring (unchanged by this script)")
    print("  TRIG_PIN          : {}  -> expected GPIO{}".format(
        config.TRIG_PIN, config.ROBOT_HAT_PIN_TO_BCM.get(config.TRIG_PIN)))
    print("  ECHO_PIN          : {}  -> expected GPIO{}".format(
        config.ECHO_PIN, config.ROBOT_HAT_PIN_TO_BCM.get(config.ECHO_PIN)))
    print("")


def show_robot_hat():
    print("robot_hat")
    try:
        import robot_hat
    except Exception as exc:
        print("  NOT IMPORTABLE: {}: {}".format(type(exc).__name__, exc))
        print("  Under systemd this usually means HOME is wrong, so a")
        print("  pip --user install is invisible. See deploy/README.md.")
        return False

    print("  version           : {}".format(
        getattr(robot_hat, "__version__", "unknown")))
    print("  module path       : {}".format(
        getattr(robot_hat, "__file__", "unknown")))

    # Board type decides WHICH port->GPIO table robot_hat uses, which is the
    # thing that could silently move ECHO to a different pin.
    for attribute in ("board_type", "get_board_type", "__board_type__"):
        value = getattr(robot_hat, attribute, None)
        if value is not None:
            try:
                print("  {:<17} : {}".format(
                    attribute, value() if callable(value) else value))
            except Exception as exc:
                print("  {:<17} : unavailable ({})".format(attribute, exc))
    print("")
    return True


class BusyThread(threading.Thread):
    """Spins in pure Python, to reproduce GIL starvation on purpose."""

    def __init__(self):
        super().__init__(name="busy", daemon=True)
        self._stop_flag = threading.Event()
        self.iterations = 0

    def run(self):
        while not self._stop_flag.is_set():
            self.iterations += 1

    def halt(self):
        self._stop_flag.set()


def main():
    show_environment()
    if not show_robot_hat():
        return 1

    from hardware.ultrasonic import UltrasonicError, UltrasonicSensor

    sensor = UltrasonicSensor(config.TRIG_PIN, config.ECHO_PIN)
    try:
        sensor.open()
    except UltrasonicError as exc:
        print("OPEN FAILED: {}".format(exc))
        return 1

    resolved = getattr(sensor, "resolved_bcm", {})
    print("GPIO numbers robot_hat ACTUALLY resolved")
    mismatch = False
    for label, port in (("TRIG", config.TRIG_PIN), ("ECHO", config.ECHO_PIN)):
        expected = config.ROBOT_HAT_PIN_TO_BCM.get(port)
        actual = resolved.get(label)
        flag = ""
        if actual is None:
            flag = "  (could not read it back from this robot_hat build)"
        elif expected is not None and actual != expected:
            flag = "  <<< MISMATCH - this is very likely the bug"
            mismatch = True
        print("  {:<5} {} -> GPIO{}{}".format(
            label, port, actual if actual is not None else "?", flag))
    print("")

    busy = None
    if "--load" in sys.argv:
        print("Starting a busy Python thread to reproduce GIL starvation...")
        busy = BusyThread()
        busy.start()
        print("")

    print("Raw pings - wave your hand in front of the sensor now")
    print("-" * 66)
    values = []
    errors = 0
    try:
        for index in range(PINGS):
            started = time.monotonic()
            try:
                distance = sensor.measure_once()
            except UltrasonicError as exc:
                errors += 1
                print("  {:>3}  ERROR   {:>6.1f} ms  {}".format(
                    index + 1, (time.monotonic() - started) * 1000.0,
                    str(exc).splitlines()[0][:44]))
            else:
                values.append(distance)
                print("  {:>3}  {:>7.1f} cm  {:>6.1f} ms".format(
                    index + 1, distance,
                    (time.monotonic() - started) * 1000.0))
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("  interrupted")
    finally:
        if busy is not None:
            busy.halt()
        sensor.close()

    print("-" * 66)
    print("")
    print("Summary")
    print("  good pings        : {} / {}".format(len(values), PINGS))
    print("  errors            : {}".format(errors))
    if values:
        spread = max(values) - min(values)
        print("  min / max         : {:.1f} / {:.1f} cm".format(
            min(values), max(values)))
        print("  spread            : {:.1f} cm".format(spread))
        print("  median            : {:.1f} cm".format(
            statistics.median(values)))
        if len(values) > 1:
            print("  stdev             : {:.1f} cm".format(
                statistics.pstdev(values)))
        print("")
        if spread < 5.0:
            print("  VERDICT: the reading did NOT respond to your hand.")
            if mismatch:
                print("  robot_hat resolved a GPIO we did not expect - fix that")
                print("  first; ECHO on the wrong pin never responds.")
            else:
                print("  The pins resolved correctly, so suspect the pulse")
                print("  timing being preempted. A value near 184 cm is")
                print("  10.8 ms, about two 5 ms GIL switch intervals.")
                print("  Compare this run with and without --load.")
        else:
            print("  VERDICT: the reading tracked your hand. Sensor path OK.")
    if busy is not None:
        print("")
        print("  busy thread iterations: {:,}".format(busy.iterations))

    print("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
