# Running Sense headless at boot

Turn the Pi on, and Sense starts on its own: camera, ultrasonic, Gemini and
audio come up, then the headphones say **"Sense ready."** No HDMI, no
keyboard, no desktop login, no terminal.

The service starts Sense with `--headless`, which disables only the preview
window and the on-screen HUD. Camera capture, Gemini vision, ultrasonic
sensing, warning beeps, spoken guidance, headphone routing, navigation and
logging all keep working exactly as before.

---

## Install

Five commands on the Pi:

```bash
sudo cp /home/sense/Sense/deploy/sense.service /etc/systemd/system/sense.service
sudo systemctl daemon-reload
sudo systemctl enable sense.service
sudo systemctl start sense.service
systemctl status sense.service
```

Then reboot once to prove it comes up by itself:

```bash
sudo reboot
```

## Check it before you trust it

The single most likely thing to break is the Python import path. By hand you
run `sudo -E`, which keeps `HOME=/home/sense`, so `pip3 --user` packages are
found. As a service, root's `HOME` would default to `/root` and
`google-genai` would vanish. The unit sets `HOME=/home/sense` to match — this
command verifies that assumption in the same context the service uses:

```bash
sudo HOME=/home/sense /usr/bin/python3 -c "import google.genai, robot_hat; print('imports OK')"
```

If that fails, either install the package for root
(`sudo pip3 install --break-system-packages google-genai`) or correct the
`HOME=` line in the unit.

## Everyday commands

| What | Command |
| --- | --- |
| Is it running? | `systemctl is-active sense` |
| Full status | `systemctl status sense` |
| Live logs | `journalctl -u sense -f` |
| Logs since boot | `journalctl -u sense -b` |
| Just today's errors | `journalctl -u sense -b -p warning` |
| Stop | `sudo systemctl stop sense` |
| Restart | `sudo systemctl restart sense` |
| Start again | `sudo systemctl start sense` |
| Disable autostart | `sudo systemctl disable sense` |
| Re-enable autostart | `sudo systemctl enable sense` |
| Remove completely | `sudo systemctl disable --now sense && sudo rm /etc/systemd/system/sense.service && sudo systemctl daemon-reload` |

To run it by hand while the service is stopped:

```bash
sudo systemctl stop sense
cd /home/sense/Sense
export AUDIO_DEVICE=plughw:1,0
export ROBOT_HAT_GPIOCHIP=0
sudo -E python3 main.py                 # with preview, if HDMI is attached
sudo -E python3 main.py --headless      # exactly what the service runs
```

## What a healthy boot looks like

```
journalctl -u sense -b
```

```
Audio device      : plughw:1,0
Camera: OK - imx219 @ 640x480 (RGB888)
Ultrasonic sensor: OK - robot_hat TRIG=D0 (GPIO17) ECHO=D1 (GPIO4) - first reading 84.2 cm
Audio: OK - aplay (...) -> plughw:1,0
Speech: OK - espeak-ng (...) -> plughw:1,0
       spoke "Sense ready." on that device - did you hear it in the headphones?
Gemini vision: OK - gemini-3.5-flash-lite (Developer API, ...)
Gemini: connection warm (412 ms)
Headless mode. Press Ctrl+C to quit.
```

Then, as you approach something:

```
GEMINI RAW: 'Chair directly ahead, move left.'
AI ACCEPTED: Chair directly ahead, move left.   [58cm CAUTION via band  enc 12ms  api 1420ms  age 1.5s]
SPEECH QUEUED: Chair directly ahead, move left.
SPEECH PLAYING: Chair directly ahead, move left.
```

## Tuning

Both are in `sense.service`; edit, then
`sudo systemctl daemon-reload && sudo systemctl restart sense`.

- **Slow boot?** `Wants=`/`After=network-online.target` can add 10–30 s on
  Wi-Fi. Sense works without the network, so changing both to
  `network.target` makes "Sense ready." arrive sooner; Gemini then simply
  fails its first request and recovers.
- **`Camera: FAIL` on a cold boot but fine on restart?** Raise
  `ExecStartPre=/bin/sleep 5`. Sense treats a missing camera as non-fatal, so
  it will keep running without one rather than exiting for a restart.

## Notes

- The **Gemini API key is not in the unit file.** `main.py` reads it from
  `/home/sense/Sense/.env.local`, which stays gitignored. Keep that file
  readable by root: `sudo chmod 600 /home/sense/Sense/.env.local`.
- `AUDIO_DEVICE` and `ROBOT_HAT_GPIOCHIP` are set by the unit, not by
  `.bashrc` — systemd never loads interactive shell config.
- Ultrasonic pins stay `D0`/`D1` (GPIO17/GPIO4); the service changes no
  hardware configuration.
