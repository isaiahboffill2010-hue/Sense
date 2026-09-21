"""
hardware/ultrasonic.py
======================

SunFounder ultrasonic module on a SunFounder Robot HAT.

Why this uses robot_hat instead of raw GPIO
-------------------------------------------
The sensor is not a bare HC-SR04 wired to the Pi header. It is the module
SunFounder supplies with the Robot HAT: an HC-SR04-style head with their own
interface board on the back, plugged into the HAT's 3-pin DIGITAL ports.

SunFounder ships `robot_hat`, the official library for that board, and it is
the documented way to drive this exact sensor:

    from robot_hat import Ultrasonic, Pin
    ultrasonic = Ultrasonic(Pin("D2"), Pin("D3"))
    distance = ultrasonic.read()

Using it means the Robot HAT's own port names ("D2", "D3") are what appears
in config.py, which is what is printed on the board next to the sockets - so
the code reads the same way the hardware is labelled. The ping timing, the
pulse measurement and the cm conversion all come from SunFounder's library.

What this module adds on top
----------------------------
robot_hat.Ultrasonic.read() reports failure by RETURNING a negative sentinel
(-1, and -2 in some versions) rather than raising. A returned -1 that leaked
into the rest of the program would show up as a distance of "-1 cm" and be
classified DANGER, which is exactly the kind of fake reading this project
must never produce. So this module:

  * translates those sentinels into a real UltrasonicError
  * takes a median of several pings to reject spikes
  * runs the whole thing on a background thread so the camera never waits

Honesty policy
--------------
This module never invents a reading. If no echo comes back you get an
UltrasonicError saying so - not a number.
"""

import threading
import time

import config


class UltrasonicError(RuntimeError):
    """Raised when a measurement fails (no echo, bad wiring, HAT not found)."""


class UltrasonicSensor:
    """Blocking, single-shot access to the Robot HAT ultrasonic module."""

    def __init__(self, trig_pin=None, echo_pin=None, timeout=None):
        # Robot HAT digital port names, e.g. "D2" / "D3".
        self.trig_pin = config.TRIG_PIN if trig_pin is None else trig_pin
        self.echo_pin = config.ECHO_PIN if echo_pin is None else echo_pin
        self.timeout = config.ULTRASONIC_TIMEOUT_S if timeout is None else timeout
        self._ultrasonic = None

    # ---------------------------------------------------------------- setup
    @staticmethod
    def pins_are_configured():
        """True once TRIG_PIN and ECHO_PIN are set in config.py."""
        return config.TRIG_PIN is not None and config.ECHO_PIN is not None

    @property
    def description(self):
        """e.g. 'robot_hat TRIG=D2 (GPIO27) ECHO=D3 (GPIO22)'."""
        return "robot_hat TRIG={} ({}) ECHO={} ({})".format(
            self.trig_pin, self._bcm_label(self.trig_pin),
            self.echo_pin, self._bcm_label(self.echo_pin),
        )

    @staticmethod
    def _bcm_label(pin_name):
        bcm = config.ROBOT_HAT_PIN_TO_BCM.get(pin_name)
        return "GPIO{}".format(bcm) if bcm is not None else "unknown GPIO"

    def _validate_pins(self):
        for label, pin in (("TRIG_PIN", self.trig_pin), ("ECHO_PIN", self.echo_pin)):
            if pin is None:
                raise UltrasonicError(
                    "{} is not set. Open config.py and set it to a Robot HAT "
                    'digital port name such as "D2".'.format(label)
                )
            if not isinstance(pin, str):
                raise UltrasonicError(
                    '{} must be a Robot HAT port name string such as "D2", '
                    "got {!r}. (This project no longer takes raw BCM numbers - "
                    "the sensor is addressed through the HAT.)".format(label, pin)
                )
        if self.trig_pin == self.echo_pin:
            raise UltrasonicError(
                "TRIG_PIN and ECHO_PIN are both {!r}. They must be different "
                "Robot HAT ports.".format(self.trig_pin)
            )

    def open(self):
        """Claim the two Robot HAT ports. Raises UltrasonicError on failure."""
        self._validate_pins()

        try:
            from robot_hat import Pin, Ultrasonic
        except ImportError as exc:
            raise UltrasonicError(
                "The SunFounder robot_hat library is not installed ({}).\n"
                "  Install it on the Raspberry Pi with:\n"
                "      cd ~\n"
                "      git clone https://github.com/sunfounder/robot-hat.git -b 2.5.x\n"
                "      cd robot-hat\n"
                "      sudo python3 install.py\n"
                "  Then check it with:\n"
                '      python3 -c "import robot_hat; print(robot_hat.__version__)"'
                .format(exc)
            ) from exc

        try:
            trig = Pin(self.trig_pin)
            echo = Pin(self.echo_pin)
        except Exception as exc:
            raise UltrasonicError(
                "robot_hat could not open ports {!r}/{!r}: {}: {}\n"
                "  Valid digital ports on the Robot HAT are D0-D3.".format(
                    self.trig_pin, self.echo_pin, type(exc).__name__, exc
                )
            ) from exc

        try:
            # `timeout` is accepted by current robot_hat; older builds take
            # only (trig, echo), so fall back rather than failing outright.
            try:
                self._ultrasonic = Ultrasonic(trig, echo, timeout=self.timeout)
            except TypeError:
                self._ultrasonic = Ultrasonic(trig, echo)
        except Exception as exc:
            raise UltrasonicError(
                "robot_hat.Ultrasonic could not be created: {}: {}\n"
                "  Is the Robot HAT seated firmly on all 40 pins, and powered?"
                .format(type(exc).__name__, exc)
            ) from exc

        time.sleep(0.05)      # let the module settle after being claimed
        return self

    # ---------------------------------------------------------- measurement
    def measure_once(self):
        """Fire one ping and return the distance in cm.

        Raises UltrasonicError if robot_hat reports a failure.
        """
        if self._ultrasonic is None:
            raise UltrasonicError("Sensor is not open. Call open() first.")

        try:
            # times=1 because OUR measure() already averages; letting
            # robot_hat retry 10 times internally would stall the thread
            # for up to a third of a second on every failed reading.
            value = self._ultrasonic.read(times=1)
        except Exception as exc:
            raise UltrasonicError(
                "robot_hat read failed: {}: {}".format(type(exc).__name__, exc)
            ) from exc

        return self._interpret(value)

    def _interpret(self, value):
        """Turn a robot_hat return value into cm, or raise UltrasonicError.

        robot_hat signals failure by returning a negative number instead of
        raising, so this is where a fake "-1 cm" reading gets stopped.
        """
        if value is None:
            raise UltrasonicError(
                "robot_hat returned no value for ECHO ({}).".format(self.echo_pin)
            )

        if value == -1:
            raise UltrasonicError(
                "No echo received on ECHO {} ({}). Either nothing is within "
                "range (~4 m), or check that the white ECHO wire is in the "
                "yellow signal pin of the {} port and the yellow TRIG wire is "
                "in the yellow signal pin of the {} port.".format(
                    self.echo_pin, self._bcm_label(self.echo_pin),
                    self.echo_pin, self.trig_pin,
                )
            )

        if value == -2:
            raise UltrasonicError(
                "Echo pulse never ended on ECHO {} - the line stayed HIGH. "
                "Usually a loose cable or a sensor that is not getting power "
                "from the red VCC pin of the {} port.".format(
                    self.echo_pin, self.trig_pin
                )
            )

        if value < 0:
            raise UltrasonicError(
                "robot_hat returned {} for ECHO {}, which is not a real "
                "distance.".format(value, self.echo_pin)
            )

        if value > config.SENSOR_IMPLAUSIBLE_ABOVE_CM:
            # Not "very far away" - the sensor cannot see that far at all.
            # Current robot_hat builds can return a huge number when ECHO is
            # already HIGH as a ping starts. Reporting it would show SAFE for
            # a sensor that is actually faulty.
            raise UltrasonicError(
                "Implausible reading of {:.0f} cm on ECHO {} - beyond anything "
                "this sensor can measure. Usually the ECHO line was already "
                "HIGH when the ping started: check the white ECHO wire in the "
                "{} port and that the sensor has power from the red pin of the "
                "{} port.".format(value, self.echo_pin, self.echo_pin, self.trig_pin)
            )

        return float(value)

    def measure(self):
        """Median of several pings - rejects the occasional wild reading.

        Raises UltrasonicError only if EVERY ping in the burst failed; the
        message is the one from the last failure.
        """
        samples = []
        last_error = None
        for _ in range(max(1, config.SENSOR_SAMPLES_PER_READING)):
            try:
                samples.append(self.measure_once())
            except UltrasonicError as exc:
                last_error = exc

        if not samples:
            raise last_error

        samples.sort()
        return samples[len(samples) // 2]

    # -------------------------------------------------------------- cleanup
    def close(self):
        """Release the sensor. Safe to call more than once."""
        ultrasonic, self._ultrasonic = self._ultrasonic, None
        if ultrasonic is None:
            return

        # robot_hat has gained and lost a Pin.close() across versions, so
        # only call it if this build actually has one.
        for pin in (getattr(ultrasonic, "trig", None), getattr(ultrasonic, "echo", None)):
            closer = getattr(pin, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass


class UltrasonicMonitor(threading.Thread):
    """Reads the sensor continuously on a background thread.

    The camera loop must never block waiting for a ping, so all of the
    waiting happens in here. The main loop just calls snapshot().
    """

    def __init__(self, sensor):
        super().__init__(name="ultrasonic", daemon=True)
        self._sensor = sensor
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        self._distance_cm = None      # last good reading, or None
        self._out_of_range = False    # reading outside the sensor's rated range
        self._error = None            # text of the most recent failure
        self._consecutive_errors = 0
        self._reading_count = 0

    # ------------------------------------------------------------ main loop
    def run(self):
        while not self._stop_event.is_set():
            try:
                distance = self._sensor.measure()
            except UltrasonicError as exc:
                self._record_failure(str(exc))
            except Exception as exc:       # unexpected, but still report it
                self._record_failure("{}: {}".format(type(exc).__name__, exc))
            else:
                with self._lock:
                    self._distance_cm = distance
                    self._out_of_range = (
                        distance > config.SENSOR_MAX_DISTANCE_CM
                        or distance < config.SENSOR_MIN_DISTANCE_CM
                    )
                    self._error = None
                    self._consecutive_errors = 0
                    self._reading_count += 1

            self._stop_event.wait(config.SENSOR_READ_INTERVAL_S)

    def _record_failure(self, message):
        with self._lock:
            self._error = message
            self._consecutive_errors += 1
            # Once the sensor has been failing for a while, drop the last
            # reading. Showing a stale number would be lying about the world.
            if self._consecutive_errors >= config.SENSOR_ERRORS_BEFORE_FAIL:
                self._distance_cm = None

    # ------------------------------------------------------------- read out
    def snapshot(self):
        """Thread-safe copy of the current sensor state.

        Returns a dict with keys:
            distance_cm     float or None
            out_of_range    bool
            error           str or None
            healthy         bool  (False once it has failed repeatedly)
            reading_count   int
        """
        with self._lock:
            healthy = self._consecutive_errors < config.SENSOR_ERRORS_BEFORE_FAIL
            return {
                "distance_cm": self._distance_cm,
                "out_of_range": self._out_of_range,
                "error": self._error,
                "healthy": healthy,
                "reading_count": self._reading_count,
            }

    def stop(self, timeout=2.0):
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout=timeout)
