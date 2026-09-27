# Phase 7.5 Person Awareness

Reuses Phase 7-style local events and unchanged Phase 3.1/4 tracking. CENTER is the image middle third only. `POSSIBLE APPROACH` is relative visual closing: at least four samples, median box-area growth of 18%, CENTER, and normalized center span at most 22%. A crossing or reliable moving traversal with span >=45% suppresses the claim; an edge-entry candidate uses an 8% frame-edge margin. This intentionally favors avoiding false approach alerts during sideways crossings. It cannot identify which party moved or provide distance/collision time. `PERSON AHEAD` needs confirmed CENTER plus width >=16% and area >=4.5% of frame. No production, cloud, audio, speech, navigation, or ultrasonic association.

```bash
cd ~/Sense
git pull
source .venv-vision/bin/activate
python3 -m unittest -v test_prototype_person_awareness.py
python3 prototype_person_awareness.py --debug-people
python3 prototype_person_awareness.py --benchmark-seconds 60 --no-preview
```
