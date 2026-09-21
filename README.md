# Sense — Navigation Headband

A prototype wearable navigation aid for blind and low-vision users.

**Phase 1 (hardware) and Phase 2 (Gemini vision) are implemented.**
There is no speech output yet — Gemini's descriptions appear in the terminal
and on the preview HUD. Text-to-speech comes in a later phase.

---

## What this prototype does

Running `python3 main.py` on the Raspberry Pi opens a live camera window and,
at the same time:

1. Shows the **live CSI camera feed**.
2. Continuously measures distance with the **SunFounder ultrasonic sensor** on
   the Robot HAT.
3. Draws the current distance and a status word on top of the video.
4. Plays **alerts through your headphones** — one subtle tone when an
   obstacle gets close, repeated beeps only when it gets dangerously close.
5. Asks **Gemini** for a very short description of what is ahead, once per
   approach, and shows it as `AI: Person ahead.`

The on-screen overlay looks like this:

```
-----------------------------------------
| FPS 28.4                              |
|                                       |
|             LIVE CAMERA               |
|                                       |
| Distance: 72 cm                       |
| Status: CAUTION                       |
| AI: Person ahead.                     |
| Camera: OK                            |
| Ultrasonic: OK                        |
| Audio: OK                             |
| Gemini: OK                            |
| Press Q to quit                       |
-----------------------------------------
```

### Distance bands and alert behaviour

The device stays **mostly silent during normal use**. It only makes a sound
when it has something new to tell you.

| Distance | Status | Sound | Gemini |
| --- | --- | --- | --- |
| more than 100 cm | `SAFE` | silent, still measuring | no |
| 50 – 100 cm | `CAUTION` | silent, obstacle tracked | **yes** — one analysis |
| 25 – 50 cm | `WARNING` | **one** subtle 660 Hz tone on entry, then quiet | only if the cooldown allows |
| under 25 cm | `DANGER` | repeated 1000 Hz beeps while it lasts | only if the cooldown allows |
| no valid reading | `UNKNOWN` | silent | no |

So a family member standing in front of you at 30–40 cm produces **one tone,
not a stream of them**. The tone plays again only if the obstacle leaves the
warning band and comes back — either by moving away, or by coming closer into
`DANGER` and then backing off again.

The two sounds are deliberately different so you can tell them apart without
looking: the warning tone is lower and quieter, the danger beep is higher and
more urgent.

#### Anti-chatter

Ultrasonic readings jitter by a few cm, so an obstacle sitting right on the
25 cm or 50 cm line would otherwise flip bands on every read. Two guards
prevent that, both in [config.py](config.py):

- **`STATUS_HYSTERESIS_CM = 5.0`** — moving to a *closer* band is reported
  immediately, because getting nearer is the safety-critical direction. Moving
  *back out* requires clearing the boundary by 5 cm, so leaving `DANGER` needs
  more than 30 cm and leaving `WARNING` needs more than 55 cm.
- **`WARNING_TONE_MIN_GAP_S = 3.0`** — even a legitimate re-entry will not
  sound again within 3 seconds. Set it to `0.0` to disable.

`UNKNOWN` is deliberately silent. If the sensor stops answering, the program
says so — it never guesses a distance and never fakes a "safe" reading.

This logic is covered by [test_alerts.py](test_alerts.py), which needs no
hardware and makes no network calls:

```bash
python3 test_alerts.py
```

---

## Gemini vision (Phase 2)

When an obstacle moves into a **closer** band, the main loop hands a copy of
the camera frame it already has to a background thread, which asks Gemini for
one short navigation phrase.

```
AI: Person ahead.
AI: Two people ahead.
AI: Chair directly ahead.
AI: Closed door ahead.
AI: Stairs descending ahead.
AI: Path clear.
```

**Gemini is strictly optional.** No internet, a timeout, an API error, a quota
limit, a malformed reply — none of it can delay or suppress the ultrasonic
readings or the local beeps. If Gemini is unavailable the device behaves
exactly as it did in Phase 1 and simply shows no description.

### When it asks

One analysis per approach, triggered when an obstacle steps **up** in severity
into `CAUTION`, `WARNING` or `DANGER`, and rate-limited by a single global
`GEMINI_COOLDOWN_S = 5.0` cooldown.

In an ordinary walk-up (`SAFE → CAUTION → WARNING → DANGER`) only the
**CAUTION** entry actually reaches the API — the later steps follow within a
second or two and the cooldown absorbs them. CAUTION is deliberately where it
fires: at 50–100 cm the camera framing is far better than at 25 cm, where a
person simply fills the frame, and there is more time for the reply to arrive.

Keying on *any* increase in severity rather than one named band matters,
because readings arrive every ~90 ms and walking pace covers ~13 cm in that
time. A fast approach — or just turning your head — can jump straight from
`SAFE` to `WARNING` or `DANGER`, skipping a band. Those cases still get
analysed.

Moving **away** never triggers a request.

### Stale descriptions are discarded

Age is measured from the moment the **image was captured**, not from when the
reply arrived. A description older than `GEMINI_RESULT_MAX_AGE_S = 4.0`
seconds is thrown away instead of shown, so `Chair ahead.` can never appear
eight seconds after you have already walked past the chair. This applies both
when a reply arrives late and while a description is sitting on screen.

### Nothing stacks up

The request queue holds exactly **one** item. If an analysis is already queued
or in flight, a new request is dropped rather than added. JPEG encoding and
the network call both happen on the worker thread, never on the camera/UI
thread.

### Cost

A 640×480 frame fits in a single 768×768 tile = 258 image tokens. With the
prompt and a short reply that is roughly **$0.0001 per call** on
`gemini-3.5-flash-lite`. Even saturating the 5 s cooldown for a solid hour is
about 8 cents. There is also a free tier.

---

## Hardware used

| Part | Notes |
| --- | --- |
| Raspberry Pi 3 | running Raspberry Pi OS, Wi-Fi for Gemini (optional) |
| SunFounder Robot HAT+5 | mounted on the Pi's 40-pin header |
| SunFounder ultrasonic sensor | HC-SR04-style head with SunFounder's interface board, into the HAT's digital ports |
| Raspberry Pi CSI camera | connected with the ribbon cable |
| Stereo headphones | plugged into the **Raspberry Pi's** 3.5 mm jack |
| USB power bank | powers the Pi |

There is **no buzzer**. Every beep goes out through the Pi's normal audio
system into the headphones, in both ears.

---

## Wiring

The ultrasonic sensor plugs into the Robot HAT's 3-pin **digital ports**, so
the code addresses it by Robot HAT port name (`"D2"`, `"D3"`) rather than by
raw pin number. Each port is hard-wired by the HAT to one BCM GPIO:

| Signal | Cable colour | Robot HAT port | BCM GPIO |
| --- | --- | --- | --- |
| VCC | **red** | red power pin of the **D2** port | — |
| TRIG | **yellow** | yellow signal pin of the **D2** port | GPIO27 |
| ECHO | **white** | yellow signal pin of the **D3** port | GPIO22 |
| GND | **black** | black ground pin of the **D3** port | — |

Splitting the 4-wire cable across two 3-pin ports like this is fine — all of
the HAT's digital ports share the same VCC and GND rails, so the sensor still
gets power from D2 and ground from D3.

For reference, the full digital port map on this HAT is
`D0 → GPIO17`, `D1 → GPIO4`, `D2 → GPIO27`, `D3 → GPIO22`.

### About the 5 V ECHO warning

A **bare HC-SR04 wired straight to the Pi header** drives roughly 5 V on ECHO,
which would damage a 3.3 V Pi GPIO pin, and needs a voltage divider or a level
shifter. **That is not this setup.**

Here the Robot HAT sits between the sensor and the Pi, its digital ports are
3.3 V ports, and SunFounder supplies this sensor specifically for them. Use the
supplied cable as-is — **do not add a divider of your own.**

The warning comes back if you ever move this sensor onto the bare Pi header or
substitute a generic 5 V HC-SR04.

---

## Python version

Raspberry Pi OS ships with **Python 3** already installed, and that is what
this project targets:

- **Python 3.9 or newer** (Bookworm ships 3.11; Bullseye ships 3.9)
- Always run it as `python3`, never `python`

```bash
python3 --version
```

---

## Dependencies

### 1. Raspberry Pi OS packages (`apt`)

On Raspberry Pi OS these should be installed with `apt`, **not** `pip` — the
apt builds are compiled against the system's libcamera, SDL and kernel
headers, they install in seconds, and the pip versions frequently fail to
build on a Pi 3 or end up unable to see libcamera.

```bash
sudo apt update
sudo apt install -y python3-picamera2 python3-opencv python3-pygame alsa-utils \
                    git python3-pip python3-setuptools python3-smbus i2c-tools
```

| Package | Why |
| --- | --- |
| `python3-picamera2` | current Raspberry Pi camera stack (Picamera2 / libcamera) |
| `python3-opencv` | draws the overlay and shows the live preview window |
| `python3-pygame` | low-latency beep playback |
| `alsa-utils` | provides `aplay`, the fallback audio backend and a test tool |
| `python3-smbus`, `i2c-tools` | I²C support the Robot HAT library needs |

### 2. SunFounder `robot_hat` (from SunFounder's git repository)

The ultrasonic sensor is driven through SunFounder's **official Robot HAT
library**. It is not installed with pip:

```bash
cd ~
git clone https://github.com/sunfounder/robot-hat.git -b 2.5.x
cd robot-hat
sudo python3 install.py
```

Verify it:

```bash
python3 -c "import robot_hat; print(robot_hat.__version__)"
```

> **Do not** run `pip install robot-hat` — that PyPI name belongs to a
> different, unofficial fork.
>
> If the `2.5.x` branch does not exist or the install fails, the older
> documented route is `git clone -b v2.0 https://github.com/sunfounder/robot-hat.git`
> then `sudo python3 setup.py install`.

The Robot HAT needs **I²C enabled**:

```bash
sudo raspi-config     # Interface Options -> I2C -> Yes
sudo reboot
```

### 3. Python packages (`pip`)

Exactly one: **`google-genai`**, for Gemini. It is pure Python, there is no
apt package for it, and the old `google-generativeai` SDK is deprecated.

Raspberry Pi OS Bookworm and newer block system-wide pip installs (PEP 668).
**Do not solve that with a plain virtual environment** — a plain venv cannot
see the apt-installed Picamera2 or the system-installed `robot_hat`, which
would break Phase 1. Use either:

```bash
# (a) user install - leaves the working Phase 1 environment untouched
pip3 install --user --break-system-packages google-genai
```

```bash
# (b) or a venv that can still see the system packages
python3 -m venv --system-site-packages ~/sense-venv
~/sense-venv/bin/pip install google-genai
~/sense-venv/bin/python main.py
```

Everything else this project uses comes from apt or the standard library.

> **If you use a virtual environment**, create it with
> `python3 -m venv --system-site-packages .venv` so it can still see the
> apt-installed Picamera2/OpenCV and the system-installed `robot_hat`. A plain
> `venv` cannot see them and neither the camera nor the sensor will start.

---

## Camera setup

This project uses **Picamera2 / libcamera**, the current stack. The old
`picamera` library and the "legacy camera" option in `raspi-config` are
obsolete and must stay **disabled**.

1. Power the Pi **off** before connecting or reseating the ribbon cable.
2. Seat the CSI ribbon at both ends, metal contacts facing the correct way.
3. Power on and confirm the camera is detected:

```bash
rpicam-hello --list-cameras     # Bookworm and newer
libcamera-hello --list-cameras  # older Raspberry Pi OS
```

4. Try a five-second preview straight from the shell:

```bash
rpicam-hello -t 5000
```

If that shows a picture, the camera hardware is fine and `main.py` will work.

> With the Robot HAT fitted, check the camera ribbon still has clearance — the
> HAT sits directly over the CSI connector on a Pi 3.

---

## Audio setup

The beep is generated locally the first time you run the program and saved to
`assets/beep.wav`. **No internet connection is ever needed.**

### Which output do the beeps come out of?

With a Robot HAT fitted there are **two** possible outputs:

| Output | What it is |
| --- | --- |
| the Pi 3's own 3.5 mm jack | **stereo — this is where your headphones go** |
| the Robot HAT's onboard speaker | mono I²S speaker soldered to the HAT, no headphone socket |

The Robot HAT has no headphone socket, so the headphones stay in the **Pi's**
jack. But if you have run SunFounder's `i2samp.sh` speaker script, it can make
the HAT's I²S speaker the **default** ALSA output — and then the beeps come out
of the little onboard speaker instead of your headphones.

Two ways to fix that:

```bash
sudo raspi-config
#   System Options  ->  Audio  ->  Headphones
```

or pin it explicitly in [config.py](config.py), which both audio backends
honour:

```bash
aplay -L | grep -i -E 'headphone|card|hw:'     # find the exact device name
```

```python
AUDIO_DEVICE = "plughw:CARD=Headphones,DEV=0"
```

### General audio checks

```bash
alsamixer                      # volume up, unmute with M, Esc to exit
speaker-test -c2 -twav -l1     # should say "Front Left" / "Front Right"
aplay -l                       # lists the available playback devices
```

When `main.py` starts it plays **one short test beep** so you can confirm the
headphones really work before you trust the warnings.

---

## Installation

On the **Raspberry Pi** (not on your Windows PC):

```bash
# 1. system packages
sudo apt update
sudo apt install -y python3-picamera2 python3-opencv python3-pygame alsa-utils \
                    git python3-pip python3-setuptools python3-smbus i2c-tools

# 2. SunFounder Robot HAT library
cd ~
git clone https://github.com/sunfounder/robot-hat.git -b 2.5.x
cd robot-hat
sudo python3 install.py

# 3. enable I2C for the HAT, then reboot
sudo raspi-config      # Interface Options -> I2C -> Yes
sudo reboot

# 4. get this project
cd ~
git clone https://github.com/isaiahboffill2010-hue/Sense.git
cd Sense

# 5. Gemini SDK
pip3 install --user --break-system-packages google-genai

# 6. Gemini API key - create .env.local (already gitignored)
echo 'GEMINI_API_KEY=your-key-here' > .env.local
```

Get a key from <https://aistudio.google.com/apikey>. The key is read from the
environment, never hardcoded, never printed, and never committed.

Already cloned it before? Just `cd ~/Sense && git pull`.

`TRIG_PIN` and `ECHO_PIN` are already set to `"D2"` and `"D3"` in
[config.py](config.py), matching the wiring table above — if you used those
ports, there is nothing to edit.

---

## How to run the test

Run it **from the Pi's desktop**, or over VNC, or from a terminal on a screen
attached to the Pi — the live preview needs a display:

Check Gemini first — this makes one API call (~$0.0001) and exercises the
exact code path the app uses, so if it passes, the app's Gemini path works:

```bash
python3 smoke_test_gemini.py
```

Then:

```bash
python3 main.py
```

You should see a startup report, then a live camera window:

```
==============================================================
  AI Navigation Headband - Phase 1 Hardware Test
==============================================================
Operating system : Linux 6.6.51+rpt-rpi-v8
Architecture     : aarch64
Python           : 3.11.2
Raspberry Pi detected: Raspberry Pi 3 Model B Plus Rev 1.3
--------------------------------------------------------------
Camera: checking...
Camera: OK - imx219 @ 640x480 (RGB888)
Ultrasonic sensor: checking... (robot_hat TRIG=D2 (GPIO27) ECHO=D3 (GPIO22))
Ultrasonic sensor: OK - robot_hat TRIG=D2 (GPIO27) ECHO=D3 (GPIO22) - first reading 84.2 cm
Audio: checking...
Audio: OK - pygame.mixer / SDL (pulseaudio)
       beep file: /home/pi/Sense/assets/beep.wav
       a test beep was sent to the headphones - did you hear it?
Gemini vision: checking...
       loaded from .env.local: GEMINI_API_KEY
Gemini vision: OK - gemini-3.5-flash-lite (SDK timeout 8000 ms)
--------------------------------------------------------------
```

### Useful flags

| Command | What it does |
| --- | --- |
| `python3 main.py` | the normal full test |
| `python3 main.py --headless` | no window; prints status to the terminal (for plain SSH) |
| `python3 main.py --skip-camera` | test only the sensor and the beeps |
| `python3 main.py --skip-ultrasonic` | test only the camera and the beeps |
| `python3 main.py --skip-audio` | test silently |
| `python3 main.py --skip-gemini` | run Phase 1 only, no API calls |
| `python3 main.py --help` | list all options |

---

## How to quit

- Press **Q** (or **Esc**) with the camera window focused, **or**
- close the window with its X button, **or**
- press **Ctrl+C** in the terminal.

All three paths do the same clean shutdown: stop the camera, stop the
ultrasonic thread, stop the beeper thread, release the sensor, close the audio
device and destroy the OpenCV windows.

In `--headless` mode, use **Ctrl+C**.

---

## Troubleshooting

### "WRONG COMPUTER" appears immediately

You ran it on Windows or macOS. This program drives real Pi hardware; run it
on the Raspberry Pi.

### `ULTRASONIC ERROR: The SunFounder robot_hat library is not installed`

Install it from SunFounder's repository (see
[Dependencies](#2-sunfounder-robot_hat-from-sunfounders-git-repository)), then:

```bash
python3 -c "import robot_hat; print(robot_hat.__version__)"
```

If that works from a plain shell but not from your virtual environment,
recreate the venv with `--system-site-packages`.

### `ULTRASONIC ERROR: No echo received on ECHO D3`

Work down this list:

1. Is the **white ECHO** wire in the **yellow signal** pin of the **D3** port,
   and the **yellow TRIG** wire in the **yellow signal** pin of the **D2** port?
   Swapping these two is the most common mistake.
2. Is the **red VCC** wire in the **red** pin of D2, and the **black GND** wire
   in the **black** pin of D3?
3. Is the Robot HAT seated firmly on all 40 pins, and is it powered (battery
   connected / power switch on)?
4. Is there anything within about 4 m for the ping to bounce off? Point it at a
   wall — an empty room legitimately returns no echo.
5. Confirm I²C is enabled: `sudo raspi-config` → Interface Options → I2C.

### `ULTRASONIC ERROR: robot_hat could not open ports`

Valid digital port names on this HAT are `"D0"` – `"D3"`. Check the spelling
and capitalisation in [config.py](config.py).

### `Ultrasonic: NOT CONFIGURED`

`TRIG_PIN` / `ECHO_PIN` are `None` in [config.py](config.py). They ship set to
`"D2"` / `"D3"`, so this only appears if they were edited.

### `CAMERA ERROR: No camera detected by libcamera`

```bash
rpicam-hello --list-cameras
dmesg | grep -i -E 'imx|ov5647|camera'
grep camera /boot/firmware/config.txt
```

Power the Pi down and reseat both ends of the ribbon cable — with the HAT
fitted over the CSI connector, a partly lifted ribbon is very easy to miss.

### `CAMERA ERROR: Picamera2 is not installed`

```bash
sudo apt install -y python3-picamera2
python3 -c "from picamera2 import Picamera2; print('ok')"
```

### `Gemini: NOT CONFIGURED`

`GEMINI_API_KEY` is not set. Put it in `.env.local` (already gitignored) or
export it, then re-run `python3 smoke_test_gemini.py`.

### `GEMINI ERROR: The google-genai package is not installed`

```bash
pip3 install --user --break-system-packages google-genai
python3 -c "import google.genai; print('ok')"
```

### Gemini descriptions never appear

Run `python3 smoke_test_gemini.py` first. If that passes but the HUD stays
blank, the replies are probably arriving too late and being discarded as
stale — the smoke test prints the round-trip time. Raise
`GEMINI_RESULT_MAX_AGE_S` in [config.py](config.py) if your connection is
consistently slow.

### Gemini keeps erroring but the beeps still work

That is the intended behaviour — Gemini is an optional layer. Use
`python3 main.py --skip-gemini` to silence it entirely.

### `AUDIO ERROR: Headphone/audio output unavailable`

```bash
aplay -l                          # is there a playback device?
aplay -L                          # exact device names for AUDIO_DEVICE
speaker-test -c2 -twav -l1        # does anything reach the headphones?
alsamixer                         # volume up, unmute with M
sudo raspi-config                 # System Options -> Audio -> Headphones
```

### The beeps come out of the Robot HAT's little speaker, not the headphones

SunFounder's `i2samp.sh` has made the HAT's I²S speaker the default output.
Either switch the default back with `raspi-config`, or set `AUDIO_DEVICE` in
[config.py](config.py) to the Pi's headphone device — see
[Audio setup](#audio-setup).

### `DISPLAY ERROR: could not open a preview window`

You are on a text-only SSH session. Run it from the Pi's desktop or over VNC,
or use `python3 main.py --headless`.

### The preview is choppy

A Pi 3 is not fast. Lower `CAMERA_RESOLUTION` in `config.py` (try `(480, 360)`
or `(320, 240)`). The FPS counter in the corner tells you what you are getting.

### The colours look wrong (blue faces, orange sky)

Set `CAMERA_SWAP_RED_BLUE = True` in [config.py](config.py).

---

## Project layout

```
main.py                 startup diagnostics, the main loop, clean shutdown
config.py               ALL settings: HAT ports, thresholds, camera, audio
alerts.py               distance -> SAFE / CAUTION / WARNING / DANGER -> beep rate
ui.py                   draws the text overlay onto the camera frame
diagnostics.py          "am I actually running on a Raspberry Pi?"

hardware/
    __init__.py
    camera.py           Picamera2 / libcamera CSI camera
    ultrasonic.py       SunFounder sensor via robot_hat + background reader thread
    audio.py            beep generation + playback + the beeper thread

vision.py               Gemini worker thread, .env loader, staleness rules

assets/                 beep.wav and warning_tone.wav generated on first run
test_alerts.py          hardware-free tests for alerts + the Gemini logic
smoke_test_gemini.py    one-shot "can this Pi reach Gemini?" check
requirements.txt        the one pip dependency, and why everything else is apt
```

### How it stays smooth

Three threads, so nothing blocks the video:

| Thread | Job |
| --- | --- |
| main thread | capture frames, draw the overlay, `cv2.imshow`, handle keys |
| `ultrasonic` | fire pings through `robot_hat`, publish the latest reading |
| `beeper` | sleep between danger beeps, and play one-shot warning tones |
| `gemini` | JPEG-encode a frame copy and call the API |

The main loop never calls `time.sleep()` for the beep rhythm and never waits
for an echo. It just reads the latest sensor snapshot and tells the beeper
which interval to use. Both background threads wait on a `threading.Event`, so
they stop instantly when you quit rather than finishing a sleep first.

---

## Honesty policy

This program is a **diagnostic tool**, so it never pretends:

- no simulated camera frames or test patterns
- no fake or interpolated distance values
- no silent fallback to mock hardware
- failures are printed with the real underlying error text

One detail worth knowing: `robot_hat.Ultrasonic.read()` signals failure by
*returning* `-1` rather than raising. Left alone that would surface as a
distance of "-1 cm" and be classified `DANGER`. `hardware/ultrasonic.py`
converts those sentinels into a real error instead, so a broken sensor can
never look like a very close obstacle.

---

## Roadmap — not built yet

Later phases will add spoken output (text-to-speech) for the Gemini
descriptions, richer obstacle warnings, and possibly GPS navigation.
**None of that is in this repository yet.** Gemini's descriptions currently
appear only in the terminal and on the HUD.
