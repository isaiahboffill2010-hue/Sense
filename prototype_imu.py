"""Standalone, hardware-guarded IMU/head-motion prototype for Sense.

This module deliberately has no imports from the camera, vision, speech,
ultrasonic, Event Manager, or production entry point.  Its classifier and
data classes are suitable for later import by camera-motion fusion code.

The hardware adapter uses SunFounder's ``pidog.sh3001.Sh3001`` implementation.
It performs a read-only chip-ID check before asking that driver to initialise
the sensor, so an unrelated I2C device is never written to by this prototype.
"""

from __future__ import annotations

import argparse
import dataclasses
import enum
import math
import os
import platform
import pkgutil
import shutil
import statistics
import subprocess
import sys
import time
from collections.abc import Iterable, Sequence
from typing import Protocol


SH3001_ADDRESS = 0x36
SH3001_CHIP_ID = 0x61
RGB_BOARD_ADDRESS = 0x74
ACC_LSB_PER_G = 16384.0
GYRO_LSB_PER_DPS = 16.4


class ImuError(RuntimeError):
    """A real IMU sample could not be obtained."""


class MotionState(str, enum.Enum):
    STABLE = "STABLE"
    TURNING_LEFT = "TURNING LEFT"
    TURNING_RIGHT = "TURNING RIGHT"
    TILTING_UP = "TILTING UP"
    TILTING_DOWN = "TILTING DOWN"
    MOVING = "MOVING"
    UNCERTAIN = "UNCERTAIN"


@dataclasses.dataclass(frozen=True)
class ImuSample:
    timestamp: float
    accel_x: float
    accel_y: float
    accel_z: float
    gyro_x: float
    gyro_y: float
    gyro_z: float

    @property
    def accel(self) -> tuple[float, float, float]:
        return self.accel_x, self.accel_y, self.accel_z

    @property
    def gyro(self) -> tuple[float, float, float]:
        return self.gyro_x, self.gyro_y, self.gyro_z


@dataclasses.dataclass(frozen=True)
class ImuObservation:
    timestamp: float
    gyro_x: float | None
    gyro_y: float | None
    gyro_z: float | None
    accel_x: float | None
    accel_y: float | None
    accel_z: float | None
    motion_state: MotionState
    confidence: float
    read_error: str | None = None


@dataclasses.dataclass(frozen=True)
class Calibration:
    gyro_bias: tuple[float, float, float]
    accel_reference: tuple[float, float, float]
    samples: int


@dataclasses.dataclass(frozen=True)
class AxisMapping:
    """Map sensor gyro axes to head motion.

    A positive mapped yaw rate means TURNING RIGHT.  A positive mapped pitch
    rate means TILTING UP.  The defaults are only a conventional starting
    point; use the CLI axis/sign options after the physical orientation test.
    """

    yaw_axis: str = "z"
    yaw_sign: int = 1
    pitch_axis: str = "y"
    pitch_sign: int = 1

    def __post_init__(self) -> None:
        if self.yaw_axis not in "xyz" or self.pitch_axis not in "xyz":
            raise ValueError("axis must be x, y, or z")
        if self.yaw_axis == self.pitch_axis:
            raise ValueError("yaw and pitch axes must be different")
        if self.yaw_sign not in (-1, 1) or self.pitch_sign not in (-1, 1):
            raise ValueError("axis signs must be -1 or +1")


class ImuDevice(Protocol):
    def read(self) -> ImuSample: ...
    def close(self) -> None: ...


def calibrate_samples(
    samples: Iterable[ImuSample],
    minimum: int = 10,
    maximum_gyro_stddev: float = 3.0,
) -> Calibration:
    values = list(samples)
    if len(values) < minimum:
        raise ImuError(
            f"calibration needs at least {minimum} valid samples; got {len(values)}"
        )
    gyros = list(zip(*(sample.gyro for sample in values)))
    accels = list(zip(*(sample.accel for sample in values)))
    gyro_noise = tuple(statistics.pstdev(axis) for axis in gyros)
    if max(gyro_noise) > maximum_gyro_stddev:
        raise ImuError(
            "device moved during calibration (gyro standard deviations: {})".format(
                ", ".join(f"{value:.2f}" for value in gyro_noise)
            )
        )
    return Calibration(
        gyro_bias=tuple(statistics.fmean(axis) for axis in gyros),
        accel_reference=tuple(statistics.fmean(axis) for axis in accels),
        samples=len(values),
    )


class HeadMotionClassifier:
    """Low-cost EMA filtering plus dead-zone and temporal hysteresis."""

    def __init__(
        self,
        calibration: Calibration,
        mapping: AxisMapping | None = None,
        smoothing_alpha: float = 0.35,
        enter_rate_dps: float = 22.0,
        exit_rate_dps: float = 12.0,
        acceleration_delta_g: float = 0.18,
        confirm_samples: int = 3,
        recovery_samples: int = 2,
    ) -> None:
        if not 0 < smoothing_alpha <= 1:
            raise ValueError("smoothing_alpha must be in (0, 1]")
        if exit_rate_dps >= enter_rate_dps:
            raise ValueError("exit_rate_dps must be below enter_rate_dps")
        self.calibration = calibration
        self.mapping = mapping or AxisMapping()
        self.alpha = smoothing_alpha
        self.enter_rate = enter_rate_dps
        self.exit_rate = exit_rate_dps
        self.acceleration_delta = acceleration_delta_g
        self.confirm_samples = max(1, confirm_samples)
        self.recovery_samples = max(1, recovery_samples)
        self.state = MotionState.STABLE
        self._filtered_gyro = [0.0, 0.0, 0.0]
        self._candidate = MotionState.STABLE
        self._candidate_count = 0
        self._have_filter = False

    @staticmethod
    def _axis(values: Sequence[float], name: str) -> float:
        return values["xyz".index(name)]

    def missing(self, timestamp: float | None = None, error: str = "IMU read failed") -> ImuObservation:
        self.state = MotionState.UNCERTAIN
        self._candidate = MotionState.UNCERTAIN
        self._candidate_count = 0
        return ImuObservation(
            timestamp=time.monotonic() if timestamp is None else timestamp,
            gyro_x=None, gyro_y=None, gyro_z=None,
            accel_x=None, accel_y=None, accel_z=None,
            motion_state=MotionState.UNCERTAIN,
            confidence=0.0,
            read_error=error,
        )

    def update(self, sample: ImuSample) -> ImuObservation:
        corrected = [
            sample.gyro[i] - self.calibration.gyro_bias[i] for i in range(3)
        ]
        if not self._have_filter:
            self._filtered_gyro[:] = corrected
            self._have_filter = True
        else:
            for i, value in enumerate(corrected):
                self._filtered_gyro[i] += self.alpha * (value - self._filtered_gyro[i])

        desired, strength = self._desired_state(sample.accel)
        required = self.recovery_samples if self.state is MotionState.UNCERTAIN else self.confirm_samples
        if desired == self.state:
            self._candidate = desired
            self._candidate_count = 0
        elif desired == self._candidate:
            self._candidate_count += 1
            if self._candidate_count >= required:
                self.state = desired
                self._candidate_count = 0
        else:
            self._candidate = desired
            self._candidate_count = 1
            if self._candidate_count >= required:
                self.state = desired
                self._candidate_count = 0

        confidence = min(1.0, max(0.0, strength / max(self.enter_rate, 1.0)))
        if self.state is MotionState.STABLE:
            confidence = min(1.0, max(0.0, 1.0 - strength / self.enter_rate))
        return ImuObservation(
            timestamp=sample.timestamp,
            gyro_x=self._filtered_gyro[0],
            gyro_y=self._filtered_gyro[1],
            gyro_z=self._filtered_gyro[2],
            accel_x=sample.accel_x,
            accel_y=sample.accel_y,
            accel_z=sample.accel_z,
            motion_state=self.state,
            confidence=confidence,
        )

    def _desired_state(self, accel: Sequence[float]) -> tuple[MotionState, float]:
        yaw = self._axis(self._filtered_gyro, self.mapping.yaw_axis) * self.mapping.yaw_sign
        pitch = self._axis(self._filtered_gyro, self.mapping.pitch_axis) * self.mapping.pitch_sign
        used = {self.mapping.yaw_axis, self.mapping.pitch_axis}
        other = next(axis for axis in "xyz" if axis not in used)
        roll = self._axis(self._filtered_gyro, other)
        magnitude = math.sqrt(sum(value * value for value in self._filtered_gyro))
        threshold = self.exit_rate if self.state is not MotionState.STABLE else self.enter_rate

        if abs(yaw) >= threshold and abs(yaw) >= abs(pitch) * 1.15:
            return (
                MotionState.TURNING_RIGHT if yaw > 0 else MotionState.TURNING_LEFT,
                abs(yaw),
            )
        if abs(pitch) >= threshold and abs(pitch) >= abs(yaw) * 1.15:
            return (
                MotionState.TILTING_UP if pitch > 0 else MotionState.TILTING_DOWN,
                abs(pitch),
            )
        accel_change = math.sqrt(sum(
            (accel[i] - self.calibration.accel_reference[i]) ** 2 for i in range(3)
        ))
        if magnitude >= threshold or abs(roll) >= threshold or accel_change >= self.acceleration_delta:
            return MotionState.MOVING, max(magnitude, accel_change * self.enter_rate)
        return MotionState.STABLE, magnitude


class SunFounderSh3001:
    """Guarded adapter around SunFounder's official PiDog SH3001 driver."""

    def __init__(self, bus: int = 1):
        self.bus = bus
        self._imu = None

    def open(self) -> "SunFounderSh3001":
        try:
            from pidog.sh3001 import Sh3001
            from robot_hat import I2C
        except ImportError as exc:
            raise ImuError(
                "SunFounder's SH3001 driver is unavailable. Do not install or "
                "initialize another package until --probe has positively located "
                f"the hardware ({exc})."
            ) from exc
        imu = None
        try:
            imu = Sh3001.__new__(Sh3001)
            I2C.__init__(imu, address=SH3001_ADDRESS, bus=self.bus)
            chip_id = imu.mem_read(1, imu.SH3001_CHIP_ID)
        except Exception as exc:
            if imu is not None:
                self._close_driver(imu)
            raise ImuError(
                f"cannot read SH3001 candidate at /dev/i2c-{self.bus} address "
                f"0x{SH3001_ADDRESS:02x}: {type(exc).__name__}: {exc}"
            ) from exc
        if not chip_id or list(chip_id) != [SH3001_CHIP_ID]:
            self._close_driver(imu)
            shown = "no response" if not chip_id else repr(list(chip_id))
            raise ImuError(
                f"device at bus {self.bus}, address 0x{SH3001_ADDRESS:02x} returned "
                f"chip ID {shown}, expected [0x{SH3001_CHIP_ID:02x}]. Refusing writes."
            )
        try:
            if not imu.sh3001_init():
                raise ImuError("SunFounder SH3001 initialization rejected the chip ID")
        except Exception as exc:
            self._close_driver(imu)
            if isinstance(exc, ImuError):
                raise
            raise ImuError(f"SH3001 initialization failed: {type(exc).__name__}: {exc}") from exc
        self._imu = imu
        return self

    def read(self) -> ImuSample:
        if self._imu is None:
            raise ImuError("IMU is not open")
        data = self._imu._sh3001_getimudata()
        if data is False or not data:
            raise ImuError("SunFounder SH3001 driver returned no sample")
        accel_raw, gyro_raw = data
        return ImuSample(
            timestamp=time.monotonic(),
            accel_x=accel_raw[0] / ACC_LSB_PER_G,
            accel_y=accel_raw[1] / ACC_LSB_PER_G,
            accel_z=accel_raw[2] / ACC_LSB_PER_G,
            gyro_x=gyro_raw[0] / GYRO_LSB_PER_DPS,
            gyro_y=gyro_raw[1] / GYRO_LSB_PER_DPS,
            gyro_z=gyro_raw[2] / GYRO_LSB_PER_DPS,
        )

    @staticmethod
    def _close_driver(driver: object) -> None:
        bus = getattr(driver, "_smbus", None)
        close = getattr(bus, "close", None)
        if callable(close):
            close()

    def close(self) -> None:
        imu, self._imu = self._imu, None
        if imu is not None:
            self._close_driver(imu)


class SunFounderRgbBoard:
    """Optional 11-channel PiDog light-board visualization."""

    COLORS = {
        MotionState.STABLE: (0, 40, 0),
        MotionState.TURNING_LEFT: (0, 0, 100),
        MotionState.TURNING_RIGHT: (0, 0, 100),
        MotionState.TILTING_UP: (0, 80, 80),
        MotionState.TILTING_DOWN: (90, 45, 0),
        MotionState.MOVING: (70, 0, 70),
        MotionState.UNCERTAIN: (50, 0, 0),
    }

    def __init__(self, bus: int = 1, reverse: bool = False):
        self.bus = bus
        self.reverse = reverse
        self._strip = None

    def open(self) -> "SunFounderRgbBoard":
        try:
            import pidog.rgb_strip as rgb_module
        except ImportError as exc:
            raise ImuError(f"SunFounder pidog RGBStrip driver is unavailable: {exc}") from exc

        # Upstream RGBStrip hard-codes SMBus(1). Redirect only construction so
        # its official register implementation can be used on a selected bus.
        original_smbus = rgb_module.SMBus
        rgb_module.SMBus = lambda _ignored: original_smbus(self.bus)
        try:
            self._strip = rgb_module.RGBStrip(RGB_BOARD_ADDRESS, 11)
        finally:
            rgb_module.SMBus = original_smbus
        return self

    def update(self, state: MotionState) -> None:
        color = self.COLORS[state]
        off = (0, 0, 0)
        if state is MotionState.STABLE:
            pixels = [off] * 4 + [color] * 3 + [off] * 4
        elif state is MotionState.TURNING_LEFT:
            pixels = [color] * 4 + [off] * 7
        elif state is MotionState.TURNING_RIGHT:
            pixels = [off] * 7 + [color] * 4
        elif state is MotionState.TILTING_UP:
            pixels = [color if i % 2 == 0 else off for i in range(11)]
        elif state is MotionState.TILTING_DOWN:
            pixels = [off if i % 2 == 0 else color for i in range(11)]
        else:
            pixels = [color] * 11
        if self.reverse:
            pixels.reverse()
        self._strip.display([list(pixel) for pixel in pixels])

    def close(self) -> None:
        strip, self._strip = self._strip, None
        if strip is not None:
            strip.close()
            bus_close = getattr(getattr(strip, "bus", None), "close", None)
            if callable(bus_close):
                bus_close()


class SafeLedVisualizer:
    """Make every LED failure non-fatal and disable further updates."""

    def __init__(self, device: object | None):
        self.device = device
        self.error: str | None = None
        self.updates = 0

    def update(self, state: MotionState) -> None:
        if self.device is None:
            return
        try:
            self.device.update(state)
            self.updates += 1
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            print(f"RGB LED: DISABLED AFTER ERROR ({self.error})", file=sys.stderr, flush=True)
            self.close()

    def close(self) -> None:
        device, self.device = self.device, None
        if device is not None:
            try:
                device.close()
            except Exception:
                pass


def collect_calibration(device: ImuDevice, seconds: float, rate_hz: float) -> Calibration:
    samples: list[ImuSample] = []
    deadline = time.monotonic() + seconds
    interval = 1.0 / rate_hz
    while time.monotonic() < deadline:
        started = time.monotonic()
        try:
            samples.append(device.read())
        except ImuError:
            pass
        remaining = interval - (time.monotonic() - started)
        if remaining > 0:
            time.sleep(remaining)
    return calibrate_samples(samples, minimum=max(10, int(seconds * rate_hz * 0.5)))


def _read_probe_text(path: str) -> str | None:
    """Read a small procfs/sysfs/config value without changing system state."""
    try:
        with open(path, "rb") as source:
            return source.read(16_384).replace(b"\x00", b"").decode(
                "utf-8", errors="replace"
            ).strip()
    except OSError:
        return None


def _device_tree_path(path: str) -> str:
    resolved = os.path.realpath(path)
    marker = os.path.join("firmware", "devicetree", "base")
    if marker in resolved:
        return resolved.split(marker, 1)[1].replace(os.sep, "/") or "/"
    return resolved


def _robot_hat_sensor_evidence(module: object) -> tuple[list[str], list[str]]:
    """Inspect installed Python source names/text; never import submodules."""
    package_paths = list(getattr(module, "__path__", []))
    module_names = sorted(item.name for item in pkgutil.iter_modules(package_paths))
    source_hits: list[str] = []
    needles = ("sh3001", "class imu", "rgbstrip", "rgb_strip", "0x74")
    for package_path in package_paths:
        try:
            filenames = sorted(os.listdir(package_path))
        except OSError:
            continue
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            path = os.path.join(package_path, filename)
            text = _read_probe_text(path)
            if text is None:
                continue
            lowered = text.lower()
            matches = sorted(needle for needle in needles if needle in lowered)
            if matches:
                source_hits.append(f"{filename}: {', '.join(matches)}")
    return module_names, source_hits


def probe_environment() -> int:
    print(f"Platform: {platform.platform()}")
    print(f"Python: {sys.version.split()[0]} ({sys.executable})")
    robot_hat_module = None
    for module_name in ("robot_hat", "pidog"):
        try:
            module = __import__(module_name)
            print(f"{module_name}: version={getattr(module, '__version__', 'unknown')} file={module.__file__}")
            if module_name == "robot_hat":
                robot_hat_module = module
        except ImportError as exc:
            print(f"{module_name}: NOT INSTALLED ({exc})")
    if robot_hat_module is not None:
        module_names, source_hits = _robot_hat_sensor_evidence(robot_hat_module)
        candidates = [
            name for name in module_names
            if any(token in name.lower() for token in ("imu", "sh3001", "rgb", "strip"))
        ]
        print(
            "robot_hat sensor-named modules: "
            + (", ".join(candidates) if candidates else "none")
        )
        print(
            "robot_hat exports Sh3001/RGBStrip: {}/{}".format(
                hasattr(robot_hat_module, "Sh3001"),
                hasattr(robot_hat_module, "RGBStrip"),
            )
        )
        print("robot_hat source evidence for SH3001/RGB board:")
        if source_hits:
            for hit in source_hits:
                print(f"  {hit}")
        else:
            print("  none")

    dev = "/dev"
    buses = []
    if os.path.isdir(dev):
        buses = sorted(name for name in os.listdir(dev) if name.startswith("i2c-"))
    print("I2C device nodes: " + (", ".join(buses) if buses else "none"))
    if buses:
        print("I2C controller routes (device-tree paths):")
        for bus_name in buses:
            class_path = os.path.join("/sys/class/i2c-dev", bus_name, "device")
            of_node = os.path.join(class_path, "of_node")
            controller_name = _read_probe_text(os.path.join(class_path, "name"))
            route = _device_tree_path(of_node) if os.path.exists(of_node) else "no of_node"
            print(f"  {bus_name}: {controller_name or 'unknown'} -> {route}")

    sysfs = "/sys/bus/i2c/devices"
    if os.path.isdir(sysfs):
        print("Kernel I2C devices:")
        for name in sorted(os.listdir(sysfs)):
            name_path = os.path.join(sysfs, name, "name")
            if not os.path.isfile(name_path):
                continue
            try:
                with open(name_path, encoding="utf-8") as name_file:
                    device_name = name_file.read().strip()
            except OSError:
                device_name = "unknown"
            driver_path = os.path.join(sysfs, name, "driver")
            driver = os.path.basename(os.path.realpath(driver_path)) if os.path.exists(driver_path) else "unbound"
            print(f"  {name}: {device_name} (driver={driver})")

    print("GPIO2/GPIO3 function (the Robot HAT external I2C wires):")
    pinctrl = shutil.which("pinctrl")
    raspi_gpio = shutil.which("raspi-gpio")
    command = [pinctrl, "get", "2-3"] if pinctrl else (
        [raspi_gpio, "get", "2-3"] if raspi_gpio else None
    )
    if command:
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=5, check=False
            )
            output = (result.stdout or result.stderr).strip()
            print("  " + (output.replace("\n", "\n  ") if output else "no output"))
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"  unavailable ({type(exc).__name__}: {exc})")
    else:
        print("  pinctrl/raspi-gpio command not installed")

    print("Boot configuration lines relevant to I2C routing:")
    config_found = False
    for config_path in ("/boot/firmware/config.txt", "/boot/config.txt"):
        config_text = _read_probe_text(config_path)
        if config_text is None:
            continue
        config_found = True
        print(f"  [{config_path}]")
        relevant = [
            f"{line_number}: {line.strip()}"
            for line_number, line in enumerate(config_text.splitlines(), 1)
            if any(token in line.lower() for token in ("i2c", "camera_auto_detect", "display_auto_detect"))
        ]
        if relevant:
            for line in relevant:
                print(f"    {line}")
        else:
            print("    no matching lines")
    if not config_found:
        print("  no readable config.txt found")

    print("Raspberry Pi HAT EEPROM identity:")
    hat_found = False
    for field in ("product", "product_id", "product_ver", "vendor", "uuid"):
        value = _read_probe_text(os.path.join("/proc/device-tree/hat", field))
        if value is not None:
            hat_found = True
            print(f"  {field}: {value}")
    if not hat_found:
        print("  no /proc/device-tree/hat metadata")
    print("Expected only (not proof of connection): SH3001=0x36, RGB board=0x74")
    print("No I2C address was probed and no register was read or written by this probe.")
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Standalone Sense SH3001 head-motion prototype")
    parser.add_argument("--debug", action="store_true", help="print filtered readings at 5 Hz")
    parser.add_argument("--probe", action="store_true", help="report software/sysfs evidence and exit")
    parser.add_argument("--bus", type=int, default=1, help="confirmed I2C bus number (default: 1)")
    parser.add_argument("--rate", type=float, default=50.0, help="sampling rate in Hz (default: 50)")
    parser.add_argument("--calibration-seconds", type=float, default=2.0)
    parser.add_argument("--yaw-axis", choices=tuple("xyz"), default="z")
    parser.add_argument("--yaw-sign", choices=(-1, 1), default=1, type=int)
    parser.add_argument("--pitch-axis", choices=tuple("xyz"), default="y")
    parser.add_argument("--pitch-sign", choices=(-1, 1), default=1, type=int)
    parser.add_argument("--leds", action="store_true", help="enable optional 11-channel RGB board")
    parser.add_argument("--no-leds", action="store_true", help="explicitly disable LEDs (the default)")
    parser.add_argument("--led-reverse", action="store_true", help="reverse physical LED ordering")
    parser.add_argument("--duration", type=float, default=0.0, help="stop after N seconds; 0 runs until Ctrl+C")
    args = parser.parse_args(argv)
    if args.rate <= 0 or args.rate > 200:
        parser.error("--rate must be in (0, 200]")
    if args.calibration_seconds <= 0:
        parser.error("--calibration-seconds must be positive")
    if args.yaw_axis == args.pitch_axis:
        parser.error("--yaw-axis and --pitch-axis must differ")
    if args.leds and args.no_leds:
        parser.error("choose either --leds or --no-leds")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.probe:
        return probe_environment()

    mapping = AxisMapping(args.yaw_axis, args.yaw_sign, args.pitch_axis, args.pitch_sign)
    imu = SunFounderSh3001(args.bus)
    led = SafeLedVisualizer(None)
    samples = errors = 0
    read_latencies: list[float] = []
    processing_latencies: list[float] = []
    started_wall = time.monotonic()
    started_cpu = time.process_time()
    try:
        imu.open()
        print("IMU: CALIBRATING (hold the headband still)", flush=True)
        calibration = collect_calibration(imu, args.calibration_seconds, args.rate)
        classifier = HeadMotionClassifier(calibration, mapping)
        print(
            "IMU: READY | bus={} address=0x{:02x} | yaw={}*{} pitch={}*{}".format(
                args.bus, SH3001_ADDRESS, args.yaw_sign, args.yaw_axis,
                args.pitch_sign, args.pitch_axis,
            ),
            flush=True,
        )
        if args.leds:
            try:
                led = SafeLedVisualizer(SunFounderRgbBoard(args.bus, args.led_reverse).open())
                print(f"RGB LED: READY | bus={args.bus} address=0x{RGB_BOARD_ADDRESS:02x}", flush=True)
            except Exception as exc:
                print(f"RGB LED: DISABLED ({type(exc).__name__}: {exc})", file=sys.stderr, flush=True)

        # Performance counters describe the acquisition loop, not startup or
        # the intentionally slow calibration period.
        started_wall = time.monotonic()
        started_cpu = time.process_time()

        last_state: MotionState | None = None
        next_debug = 0.0
        interval = 1.0 / args.rate
        run_deadline = time.monotonic() + args.duration if args.duration else math.inf
        while time.monotonic() < run_deadline:
            cycle_started = time.monotonic()
            try:
                sample = imu.read()
                read_finished = time.monotonic()
                read_latencies.append((read_finished - cycle_started) * 1000.0)
                observation = classifier.update(sample)
                processing_latencies.append((time.monotonic() - read_finished) * 1000.0)
                samples += 1
            except Exception as exc:
                errors += 1
                observation = classifier.missing(error=f"{type(exc).__name__}: {exc}")

            if observation.motion_state != last_state:
                print(f"HEAD MOTION: {observation.motion_state.value}", flush=True)
                led.update(observation.motion_state)
                last_state = observation.motion_state
            now = time.monotonic()
            if args.debug and now >= next_debug:
                next_debug = now + 0.2
                print(
                    "IMU DATA: accel={} gyro(filtered)={} confidence={:.2f} errors={}".format(
                        tuple(None if value is None else round(value, 3) for value in
                              (observation.accel_x, observation.accel_y, observation.accel_z)),
                        tuple(None if value is None else round(value, 2) for value in
                              (observation.gyro_x, observation.gyro_y, observation.gyro_z)),
                        observation.confidence, errors,
                    ),
                    flush=True,
                )
            remaining = interval - (time.monotonic() - cycle_started)
            if remaining > 0:
                time.sleep(remaining)
    except KeyboardInterrupt:
        pass
    except ImuError as exc:
        print(f"IMU FAILED: {exc}", file=sys.stderr, flush=True)
        return 2
    finally:
        imu.close()
        led.close()
        elapsed = max(1e-9, time.monotonic() - started_wall)
        cpu = 100.0 * (time.process_time() - started_cpu) / elapsed
        average_read = statistics.fmean(read_latencies) if read_latencies else 0.0
        average_processing = (
            statistics.fmean(processing_latencies) if processing_latencies else 0.0
        )
        print(
            "IMU SUMMARY: samples={} rate={:.1f} Hz avg_read={:.2f} ms "
            "avg_process={:.3f} ms errors={} LED_updates={} ({:.2f}/s) "
            "process_CPU={:.1f}%".format(
                samples, samples / elapsed, average_read, average_processing,
                errors, led.updates, led.updates / elapsed, cpu
            ),
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
