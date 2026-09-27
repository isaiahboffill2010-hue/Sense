# Sense Phase 7 — Vehicle Awareness Prototype

Uses unchanged Phase 3.1 persistent COCO tracks for `car`, `truck`, `bus`, `motorcycle`, and `bicycle`, plus unchanged Phase 4 corrected horizontal motion. The forward visual area is the central third of the image; it is image geometry only, not a lane or route.

Confirmed current tracks receive relevance points for CENTER (+2), reliable corrected motion (+2), entering CENTER (+3), crossing (+2), and persistence (+1). HIGH is 7+, MEDIUM 4–6, LOW otherwise. Tentative/LOST tracks are excluded. A side vehicle with low motion remains LOW.

Camera uncertainty produces `MOTION: UNCERTAIN` and never a corrected direction or group claim. Group motion needs at least two confirmed vehicles with the same reliable corrected direction and a strict majority. Normal output caps events at three; debug exposes raw/camera/corrected displacement, age, zones, reasons, score, and NEW/ONGOING/CHANGED event state.

Phase 4 masks all `HIGH_PRIORITY` tracks, which includes every Phase 7 vehicle class. Large or numerous moving boxes can still reduce available background features or contaminate unmasked areas; Phase 7 does not alter that implementation. Phase 3.1 only has short lost-track persistence, so long occlusions may become new IDs.

Ultrasonic remains separate and outdoor instability is not modified here. No Gemini, audio, TTS, navigation, or production Sense changes exist.

```bash
cd ~/Sense
git pull
sudo systemctl stop sense
source .venv-vision/bin/activate
export ROBOT_HAT_GPIOCHIP=0
python3 -m unittest -v test_prototype_vehicle_awareness.py
python3 prototype_vehicle_awareness.py
python3 prototype_vehicle_awareness.py --debug-vehicles
python3 prototype_vehicle_awareness.py --benchmark-seconds 60 --no-preview
```
