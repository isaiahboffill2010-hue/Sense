# Standalone IMU / head-motion prototype

This phase is isolated from the working Sense pipeline. `prototype_imu.py`
does not import or run the camera, SSD MobileNet, Gemini, speech, ultrasonic,
Event Manager, or production `main.py` systems. Nothing in those systems was
changed.

## What can and cannot be identified from the available evidence

SunFounder's official PiDog documentation identifies its board labelled
**6-DOF IMU**, with the four `SCL`, `SDA`, `VCC`, and `GND` connections, as an
**SH3001** three-axis accelerometer plus three-axis gyroscope. The official
module is 3.3 V I2C and uses address **0x36**. SunFounder's PiDog source uses
`pidog.sh3001.Sh3001`, which in turn uses `robot_hat.I2C`. The driver verifies
SH3001 chip ID `0x61`, configures ±2 g acceleration and ±2000 degrees/second
gyro ranges, and reads the six axes together.

Sources inspected:

- [SunFounder 6-DOF IMU hardware page](https://docs.sunfounder.com/projects/pidog/en/latest/hardware/cpn_6dof_imu.html)
- [SunFounder IMU example](https://docs.sunfounder.com/projects/pidog/en/latest/python/py_b10_imu.html)
- [SunFounder PiDog source](https://github.com/sunfounder/pidog), especially
  `pidog/sh3001.py`, `test/imu_test.py`, and `basic_examples/10_imu_read.py`
- [SunFounder robot-hat 2.5.x source](https://github.com/sunfounder/robot-hat/tree/2.5.x),
  especially `robot_hat/i2c.py`

That product-family match is strong, but the **electrical identity of the
connected board is not yet proven**. The scans supplied for this project do
not show `0x36` on an accessible HAT bus. They show:

- bus 0: `0x50`
- bus 2: `0x30`, `0x50`
- bus 11: `0x36` as `UU`, plus `0x50`

Buses 0, 10, and 11 are described as channels of the camera/display I2C mux.
The Raspberry Pi probe has now identified kernel device `10-0036` as the
**OV5647 camera**, owned by driver `ov5647`. It is not the IMU. The prototype
will not detach its driver or force access. The identities of `0x30` and
`0x50` are also not proven, so the prototype never accesses or writes either
address.

The physical RGB board is consistent with SunFounder's separate **11-channel
Light Board**. Official PiDog source controls it with
`pidog.rgb_strip.RGBStrip(addr=0x74, nums=11)`. That driver talks to the LED
controller over I2C and writes complete RGB frames; the light board and IMU
share/pass through the I2C connection but are separate devices. SunFounder's
FAQ expects `0x36` for the IMU and `0x74` for the light board. Since `0x74`
does not appear in the supplied scans, the connected RGB board/control path
is **not yet electrically proven** and LEDs are opt-in.

## Safe hardware identification on the Raspberry Pi

First pull this commit, enter the repository, and run the read-only probe:

```bash
cd ~/blind-navigation-headband
python3 prototype_imu.py --probe
```

The expanded probe reports:

- Python/package locations;
- whether the installed `robot_hat` exports or contains SH3001/RGB support;
- every `/dev/i2c-*` controller and its device-tree route;
- devices already represented in `/sys/bus/i2c/devices` and bound drivers;
- the current GPIO2/GPIO3 pin function;
- active I2C-related boot configuration lines;
- Raspberry Pi HAT EEPROM product metadata.

It does not open an I2C device, probe an address, or read/write a device
register. Save its output, then run the short follow-up commands only if a
field is unavailable:

```bash
readlink -f /sys/class/i2c-dev/i2c-*/device/of_node
pinctrl get 2-3
grep -nEi 'i2c|camera_auto_detect|display_auto_detect' /boot/firmware/config.txt
```

The first maps Linux bus numbers to physical controllers. The second shows
whether header pins GPIO2/GPIO3 are currently assigned to SDA1/SCL1. The last
shows boot settings that can explain why the header controller is absent. All
three commands are read-only.

`i2cdetect` actively probes a bus and can be unsafe for unknown hardware, so
do not repeat broad scans merely to run this prototype. If another scan is
needed, disconnect the unknown modules and reconnect one documented module at
a time. The required result before live IMU use is an unclaimed `0x36` on the
actual Robot HAT I2C bus. The required result before `--leds` is `0x74` on that
same bus. Do not use the `UU` address on bus 11 without first identifying its
kernel driver.

Do not install `pidog` merely because it is absent. SunFounder's official
PiDog repository is currently the source of the matching `sh3001.py` and
`rgb_strip.py` implementations, but those files are separable drivers rather
than capabilities supplied by `robot_hat`. `sh3001.py` depends only on
`robot_hat.I2C`, `robot_hat.fileDB`, and the Python standard library. The RGB
driver depends on `smbus`, NumPy, and the standard library. Once the hardware
is positively located, the preferred next implementation step is to reuse a
pinned, attributed copy or a small project-local adaptation of only the
needed official driver—not to install the full robot-control package. That
decision also needs to retain the upstream GPLv2 licensing terms.

## Guarded startup and bus selection

SunFounder's Robot HAT V5 documentation says both external I2C connectors are
directly wired to Raspberry Pi GPIO2/SDA and GPIO3/SCL. They are the same
electrical bus; neither is routed through the HAT MCU. On a normal Pi 3 device
tree this controller is exposed as `/dev/i2c-1`. Therefore the missing
`/dev/i2c-1` must be explained before treating bus 2 as a substitute. Bus 2
may be a display/HDMI controller and its address scan is not proof of HAT-port
routing.

SunFounder's current `Sh3001` constructor hard-codes bus 1. The existing
adapter can select another bus and has a chip-ID guard, but live initialization
must not be attempted until the expanded probe proves which Linux controller
maps to GPIO2/GPIO3 and the module is visible there.

Only after the Pi probe establishes the correct header bus and a later,
deliberately scoped read-only identity check confirms SH3001 ID `0x61` should
the live prototype be run:

```bash
cd ~/blind-navigation-headband
ROBOT_HAT_GPIOCHIP=0 python3 prototype_imu.py --bus CONFIRMED_BUS --debug
```

`ROBOT_HAT_GPIOCHIP=0` is retained for consistency with this Robot HAT setup,
although the IMU itself is I2C and does not use GPIO. No camera or other Sense
hardware is required. Stop with Ctrl+C. A finite bench run is available with
`--duration 30`.

Do **not** run live initialization yet. In particular, do not point it at bus
2 or either unknown address from the earlier scans.

## Calibration and units

Startup prints `IMU: CALIBRATING`; keep the headband relatively still for the
default two seconds. At 50 Hz, successful samples are averaged to estimate:

- gyro zero/bias independently on X/Y/Z;
- the installed resting acceleration vector, without requiring level mounting.

Calibration requires at least half the expected samples and rejects a period
whose gyro standard deviation exceeds 3 degrees/second on any axis. It then
prints `IMU: READY`. Accelerometer output is in g; gyro output is in
degrees/second using the official driver's configured sensitivities (16384
LSB/g and 16.4 LSB per degree/second).

This short startup calibration corrects gyro zero drift. It is not a factory
scale calibration and does not create absolute heading.

## Axis mapping

The board-to-head mounting orientation cannot be discovered on this Windows
development machine. The defaults are intentionally documented as provisional:

- mapped yaw: sensor Z, positive = turning right;
- mapped pitch: sensor Y, positive = tilting up.

Use `--debug`, hold still, then perform one motion at a time. Note the dominant
gyro axis and its sign for turn right and tilt up. Configure it explicitly:

```bash
python3 prototype_imu.py --bus BUS --debug \
  --yaw-axis z --yaw-sign 1 --pitch-axis y --pitch-sign -1
```

Available axes are `x`, `y`, and `z`; signs are `1` or `-1`. The yaw and pitch
axes must differ. A 6-DOF gyro measures **turning rate**, not the absolute
direction the head faces. Without a magnetometer, yaw integration drifts and
this prototype makes no compass/absolute-heading claim.

## Classification and filtering

`HeadMotionClassifier` consumes `ImuSample` and returns an importable
`ImuObservation` containing timestamp, all six axes, state, confidence, and an
optional read error. Its states are `STABLE`, `TURNING LEFT`, `TURNING RIGHT`,
`TILTING UP`, `TILTING DOWN`, `MOVING`, and `UNCERTAIN`.

The conservative defaults are:

- 50 Hz requested sampling;
- exponential gyro smoothing, alpha 0.35;
- 22 degrees/second entry dead zone;
- 12 degrees/second exit threshold (amplitude hysteresis);
- three consecutive samples before a state transition;
- two consecutive good samples to recover from `UNCERTAIN`;
- 0.18 g change from the installed resting vector for generic `MOVING`;
- dominant mapped yaw/pitch must exceed the other by 15 percent;
- rotation on the remaining axis or non-dominant/combined movement is `MOVING`.

A failed sample immediately produces `UNCERTAIN`, never synthetic zeros or
fabricated motion. Temporary failures are counted and the loop continues.
Normal output changes only when the state changes; `--debug` adds readings at
5 Hz, not every 50 Hz sample.

## Optional RGB development display

LEDs are disabled by default. Enable them only after the Pi shows the separate
light board at `0x74` on the selected bus:

```bash
ROBOT_HAT_GPIOCHIP=0 python3 prototype_imu.py --bus BUS --leds
```

Static, low-update patterns are written only when motion state changes:

- stable: three green center LEDs;
- turn left/right: four blue LEDs at the corresponding end;
- tilt up: alternating cyan, starting with the first LED;
- tilt down: alternating amber, starting with the second LED;
- moving: all magenta;
- uncertain: all dim red.

Physical LED order is not yet verified. Use `--led-reverse` if left/right are
backwards after a bench test. Any LED import, connection, initialization, or
runtime write failure disables LEDs while IMU processing continues. `--no-leds`
is accepted as an explicit form of the default.

## Performance reporting

On exit, the prototype reports successful samples, achieved acquisition rate,
average hardware-read latency, average classifier processing latency,
read-error count, LED update count/rate, and this process's average CPU
percentage. The loop sleeps to a target 50 Hz and rejects rates above 200 Hz.
Actual Raspberry Pi 3 performance is not claimed until the Pi test is run.

## Automated tests

The tests use no hardware and mock LED failure. They cover gyro bias and rest
reference calibration, movement during calibration, stable/dead-zone behavior,
left/right rotation, up/down tilt, generic motion, configurable mapping,
smoothing, noisy samples, entry/exit hysteresis, missing samples, recovery,
and non-fatal LED failure.

Run just this prototype's tests:

```bash
python3 -m unittest -v test_prototype_imu.py
```

Run the complete repository suite:

```bash
python3 -m unittest discover -v
```

## Later camera-motion integration (not implemented)

A future adapter can sample `ImuObservation` alongside
`prototype_camera_motion.py` and compare mapped yaw/pitch rate with global
optical-flow direction. It should use timestamps and freshness limits rather
than terminal text. No such fusion, camera import, or production-system change
is part of this commit.

## Known limitations and remaining Pi checks

- The connected IMU is not electrically verified until chip ID `0x61` is read
  from an unclaimed `0x36` on the HAT bus.
- The supplied scan conflicts with the official address evidence; `0x30` and
  `0x50` remain deliberately unidentified and untouched.
- The mux-owned `0x36` is confirmed as the OV5647 camera and is excluded.
- The missing GPIO2/GPIO3 header bus must be explained from device-tree,
  pin-function, and boot-configuration evidence before any sensor access.
- The RGB board is not verified until `0x74` appears. LED ordering needs a
  physical visual check.
- Axis/sign mapping needs the four-motion wearing test described above.
- Gyro bias changes with temperature; long-running use may need safe stationary
  re-zeroing later.
- Accelerometer change detects movement/gravity change but does not provide
  absolute yaw. There is no evidence of a magnetometer.
- Sample rate, read latency, CPU usage, and error rate require the real Pi run.
