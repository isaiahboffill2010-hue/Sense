# Sense Phase 1: local vision prototype

This prototype is deliberately isolated from the working Sense assistant. It
does not import or call Gemini, and it does not touch speech, microphones,
ultrasonic monitoring, proximity beeps, navigation, or `main.py`.

It uses the existing Picamera2 `Camera`/`CameraReader`, a quantized 300x300 SSD
MobileNet V1 COCO detector, and the LiteRT CPU runtime. The capture thread keeps
only its newest frame, so slow inference drops old frames instead of building a
latency-producing queue.

## Raspberry Pi install

Stop Sense first because only one process can own the CSI camera:

```bash
cd ~/Sense
sudo systemctl stop sense
sudo apt update
sudo apt install -y python3-venv python3-opencv python3-picamera2
python3 -m venv --system-site-packages .venv-vision
source .venv-vision/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r prototype_requirements.txt
python3 download_prototype_model.py
python3 -m unittest -v test_prototype_vision.py
```

The downloader fetches the public Google Coral test-data model and labels and
refuses to install either file unless both its exact byte length and SHA-256
digest match the values recorded in the script. Generated model files are
gitignored.

## Run

The default is a 60-second benchmark, 0.50 confidence threshold, at most five
detection cycles per second, and two LiteRT inference threads:

```bash
cd ~/Sense
source .venv-vision/bin/activate
python3 prototype_vision.py
```

Press `Q`, Escape, or Ctrl-C to stop early. Useful variants:

```bash
python3 prototype_vision.py --confidence 0.60 --max-detection-fps 3
python3 prototype_vision.py --benchmark-seconds 180
python3 prototype_vision.py --no-preview --benchmark-seconds 60
```

`--no-preview` is intended for an SSH/headless benchmark; it still performs
real camera capture and inference. The program prints a final summary with
average/p50/p95 inference latency, total-cycle latency, detection rate, frame
freshness, process RSS, available system memory, whole-system CPU use, and CPU
temperature. Metrics that Linux cannot provide are shown as `n/a`, never made
up.

To return control of the camera to Sense after testing:

```bash
sudo systemctl start sense
```
