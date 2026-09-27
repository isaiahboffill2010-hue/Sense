# Sense Phase 4: camera-motion awareness prototype

`prototype_camera_motion.py` is isolated from production Sense. It reuses the
Phase 3.1 tracker unchanged for IDs and the existing independent ultrasonic
monitor. It adds no cloud calls, speech, navigation, IMU, SLAM, depth claim, or
per-object ultrasonic distance.

It runs sparse Shi-Tomasi features and pyramidal Lucas-Kanade flow only between
consecutive newest detector frames, at half camera resolution (normally
320x240). This avoids a second frame queue and keeps the feature limit at 80.
The global image displacement is a robust median with 5-pixel inlier filtering;
at least eight valid features are required. The default masks high-priority
person/vehicle boxes before choosing features, so moving people are less likely
to dominate background motion. Use `--no-object-mask` only to compare behavior.

Scene displacement left maps to `CAMERA: TURNING RIGHT`; displacement right maps
to `TURNING LEFT`. A 4-pixel dead zone and three-estimate median smoother reduce
jitter. If features are insufficient or disagree, state is `MOTION UNCERTAIN`.

Corrected horizontal object motion is `object box dx - global scene dx`.
Residuals within 20 pixels are `LOW MOTION`. This is still monocular apparent
motion: head/camera movement is not true orientation, and approaching/receding
area changes are not compensated.

```bash
cd ~/Sense
git pull
sudo systemctl stop sense
source .venv-vision/bin/activate
export ROBOT_HAT_GPIOCHIP=0
python3 -m unittest -v test_prototype_camera_motion.py
python3 prototype_camera_motion.py
```

Benchmark for 60 seconds:

```bash
python3 prototype_camera_motion.py --benchmark-seconds 60
```

`--show-features` visualizes tracked feature points. The pre-existing
`GLib-GObject-CRITICAL g_object_unref` warning is not attributed to this pure
calculation; do not suppress it without a reproducible Pi-side source.
