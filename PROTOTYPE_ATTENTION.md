# Sense Phase 5 — Local Relevance and Attention Prototype

This isolated prototype ranks confirmed visual tracks by **relevance**, not danger, collision probability, distance, or a navigation command. It reuses the unchanged Phase 3.1 tracker and Phase 4 camera-motion correction. It makes no Gemini/cloud, audio, speech, GPIO, or production-Sense call.

## Rules

For a current confirmed track, the deterministic score is: persistent track **+1**; person/car/truck/bus/motorcycle/bicycle **+3**; CENTER **+2**; reliable corrected left/right motion **+2**; LEFT→CENTER or RIGHT→CENTER **+3**; and Phase 3.1 crossing for a high-priority class **+2**. Scores are HIGH at **8+**, MEDIUM at **4–7**, and LOW below 4. The score is only a sorting aid, never a percentage or safety assessment.

Corrected movement is only used when Phase 4 camera motion is reliable. When it is `MOTION UNCERTAIN`, the reason says `camera motion uncertain` and no corrected-motion points are awarded. A track entering CENTER must have its latest two recorded positions be side then CENTER. Moving away receives no entering-center bonus.

Escalation happens immediately. Falling relevance requires two consecutive lower samples, then drops one level at a time (HIGH→MEDIUM→LOW), preventing rapid flicker and allowing gradual decay. Tentative or LOST tracks score LOW and are never top attention. Normal COCO classes remain eligible but receive no high-priority class points.

The independent ultrasonic monitor remains a separate `FORWARD OBSTACLE` context line. Its range is never copied onto any visual track or used to claim which object made the echo.

## Performance and operation

Attention is integer arithmetic over the small current confirmed-track list. Its time is reported separately. Phase 5 still uses Phase 4's bounded sparse-flow configuration and newest-frame processing; actual Pi results must be measured, not assumed.

On the Raspberry Pi:

```bash
cd ~/Sense
git pull
sudo systemctl stop sense
source .venv-vision/bin/activate
export ROBOT_HAT_GPIOCHIP=0
python3 -m unittest -v test_prototype_attention.py
python3 prototype_attention.py
```

Use `--debug-attention` to print every current attention calculation. Press `Q` or `Esc` to exit the preview. For headless measurement:

```bash
python3 prototype_attention.py --benchmark-seconds 60 --no-preview
```
