# Sense Phase 6 — Ground, Curb and Walking-Space Awareness Prototype

This is a local experimental perception prototype. It does not authorize movement, estimate distance to a curb, or make navigation decisions. It leaves production Sense and Phases 1–5 unchanged.

## Research and choice

Semantic segmentation is the appropriate long-term approach: Cityscapes explicitly labels `road` and `sidewalk`, while TensorFlow Lite describes semantic segmentation as a per-pixel task. Fast-SCNN is an embedded-oriented research model, but a verified Raspberry Pi 3-compatible TFLite Cityscapes checkpoint was not available in this project. Adding an unverified model beside the existing detector would make both performance and licensing unclear. Phase 6 therefore uses **no model download and no new dependency**: a deliberately conservative OpenCV classical-CV experiment at **1 FPS** while object detection/tracking remain independently rate-limited.

It analyses only the lower image half at 160×120: low/moderate saturation, brightness, local edge density, morphology, Canny, and Hough horizontal-edge candidates. This creates a *pavement-like evidence mask*, not a semantic ground truth. Strong non-conflicting heuristic evidence can label SIDEWALK or ROAD; weak/conflicting/insufficient evidence is UNKNOWN. In ordinary uncertain conditions it may report OTHER_GROUND rather than pretend semantic certainty.

## Space and transition rules

The lower-half mask is divided into equal LEFT/CENTER/RIGHT regions. A region is APPARENTLY OPEN only if at least 55% has ground evidence. It is OCCUPIED only when a **current confirmed** track overlaps the region and its bottom box contact is in the lower 45% of the image; otherwise it is UNKNOWN. `APPARENTLY OPEN` is visual evidence only, never a safety statement.

A Hough horizontal candidate needs strength ≥0.55 plus visible ground, and must occur in two of the latest three reliable analyses before `GROUND TRANSITION: POSSIBLE`. One isolated candidate remains UNKNOWN/NOT OBSERVED. Any Phase 4 non-STABLE or uncertain camera state clears transition history and makes ground category and walking-space UNKNOWN. The system does not claim a curb distance, direction of elevation, or association to ultrasonic range.

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

Debug preview (includes masks and tentative tracks):

```bash
python3 prototype_ground_awareness.py --debug-ground
```

60-second headless benchmark:

```bash
python3 prototype_ground_awareness.py --benchmark-seconds 60 --no-preview
```

The report includes ground-analysis FPS/latency, object-detection FPS, CPU, RAM, temperature, and frame freshness. Measure these on the real Pi before drawing performance conclusions.
