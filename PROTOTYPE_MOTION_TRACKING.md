# Sense Phase 3: apparent motion tracking prototype

`prototype_motion_tracking.py` is isolated from production Sense. It uses the
Phase 1 LiteRT detector and the existing background ultrasonic monitor. It adds
no Gemini, audio, navigation, safety decision, camera-motion compensation, or
per-object ultrasonic range claim.

Tracking matches same-class detections using IoU or centre distance (default
120 pixels), greedily one-to-one. History is capped at six observations. A
track remains available through a temporary miss and expires after 1.5 seconds.
Motion is **apparent relative to the camera image**: 35 pixels of consistent
horizontal movement is required; box area must change by at least 25% over
three observations for approaching/receding. High-priority person/vehicle
tracks may show crossing after LEFT → CENTER → RIGHT or reverse.

```bash
cd ~/Sense
git pull
sudo systemctl stop sense
source .venv-vision/bin/activate
export ROBOT_HAT_GPIOCHIP=0
python3 -m unittest -v test_prototype_motion_tracking.py
python3 prototype_motion_tracking.py --benchmark-seconds 0
```

For a 60-second measurement:

```bash
python3 prototype_motion_tracking.py --benchmark-seconds 60
```

Restore Sense afterwards with `sudo systemctl start sense`.
