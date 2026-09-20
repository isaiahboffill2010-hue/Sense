"""
hardware/ultrasonic.py
======================

HC-SR04 ultrasonic distance sensor.

How an HC-SR04 works
--------------------
1. We hold TRIG high for 10 microseconds.
2. The module emits an ultrasonic burst.
3. The module raises ECHO, then lowers it again when the echo returns.
4. The width of that ECHO pulse is the round-trip flight time:

       distance_cm = pulse_seconds * 34300 / 2

   (34300 cm/s is the speed of sound; we divide by 2 because the sound
   travelled out AND back.)

ELECTRICAL SAFETY
-----------------
The ECHO pin of a standard 5V HC-SR04 outputs roughly 5V. Raspberry Pi GPIO
inputs are 3.3V and are not 5V tolerant. Use a voltage divider or a level
shifter on ECHO unless your module already shifts down to 3.3V. See config.py.

Honesty policy
--------------
This module never invents a reading. If no echo comes back you get an
UltrasonicError describing exactly what the pin did - not a fake number.
"""

import threading
import time

import config
from .gpio_backend import GpioError, open_gpio


class UltrasonicError(RuntimeError):
    """Raised when a measurement fails (no echo, stuck pin, bad wiring)."""


def _busy_wait(seconds):
    """Spin for a very short time.

    time.sleep() cannot reliably produce a 10 microsecond delay on Linux, and
    the TRIG pulse must be at least 10us, so we burn a few CPU cycles instead.
    """
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        pass


class UltrasonicSensor:
    """Blocking, single-shot access to one HC-SR04."""

    def __init__(self, trig_pin, echo_pin, chip_number=None):
        self.trig_pin = trig_pin
        self.echo_pin = echo_pin
        self.chip_number = config.GPIO_CHIP if chip_number is None else chip_number
        self._gpio = None

    # ---------------------------------------------------------------- setup
    @staticmethod
    def pins_are_configured():
        """True once TRIG_PIN and ECHO_PIN have been filled in in config.py."""
        return config.TRIG_PIN is not None and config.ECHO_PIN is not None

    def _validate_pins(self):
        for label, pin in (("TRIG_PIN", self.trig_pin), ("ECHO_PIN", self.echo_pin)):
            if pin is None:
                raise UltrasonicError(
                    "{} is not set. Open config.py and set it to the BCM GPIO "
                    "number you actually wired.".format(label)
                )
            if isinstance(pin, bool) or not isinstance(pin, int):
                raise UltrasonicError(
                    "{} must be a whole number (BCM GPIO), got {!r}.".format(label, pin)
                )
            if not 2 <= pin <= 27:
                raise UltrasonicError(
                    "{}={} is not a usable BCM GPIO number. Use 2-27 "
                    "(BCM 0 and 1 are reserved for the HAT EEPROM).".format(label, pin)
                )
        if self.trig_pin == self.echo_pin:
            raise UltrasonicError(
                "TRIG_PIN and ECHO_PIN are both {}. They must be different "
                "pins.".format(self.trig_pin)
            )

    def open(self):
        """Claim the two GPIO pins. Raises UltrasonicError on any problem."""
        self._validate_pins()
        try:
            self._gpio = open_gpio(self.chip_number)
            self._gpio.setup_output(self.trig_pin, initial=False)
            self._gpio.setup_input(self.echo_pin)
        except GpioError as exc:
            raise UltrasonicError(str(exc)) from exc
        except Exception as exc:
            raise UltrasonicError(
                "Could not claim GPIO {} (TRIG) / {} (ECHO): {}: {}".format(
                    self.trig_pin, self.echo_pin, type(exc).__name__, exc
                )
            ) from exc

        # The module needs a moment after power-up before it answers.
        time.sleep(0.05)
        return self

    @property
    def backend_description(self):
        return self._gpio.description if self._gpio else "not opened"

    # ---------------------------------------------------------- measurement
    def measure_once(self):
        """Fire one ping and return the distance in cm.

        Raises UltrasonicError if the sensor does not respond correctly.
        """
        if self._gpio is None:
            raise UltrasonicError("Sensor is not open. Call open() first.")

        gpio = self._gpio
        timeout = config.SENSOR_ECHO_TIMEOUT_S

        # Make sure TRIG is low and the line is quiet before we start.
        gpio.write(self.trig_pin, False)
        time.sleep(config.SENSOR_SETTLE_S)

        # If ECHO is already high, the previous ping never finished, or the
        # pin is mis-wired. Wait briefly for it to clear.
        clear_deadline = time.perf_counter() + timeout
        while gpio.read(self.echo_pin):
            if time.perf_counter() > clear_deadline:
                raise UltrasonicError(
                    "ECHO (BCM {}) is stuck HIGH before the ping. Check the "
                    "ECHO wire, the voltage divider, and the 5V supply to the "
                    "sensor.".format(self.echo_pin)
                )

        # 10 microsecond trigger pulse.
        gpio.write(self.trig_pin, True)
        _busy_wait(0.000010)
        gpio.write(self.trig_pin, False)

        # Wait for ECHO to go HIGH: the start of the flight time.
        rise_deadline = time.perf_counter() + timeout
        while not gpio.read(self.echo_pin):
            if time.perf_counter() > rise_deadline:
                raise UltrasonicError(
                    "No echo received - ECHO (BCM {}) never went HIGH. Check "
                    "VCC/GND, the TRIG wire (BCM {}), and that the pin numbers "
                    "in config.py match your wiring.".format(
                        self.echo_pin, self.trig_pin
                    )
                )
        start = time.perf_counter()

        # Wait for ECHO to go LOW again: the end of the flight time.
        fall_deadline = start + timeout
        while gpio.read(self.echo_pin):
            if time.perf_counter() > fall_deadline:
                raise UltrasonicError(
                    "Echo pulse never ended - ECHO (BCM {}) stayed HIGH for "
                    "more than {:.0f} ms. Usually this means nothing reflected "
                    "the ping, or ECHO is mis-wired.".format(
                        self.echo_pin, timeout * 1000
                    )
                )
        elapsed = time.perf_counter() - start

        return elapsed * config.SPEED_OF_SOUND_CM_S / 2.0

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
            time.sleep(0.010)     # the HC-SR04 needs a gap between pings

        if not samples:
            raise last_error

        samples.sort()
        return samples[len(samples) // 2]

    # -------------------------------------------------------------- cleanup
    def close(self):
        """Release the GPIO pins. Safe to call more than once."""
        if self._gpio is not None:
            try:
                self._gpio.write(self.trig_pin, False)
            except Exception:
                pass
            self._gpio.cleanup()
            self._gpio = None


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
