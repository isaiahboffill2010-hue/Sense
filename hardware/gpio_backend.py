"""
hardware/gpio_backend.py
========================

A very small wrapper around whichever GPIO library is available.

Why this exists
---------------
Raspberry Pi OS has been migrating from the old `RPi.GPIO` library to
`lgpio`. Which one is installed depends on the OS release:

    * lgpio     - present on current Raspberry Pi OS (Bookworm / Trixie),
                  works on Pi 3, 4 and 5.  Preferred.
    * RPi.GPIO  - the classic library, fine on a Pi 3.  Used as a fallback.

Both talk to the REAL GPIO header. This wrapper only chooses between two
real drivers - it never simulates pins. If neither library can be loaded,
open_gpio() raises and tells you exactly what went wrong with each one.

All pin numbers are BCM GPIO numbers.
"""


class GpioError(RuntimeError):
    """Raised when the GPIO hardware cannot be opened or driven."""


# --------------------------------------------------------------------------
# Backend 1: lgpio  (preferred - current Raspberry Pi OS)
# --------------------------------------------------------------------------
class LgpioBackend:
    name = "lgpio"

    def __init__(self, chip_number):
        import lgpio  # imported here so Windows never tries to load it

        self._lgpio = lgpio
        self._handle = lgpio.gpiochip_open(chip_number)
        self._chip_number = chip_number
        self._claimed = []

    @property
    def description(self):
        return "lgpio (/dev/gpiochip{})".format(self._chip_number)

    def setup_output(self, pin, initial=False):
        self._lgpio.gpio_claim_output(self._handle, pin, 1 if initial else 0)
        self._claimed.append(pin)

    def setup_input(self, pin):
        self._lgpio.gpio_claim_input(self._handle, pin)
        self._claimed.append(pin)

    def write(self, pin, value):
        self._lgpio.gpio_write(self._handle, pin, 1 if value else 0)

    def read(self, pin):
        return bool(self._lgpio.gpio_read(self._handle, pin))

    def cleanup(self):
        for pin in self._claimed:
            try:
                self._lgpio.gpio_free(self._handle, pin)
            except Exception:
                pass          # already released; nothing useful to report
        self._claimed = []
        try:
            self._lgpio.gpiochip_close(self._handle)
        except Exception:
            pass


# --------------------------------------------------------------------------
# Backend 2: RPi.GPIO  (classic fallback)
# --------------------------------------------------------------------------
class RPiGpioBackend:
    name = "RPi.GPIO"

    def __init__(self):
        import RPi.GPIO as GPIO  # imported here so Windows never loads it

        self._gpio = GPIO
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)     # BCM numbering, matching config.py

    @property
    def description(self):
        return "RPi.GPIO (BCM numbering)"

    def setup_output(self, pin, initial=False):
        self._gpio.setup(
            pin,
            self._gpio.OUT,
            initial=self._gpio.HIGH if initial else self._gpio.LOW,
        )

    def setup_input(self, pin):
        self._gpio.setup(pin, self._gpio.IN)

    def write(self, pin, value):
        self._gpio.output(pin, bool(value))

    def read(self, pin):
        return bool(self._gpio.input(pin))

    def cleanup(self):
        try:
            self._gpio.cleanup()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Chooser
# --------------------------------------------------------------------------
def open_gpio(chip_number=0):
    """Open the first GPIO backend that actually works.

    Tries lgpio on the requested chip, then lgpio on the other common chip
    numbers, then RPi.GPIO. Raises GpioError listing every failure if none
    of them work.
    """
    problems = []

    chips = [chip_number] + [c for c in (0, 4) if c != chip_number]
    for chip in chips:
        try:
            return LgpioBackend(chip)
        except ImportError as exc:
            problems.append("lgpio: not installed ({})".format(exc))
            break                      # no point retrying other chip numbers
        except Exception as exc:
            problems.append(
                "lgpio on /dev/gpiochip{}: {}: {}".format(
                    chip, type(exc).__name__, exc
                )
            )

    try:
        return RPiGpioBackend()
    except ImportError as exc:
        problems.append("RPi.GPIO: not installed ({})".format(exc))
    except Exception as exc:
        problems.append("RPi.GPIO: {}: {}".format(type(exc).__name__, exc))

    raise GpioError(
        "No usable GPIO backend.\n  "
        + "\n  ".join(problems)
        + "\n  Install one with:  sudo apt install -y python3-lgpio"
        + "\n  You may also need to be in the 'gpio' group:  sudo usermod -aG gpio $USER"
    )
