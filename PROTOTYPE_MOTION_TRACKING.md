# Sense Phase 3: apparent motion tracking prototype

`prototype_motion_tracking.py` is isolated from production Sense. It uses the
Phase 1 LiteRT detector and the existing background ultrasonic monitor. It adds
no Gemini, audio, navigation, safety decision, camera-motion compensation, or
per-object ultrasonic range claim.

New tracks are tentative until two hits arrive within 0.75 seconds. Tracking
matches same-class detections using a combined IoU/current-centre/predicted-
centre/box-size score, greedily one-to-one. History is capped at six
observations. Normal tracks expire after 1.5 seconds; high-priority tracks
remain available for 2 seconds. The default preview only shows currently seen,
confirmed tracks; `--debug-tracks` reveals tentative and LOST internals.

`--benchmark-seconds N` accepts a numeric duration: omit it (or pass `0`) for
continuous preview, and pass `60` for a one-minute benchmark.
Motion is **apparent relative to the camera image**: 35 pixels of consistent
horizontal movement is required; box area must change by at least 25% over
three observations for approaching/receding. High-priority person/vehicle
tracks may show crossing after LEFT → CENTER → RIGHT or reverse.

If the Pi prints a `GLib-GObject-CRITICAL g_object_unref` warning, it is not
emitted by the pure-Python tracker. The only GLib-adjacent pieces here are
Picamera2/libcamera and OpenCV's preview-window backend. This change does not
suppress or alter their cleanup behavior without a reproducible Pi-side source;
record whether the warning appears at startup, during preview, or only on exit.

```bash
cd ~/Sense
git pull
sudo systemctl stop sense
source .venv-vision/bin/activate
export ROBOT_HAT_GPIOCHIP=0
python3 -m unittest -v test_prototype_motion_tracking.py
python3 prototype_motion_tracking.py
```

For a 60-second measurement:

```bash
python3 prototype_motion_tracking.py --benchmark-seconds 60
```

Restore Sense afterwards with `sudo systemctl start sense`.
