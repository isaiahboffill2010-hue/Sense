# Sense Phase 6 — Ground, Curb and Walking-Space Awareness Prototype

This is a local experimental perception prototype. It does not authorize movement, estimate distance to a curb, or make navigation decisions. It leaves production Sense and Phases 1–5 unchanged.

## Research and choice

Semantic segmentation is the appropriate long-term approach: Cityscapes explicitly labels `road` and `sidewalk`, while TensorFlow Lite describes semantic segmentation as a per-pixel task. Fast-SCNN is an embedded-oriented research model, but a verified Raspberry Pi 3-compatible TFLite Cityscapes checkpoint was not available in this project. Adding an unverified model beside the existing detector would make both performance and licensing unclear. Phase 6 therefore uses **no model download and no new dependency**: a deliberately conservative OpenCV classical-CV experiment at **1 FPS** while object detection/tracking remain independently rate-limited.

It analyses only the lower image half at 160×120: low/moderate saturation, brightness, local edge density, morphology, Canny, and Hough horizontal-edge candidates. This creates a lower-image **ground-evidence mask**, not a semantic ground truth. `GROUND: VISIBLE` means enough consistent lower-image evidence is present for this experiment; it does not identify a surface, mean flat, or authorize movement. Weak, conflicting, insufficient, or camera-suppressed evidence is `GROUND: UNKNOWN`. The earlier ROAD/SIDEWALK/OTHER_GROUND heuristic labels were removed because indoor flooring can produce the same classical features.

## Space and transition rules

The lower-half mask is divided into equal LEFT/CENTER/RIGHT regions. A region is APPARENTLY OPEN only if at least 55% has ground evidence. It is OCCUPIED only when a **current confirmed** track overlaps at least 20% of that region (minimum 12 pixels) and its bottom box contact is in the lower 35% of the image; otherwise low ground evidence is UNKNOWN. This replaces the earlier overly broad rule where any one-pixel overlap with a box bottom at 55% could mark a region occupied. `APPARENTLY OPEN` is visual evidence only, never a safety statement.

A Hough horizontal candidate needs strength ≥0.55, visible fraction ≥0.60, ground quality ≥0.55, and low conflict before it can enter temporal history; it must occur in two of the latest three reliable analyses before `GROUND TRANSITION: POSSIBLE`. One isolated candidate remains UNKNOWN/NOT OBSERVED. Any Phase 4 non-STABLE or uncertain camera state clears transition history and makes ground category and walking-space UNKNOWN. The system does not claim a curb distance, direction of elevation, or association to ultrasonic range.

## Pi operation

```bash
cd ~/Sense
git pull
sudo systemctl stop sense
source .venv-vision/bin/activate
export ROBOT_HAT_GPIOCHIP=0
python3 -m unittest -v test_prototype_ground_awareness.py
python3 prototype_ground_awareness.py
```

Debug preview (includes masks and tentative tracks; console also prints per-region ground evidence, occupancy-causing confirmed track, transition samples, and camera suppression):

```bash
python3 prototype_ground_awareness.py --debug-ground
```

60-second headless benchmark:

```bash
python3 prototype_ground_awareness.py --benchmark-seconds 60 --no-preview
```

The report includes ground-analysis FPS/latency, object-detection FPS, CPU, RAM, temperature, and frame freshness. Measure these on the real Pi before drawing performance conclusions.
