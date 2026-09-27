# Sense Phase 2: local vision and ultrasonic fusion prototype

`prototype_sensor_fusion.py` is separate from production Sense. It reuses the
Phase 1 LiteRT detector, `hardware.camera.CameraReader`, and the existing
`hardware.ultrasonic.UltrasonicMonitor`; it does not alter `main.py`, pins,
audio, beeps, speech, navigation, Gemini, or cloud services.

The ultrasonic reading is always shown as **FORWARD OBSTACLE**, never as an
object's measured depth. It is the existing median-of-pings filtered reading.
A visual object can only be a possible target when its box overlaps the centre
beam zone by at least 25%, detector confidence meets the configured normal
threshold (default 0.50), and
both the camera result and ultrasonic measurement are fresh. Two or more such
objects are `AMBIGUOUS`. An object outside the beam is not associated.

Defaults: centre zone width 34% of the image, vision freshness 0.75 seconds,
ultrasonic freshness 0.50 seconds. A stale/invalid reading disables only the
association; it never causes a valid ultrasonic obstacle to be discarded.

## Raspberry Pi commands

The Phase 1 virtual environment is reused; there are no new packages.

```bash
cd ~/Sense
git pull
sudo systemctl stop sense
source .venv-vision/bin/activate
export ROBOT_HAT_GPIOCHIP=0
python3 -m unittest -v test_prototype_sensor_fusion.py
python3 prototype_sensor_fusion.py
```

For a headless 60-second measurement:

```bash
python3 prototype_sensor_fusion.py --no-preview --benchmark-seconds 60
```

Use `Q`, Escape, or Ctrl-C to exit. Restore the production service afterward:

```bash
sudo systemctl start sense
```
