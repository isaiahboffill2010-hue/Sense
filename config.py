"""
config.py
=========

Central configuration for the Phase 1 hardware test.

Edit THIS FILE (and only this file) to match your own wiring and preferences.
Nothing in here talks to hardware; it is plain data so it is safe to open and
read on any computer, including Windows.

--------------------------------------------------------------------------
PIN NAMING
--------------------------------------------------------------------------
The ultrasonic sensor plugs into the SunFounder Robot HAT, so it is named by
Robot HAT DIGITAL PORT ("D0", "D1") rather than by raw pin number. That is
what the robot_hat library expects.

Each port is hard-wired by the HAT to one **BCM** (Broadcom) GPIO number -
not to a physical 1-40 header position:

    "D0"  ==  BCM GPIO17  ==  physical pin 11
    "D1"  ==  BCM GPIO4   ==  physical pin 7

Run `pinout` on the Raspberry Pi to see the full map for your board.
"""

import os
from pathlib import Path

# Absolute path to this project folder (used for the generated beep file).
PROJECT_ROOT = Path(__file__).resolve().parent


def _load_local_environment():
    """Load simple KEY=VALUE settings before module constants are resolved."""
    path = PROJECT_ROOT / ".env.local"
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_local_environment()


# ==========================================================================
# 1. ULTRASONIC SENSOR WIRING (SunFounder Robot HAT digital ports)
# ==========================================================================
#
# The sensor is the SunFounder ultrasonic module - an HC-SR04-style head with
# SunFounder's own interface board on the back - plugged into the Robot HAT's
# 3-pin DIGITAL ports, not wired to the bare Pi header.
#
# We therefore address it by Robot HAT port name ("D0".."D3"), which is what
# the robot_hat library expects. The BCM number each port maps to is fixed by
# the HAT:
#
#     "D0"  ->  GPIO17     <- TRIG (yellow wire)
#     "D1"  ->  GPIO4      <- ECHO (white wire)
#     "D2"  ->  GPIO27
#     "D3"  ->  GPIO22
#
# Sensor cable colours:
#
#     RED    VCC   -> red power pin of the D0 port
#     YELLOW TRIG  -> yellow signal pin of the D0 port
#     WHITE  ECHO  -> yellow signal pin of the D1 port
#     BLACK  GND   -> black ground pin of the D1 port
#
# Splitting the 4-wire sensor across two 3-pin ports like this is fine: all
# of the Robot HAT's digital ports share the same VCC and GND rails, so the
# sensor still gets power from D0 and ground from D1.
#
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# A NOTE ON THE OLD 5V ECHO WARNING
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# A BARE HC-SR04 wired straight to the Pi header drives roughly 5V on ECHO,
# which would damage a 3.3V Pi GPIO pin, and needs a voltage divider or level
# shifter. That is NOT this setup.
#
# Here the Robot HAT sits between the sensor and the Pi, the digital ports
# are 3.3V ports, and SunFounder supplies this sensor specifically for them.
# Do not add a divider of your own - just use the supplied cable.
#
# The warning still applies if you ever move this sensor onto the bare Pi
# header, or substitute a generic 5V HC-SR04.
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

TRIG_PIN = "D0"   # Robot HAT digital port for TRIG  (yellow wire, GPIO17)
ECHO_PIN = "D1"   # Robot HAT digital port for ECHO  (white wire,  GPIO4)

# Reference only - the robot_hat library resolves the names above itself.
# Used by the startup report so you can sanity-check against `pinout`.
ROBOT_HAT_PIN_TO_BCM = {"D0": 17, "D1": 4, "D2": 27, "D3": 22}


# ==========================================================================
# 2. DISTANCE THRESHOLDS AND ALERT BEHAVIOUR
# ==========================================================================
# The device stays quiet during normal use. Sound only happens when it
# actually tells you something new:
#
#     distance  > 100          -> SAFE     silent, still measuring
#     50 <= distance <= 100    -> CAUTION  silent, obstacle tracked
#     25 <= distance <  50     -> WARNING  ONE subtle tone when the obstacle
#                                          FIRST enters this band, then quiet
#     distance  < 25           -> DANGER   repeated beeps while it lasts
#
# So a person standing still at 30-40 cm produces one tone, not a stream of
# them. The tone only plays again if the obstacle leaves the warning band
# and comes back - either by moving away past the exit threshold, or by
# coming closer into DANGER and then backing off into WARNING again.

SAFE_DISTANCE_CM = 100.0      # above this -> SAFE
CAUTION_DISTANCE_CM = 50.0    # at/above this (and <= SAFE) -> CAUTION
WARNING_DISTANCE_CM = 25.0    # at/above this (and < CAUTION) -> WARNING
                              # below WARNING_DISTANCE_CM     -> DANGER

# Seconds between beeps in the DANGER band. Smaller = faster beeping.
# This is now the only band that repeats.
BEEP_INTERVAL_DANGER_S = 0.15

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# ANTI-CHATTER
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# Ultrasonic readings jitter by a few cm. Without protection, an obstacle
# sitting right on the 25 cm or 50 cm line would flip bands on every read
# and re-trigger the tone over and over. Two guards stop that:
#
# 1. Hysteresis. Moving to a CLOSER band happens immediately - getting
#    nearer is the safety-critical direction and must never be delayed.
#    Moving BACK OUT to a further band requires clearing the boundary by
#    this margin. With 5 cm, leaving DANGER needs > 30 cm, and leaving
#    WARNING needs > 55 cm.
STATUS_HYSTERESIS_CM = 5.0

# 2. A re-arm delay on the warning tone. Even a legitimate re-entry will
#    not sound again within this many seconds. Set to 0.0 to disable.
WARNING_TONE_MIN_GAP_S = 3.0


# ==========================================================================
# 3. ULTRASONIC SENSOR TIMING
# ==========================================================================
# The ping timing itself is handled by robot_hat.Ultrasonic. These settings
# control how OUR background thread uses it.

SENSOR_MIN_DISTANCE_CM = 2.0      # HC-SR04 datasheet minimum
SENSOR_MAX_DISTANCE_CM = 400.0    # HC-SR04 datasheet maximum

SENSOR_READ_INTERVAL_S = 0.06     # pause between measurement cycles
SENSOR_SAMPLES_PER_READING = 3    # median of N pings -> rejects random spikes
SENSOR_ERRORS_BEFORE_FAIL = 12    # consecutive failures before status -> FAIL

# Passed to robot_hat.Ultrasonic(timeout=...). A 400 cm round trip takes
# about 23 ms, so 30 ms leaves headroom without stalling the thread.
ULTRASONIC_TIMEOUT_S = 0.030

# Anything above this is physically impossible for this sensor and means the
# reading is bogus, not distant. Current robot_hat builds dropped the guard
# for an ECHO line that is already HIGH when a ping starts, and in that case
# read() can return a huge number instead of its -2 error code. Treating that
# as "very far away / SAFE" is exactly the kind of false reassurance this
# project must not produce, so we reject it as a fault instead.
SENSOR_IMPLAUSIBLE_ABOVE_CM = 600.0

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# PROTECTING THE PULSE TIMING FROM THE REST OF PYTHON
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# robot_hat times the ECHO pulse in a pure-Python busy loop:
#
#     while self.echo.value() == 0: pulse_start = time.time()
#     while self.echo.value() == 1: pulse_end   = time.time()
#
# That is exquisitely sensitive to the GIL. Python hands the GIL to another
# thread every sys.getswitchinterval() seconds (5 ms by default), so if this
# thread is preempted in the middle of a pulse, the measured width is
# inflated by whole multiples of that interval - roughly 85 cm per 5 ms.
#
# A stuck reading near 184 cm is 10.8 ms, almost exactly TWO switch
# intervals, which is the signature of exactly that preemption.
#
# While a ping is in flight we raise the switch interval so the timing loop
# keeps the GIL for the whole pulse. A ping lasts at most
# ULTRASONIC_TIMEOUT_S (30 ms), so nothing else is held up for long, and the
# camera and audio threads spend their time blocked in C calls anyway.
# Set to 0 to disable and use Python's default.
SENSOR_TIMING_SWITCH_INTERVAL_S = 0.05

# Log the raw value robot_hat returned for every ping, plus the resolved
# GPIO numbers at startup. Invaluable when readings look wrong, noisy in
# normal use - leave False unless diagnosing.
SENSOR_LOG_RAW_PINGS = False


# ==========================================================================
# 4. CAMERA
# ==========================================================================

CAMERA_RESOLUTION = (640, 480)    # (width, height). Keep modest on a Pi 3.
CAMERA_WARMUP_S = 1.5             # let auto-exposure / white balance settle

# Picamera2 quirk: the libcamera format named "RGB888" actually hands back
# bytes in B, G, R order, which is exactly what OpenCV expects. So we ask for
# "RGB888" and pass the array straight to OpenCV.
CAMERA_FORMAT = "RGB888"

# If the live preview shows swapped colours (blue people, orange sky),
# flip this to True.
CAMERA_SWAP_RED_BLUE = False

WINDOW_NAME = "Navigation Headband - Phase 1 Hardware Test"

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# THE CAMERA MUST NEVER BE ABLE TO STOP THE COLLISION WARNING
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# picam2.capture_array() BLOCKS until a frame arrives. It used to be the
# first statement in the main loop, with the ultrasonic reading and the
# beep decision after it - so a camera that stopped delivering frames froze
# the warning path completely, even though the ultrasonic thread was still
# measuring perfectly well.
#
# Capture now happens on its own thread and the main loop takes whatever
# the newest frame is, without waiting. A stalled camera then costs us the
# picture and Gemini, and nothing else.
#
# How long a frame may be reused before we call it stale. The HUD and
# Gemini ignore frames older than this; the beeps do not care either way.
CAMERA_FRAME_MAX_AGE_S = 1.0

# Say so, loudly and once, if no frame arrives for this long. This is the
# log line that identifies a camera stall instead of leaving you guessing
# why nothing responds.
CAMERA_STALL_WARN_S = 5.0

# How long the capture thread waits before retrying after a failed read.
CAMERA_RETRY_S = 0.5

# Headless mode has no cv2.waitKey(1), which in the preview loop happens to
# yield the GIL for about a millisecond on every frame. Without a yield the
# main loop can spin flat out and starve the ultrasonic pulse-timing thread.
# This is the headless loop's explicit equivalent. At 5 ms it caps the loop
# at 200 Hz, far above any camera frame rate, so it costs nothing.
HEADLESS_LOOP_YIELD_S = 0.005


# ==========================================================================
# 5. AUDIO / BEEP
# ==========================================================================
# The beep is generated locally the first time you run the program and saved
# as a WAV file. No internet connection is ever needed.

# --- DANGER beep: the urgent one, repeated while under 25 cm -------------
BEEP_FREQUENCY_HZ = 1000      # pitch of the danger beep
BEEP_DURATION_S = 0.12        # length of one beep
BEEP_VOLUME = 0.6             # 0.0 .. 1.0, baked into the WAV file itself
BEEP_SAMPLE_RATE = 44100      # CD quality
BEEP_CHANNELS = 2             # 2 = stereo, identical tone in left and right

BEEP_WAV_PATH = PROJECT_ROOT / "assets" / "beep.wav"

# --- WARNING tone: the subtle one, played once on entering 25-50 cm ------
# Deliberately lower and quieter than the danger beep so the two are easy
# to tell apart by ear without looking at the screen.
WARNING_TONE_FREQUENCY_HZ = 660
WARNING_TONE_DURATION_S = 0.18
WARNING_TONE_VOLUME = 0.35

WARNING_TONE_WAV_PATH = PROJECT_ROOT / "assets" / "warning_tone.wav"

# Play one short beep during startup so you can confirm the headphones work.
PLAY_STARTUP_TEST_BEEP = True

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# WHICH OUTPUT DO THE BEEPS COME OUT OF?
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# With a Robot HAT fitted there can be TWO outputs on the Pi:
#
#   1. the Raspberry Pi 3's own 3.5mm jack   <- your stereo headphones
#   2. the Robot HAT's onboard I2S speaker   <- mono, soldered to the board
#
# The Robot HAT has no headphone socket of its own, so the headphones go in
# the Pi's jack. But if you have run SunFounder's i2samp.sh speaker script,
# it can make the HAT's I2S speaker the DEFAULT output - and then the beeps
# come out of the little onboard speaker instead of your headphones.
#
# Leave this as None to use whatever the system default is. If the beeps
# come out of the wrong place, set it to an ALSA device name. List the
# available names on the Pi with:
#
#     aplay -L | grep -i -E 'headphone|card|hw:'
#
# Typical value for the Pi's own analogue jack:
#
#     AUDIO_DEVICE = "plughw:CARD=Headphones,DEV=0"
#     AUDIO_DEVICE = "plughw:1,0"
#
# NOTE ON SIMULTANEOUS SOUNDS: danger beeps and spoken guidance can now
# play at the same time. A bare "plughw:" device is exclusive - one process
# holds it - so a beep may be skipped while a phrase is playing. If you want
# them properly mixed, name a device that supports it:
#
#     export AUDIO_DEVICE=plug:dmix:1,0
#
# Beeps that lose the race are simply skipped and retried on the next
# interval; the warning keeps going either way.
#
# It can also be set from the shell, which is usually easier while you are
# still working out which device is which:
#
#     export AUDIO_DEVICE=plughw:1,0
#     python3 main.py
#
# The environment is read HERE, at import time, and is the default for the
# value below. That matters: previously this was a hardcoded None and the
# environment was ignored entirely, so `export AUDIO_DEVICE=...` had no
# effect and every sound went to the system default output.
#
# Note that an export is needed - putting AUDIO_DEVICE in .env.local is too
# late, because .env.local is only read once the Gemini worker starts, well
# after this module has been imported and the audio devices opened.
#
# Find the exact name with:   aplay -l    and    aplay -L

AUDIO_DEVICE = os.environ.get("AUDIO_DEVICE", "").strip() or None


# ==========================================================================
# 6. GEMINI VISION  (Phase 2)
# ==========================================================================
# Gemini is strictly an ENHANCEMENT layer. The ultrasonic sensor and the
# local beeps are the safety system and never wait on it. If the network is
# down, the API errors, or a reply takes too long, the device keeps behaving
# exactly like Phase 1 and simply shows no description.

GEMINI_ENABLED = True

# Cheapest current model that does image UNDERSTANDING (image in, text out).
# Note: models with an "-image" suffix are image GENERATORS - not this.
GEMINI_MODEL = "gemini-3.5-flash-lite"

# --- API key -------------------------------------------------------------
# The key is read from the environment. vision.py also loads .env.local into
# the environment at startup so you do not have to export it by hand.
# The key is never hardcoded, never printed and never committed
# (.gitignore already covers .env and .env.*).
GEMINI_API_KEY_ENV = "GEMINI_API_KEY"
ENV_FILE_PATH = PROJECT_ROOT / ".env.local"

# --- When to ask -----------------------------------------------------------
# One analysis is requested when an obstacle moves into a CLOSER band
# (CAUTION, WARNING or DANGER). Standing still in a band asks nothing more.
# This single global cooldown is what stops repeated requests; it is
# deliberately SEPARATE from WARNING_TONE_MIN_GAP_S, which only governs the
# local tone.
GEMINI_COOLDOWN_S = 5.0

# Beyond band changes, two more things are worth a fresh look:
#
# 1. The obstacle distance changed substantially - the scene is different
#    enough that the old description may no longer apply. Must be well
#    above ultrasonic jitter (a few cm) so noise cannot trigger it.
GEMINI_DISTANCE_CHANGE_CM = 25.0
#
# 2. An obstacle has been sitting there long enough that a refreshed
#    description is useful (you may have turned your head). Set to 0 to
#    disable periodic refresh entirely.
GEMINI_REFRESH_S = 15.0

# --- Relevance, not just age ----------------------------------------------
# A slow reply is not automatically a WRONG reply. "Chair ahead, move right"
# that took 6 seconds is still correct if the chair is still 45 cm ahead; it
# is only wrong if the scene has actually changed. So acceptance is decided
# by relevance, and age is only the last-resort backstop.
#
# A reply is REJECTED if any of these is true:
#
#   1. a newer request has since been made          (newest always wins)
#   2. the obstacle is gone       - status is now SAFE/UNKNOWN
#   3. the distance moved by more than this much since the image was taken
GEMINI_RELEVANCE_DISTANCE_CM = 30.0
#   4. the reply is older than this on arrival - a hung-socket backstop,
#      deliberately generous because rules 1-3 do the real work
GEMINI_ACCEPT_MAX_AGE_S = 12.0

# How long an ACCEPTED description stays on the HUD / is considered current.
# This is a display window only; it no longer gates acceptance.
GEMINI_RESULT_MAX_AGE_S = 8.0

# --- Timeouts --------------------------------------------------------------
# Two layers. The SDK timeout asks the HTTP client to give up, and the worker
# deadline is the wall-clock backstop that discards a reply arriving after a
# hung socket finally returns. The deadline is the real guarantee.
#
# Gemini enforces a MINIMUM deadline of 10 seconds on generateContent. Asking
# for less is rejected before the model is even reached:
#
#     400 INVALID_ARGUMENT: Manually set deadline 8s is too short.
#                           Minimum allowed deadline is 10s.
#
# So GEMINI_REQUEST_TIMEOUT_S must never go below GEMINI_MIN_TIMEOUT_S.
# vision.py clamps it as a safety net, but set it correctly here.
GEMINI_MIN_TIMEOUT_S = 10.0

# 20s gives image analysis comfortable headroom on a Pi 3 over Wi-Fi.
GEMINI_REQUEST_TIMEOUT_S = 20.0

# Must be >= GEMINI_REQUEST_TIMEOUT_S, otherwise the worker would abandon a
# reply while the SDK was still legitimately waiting for it, and the timeout
# above would never actually come into play.
GEMINI_DEADLINE_S = 25.0

# --- Image / latency -----------------------------------------------------
# Token cost is flat: anything up to 768x768 is a single 258-token tile, so
# 640x480 and 512x384 cost exactly the same to the model. UPLOAD time is not
# flat though, and on a Pi 3's Wi-Fi the bytes on the wire are a real part of
# the round trip - so we downscale before encoding. Set to None to send the
# camera frame at full resolution.
GEMINI_SEND_RESOLUTION = (512, 384)
GEMINI_JPEG_QUALITY = 75

# Cap the reply.
#
# This must be generous, NOT tight. On Gemini 3 models thinking cannot be
# turned off, and thinking tokens are charged against this same output
# budget - so a small cap can be entirely consumed by hidden reasoning,
# leaving an empty description. The prompt is what keeps the visible answer
# to six words; this is only a runaway guard.
GEMINI_MAX_OUTPUT_TOKENS = 512

# --- thinking ------------------------------------------------------------
# Hidden "thinking" tokens are mostly latency for a six-word answer, so we
# ask for as little of it as the model allows. HOW you ask depends on the
# model generation, and getting it wrong is rejected outright:
#
#   Gemini 2.5:  thinking_budget=0            disables thinking entirely
#   Gemini 3  :  thinking_level="LOW"         thinking CANNOT be disabled;
#                                             sending thinking_budget gives
#                                             400 INVALID_ARGUMENT
#
# vision.py picks the right form from GEMINI_MODEL. Set this to None to send
# no thinking option at all and take the model's default.
GEMINI_THINKING_LEVEL = "LOW"

# Kept for the Gemini 2.5 style models only - see above. Has no effect on
# Gemini 3, where thinking is always on.
GEMINI_DISABLE_THINKING = True

# Make one tiny throwaway call at startup so DNS, TLS and the connection
# pool are already warm when the first real obstacle appears. Costs about
# $0.00001 and removes handshake time from the first useful request.
GEMINI_PREWARM = True

# --- Output ----------------------------------------------------------------
# Roughly ten words. Generous enough that a useful phrase such as
# "Table ahead, path clear on the right." is never cut off mid-sentence,
# which would be worse than useless over TTS.
GEMINI_MAX_DESCRIPTION_CHARS = 90

# Do not reprint the same error more often than this (seconds). Keeps a
# flapping network from filling the terminal.
GEMINI_ERROR_REPEAT_S = 30.0

# Log the model's reply EXACTLY as it arrived, before any cleanup, as
#
#     GEMINI RAW: 'Person ahead, slightly left.'
#     AI ACCEPTED: Person ahead, slightly left.
#
# Printed as a repr so stray newlines, quotes and padding are visible.
# Comparing the two lines tells you immediately whether a disappointing
# description came from the model or from our own post-processing.
GEMINI_LOG_RAW = True

# The prompt. {distance_cm} is filled in from the ULTRASONIC reading, so the
# model never has to guess distance.
#
# The sensor ALREADY tells the wearer that something is close. The camera's
# entire job is to add what the sensor cannot know: WHAT the thing is.
# So identification is the primary instruction here, and the generic
# fallback is deliberately made unattractive.
#
# An earlier version of this prompt ended with "If the frame does not
# clearly show which way is safe... reply exactly: Obstacle ahead." At
# 25-50 cm an obstacle usually fills the frame, so that condition was
# almost always true and the model took the sanctioned generic answer
# nearly every time. The fix is to separate the two kinds of uncertainty:
# not knowing which WAY to move is common and fine (just omit the
# suggestion), whereas not knowing WHAT the object is should be rare.
GEMINI_PROMPT = (
    "You are the camera of a navigation aid for a blind user.\n\n"
    "An ultrasonic sensor has already measured something {distance_cm} cm "
    "directly ahead, so the wearer KNOWS an obstacle is there. That reading "
    "is accurate - never estimate or mention distance.\n\n"
    "Your job is the one thing the sensor cannot do: say WHAT it is.\n\n"
    "Answer these in order:\n"
    "1. WHAT is the most relevant object or hazard in the travel path? "
    "Name it specifically - person, car, bicycle, chair, table, wall, "
    "doorway, stairs, curb, pole, sign, bin, fence, hedge, counter, "
    "trolley, glass door.\n"
    "2. WHERE is it - on the left, directly ahead, or on the right?\n"
    "3. WHAT should the wearer do? Add a short movement suggestion ONLY if "
    "the image clearly shows a clear side. If it does not, simply leave the "
    "suggestion out - still name the object and its direction.\n\n"
    "Reply with ONE spoken phrase of about three to ten words. No distance, "
    "no commentary, no explanation, no lists.\n\n"
    'Good replies: "Person ahead, slightly left." "Car ahead on your right." '
    '"Chair directly ahead, move left." '
    '"Table ahead, path clear on the right." '
    '"Doorway ahead on the left." "Wall directly ahead, turn right." '
    '"Stairs going down ahead." "Pole ahead, slightly right."\n\n'
    "IMPORTANT: naming the object is the whole point. Do not reply with a "
    "generic phrase such as \"Obstacle ahead\" when the image lets you "
    "identify the thing. Even a partial identification - a wall, a doorway, "
    "furniture, a vehicle, a person - is far more useful than a generic "
    "word. Being unable to suggest a direction is NOT a reason to withhold "
    "the object name.\n\n"
    "Equally, do not guess an object you cannot actually see. If the frame "
    "is genuinely too dark, too blurred or too close to identify anything, "
    'reply exactly: "Unknown object directly ahead."\n\n'
    "Do not identify who anyone is. Do not describe appearance, clothing, "
    "age, gender, race or any other personal characteristic - "
    '"Person ahead" is the correct level of detail for a human.\n\n'
    'If nothing is actually blocking the path, reply exactly: "Path clear."'
)


# ==========================================================================
# 7. SPOKEN NAVIGATION  (Phase 3)
# ==========================================================================
# Accepted Gemini guidance is spoken through the same headphones as the
# beeps, using a local offline text-to-speech engine. No network, no cloud
# TTS, nothing added to the Gemini round trip.
#
# The beep system is completely untouched by this. Speech runs on its own
# thread so a 2-second phrase can never delay a danger beep.

SPEECH_ENABLED = True

# Words per minute. Assistive speech wants to be brisk but intelligible;
# espeak's default 175 is a reasonable middle.
SPEECH_RATE_WPM = 170

# espeak amplitude, 0-200. 100 is its default.
SPEECH_VOLUME = 110

# Do not repeat identical guidance within this many seconds. Stops "Person
# ahead, left." being spoken over and over while you stand in a doorway.
SPEECH_DUPLICATE_GAP_S = 12.0

# SAFETY: the DANGER band no longer silences speech.
#
# The repeated urgent beeps are driven straight from the ultrasonic reading
# and always continue - they are the collision warning and never wait for
# anything. But accepted Gemini guidance is allowed to speak over them,
# because "Table leg ahead, move left." is exactly what you most want to
# hear at 20 cm. Guidance is never discarded merely for arriving while the
# distance happens to be in the DANGER band.
#
# To keep both intelligible, the danger beeps are SPACED OUT - not silenced,
# not quietened - while a phrase is being spoken. The interval is multiplied
# by this factor for the duration of the phrase only.
#
# 1.0 disables spacing entirely (beeps keep their normal urgent rhythm).
# The result is clamped so the beeps can never stop or become slower than
# BEEP_MAX_INTERVAL_WHILE_SPEAKING_S.
# While a phrase is being spoken, HOLD the beeps rather than letting them
# fight for the sound card.
#
# A "plughw:" device is a DIRECT hardware device: exclusive, one process at
# a time. The beeper spawns `aplay` every 0.15 s in DANGER, and speech needs
# the same device for one or two seconds - so whichever loses the race gets
# "Device or resource busy" and produces silence. That is precisely why
# accepted guidance was never heard at close range while the beeps worked.
#
# With this True the beeper skips its beeps for the duration of a phrase and
# resumes the instant it finishes, which is the "pause the beeps while TTS
# speaks, then resume" behaviour. The pause lasts only as long as the
# phrase, and the spoken guidance IS the warning during that window.
#
# Set to False if you would rather have both at once - but then point
# AUDIO_DEVICE at a mixing device or you will simply get dropouts:
#     export AUDIO_DEVICE=plug:dmix:1,0
BEEP_PAUSE_WHILE_SPEAKING = True

# Hard ceiling on how long the beeps will ever stand aside for speech.
# No phrase we speak lasts anywhere near this long, so if the flag is still
# set after this the flag itself is wrong - and a stuck flag must NEVER be
# able to silence the collision warning indefinitely. This is the backstop
# that guarantees the danger beep always comes back.
SPEECH_MAX_HOLD_S = 6.0

BEEP_SPACING_WHILE_SPEAKING = 2.0
BEEP_MAX_INTERVAL_WHILE_SPEAKING_S = 0.40

# Speech can still be muted explicitly (SpeechController.set_muted), which
# is what shutdown uses. Nothing in the navigation logic mutes it.

# Longest phrase we will speak. Anything longer is truncated - assistive
# audio must stay short. Kept in step with GEMINI_MAX_DESCRIPTION_CHARS so
# an accepted description is never clipped on its way to the voice.
SPEECH_MAX_CHARS = 90

# Spoken once at startup, as the real playback test. If you do not hear
# this in your headphones, the routing is wrong and startup will say so.
SPEECH_STARTUP_PHRASE = "Sense ready."


# ========================================================================== 
# 8. VOICE ASSISTANT (local wake word/VAD, Gemini transcription + answers)
# ========================================================================== 
# Environment values are read here so main.py and systemd use one source of
# truth.  An empty input device means ALSA's default capture device; unlike
# AUDIO_DEVICE, it is never guessed from a card number.
ASSISTANT_ENABLED = os.environ.get("ASSISTANT_ENABLED", "1").lower() not in (
    "0", "false", "no", "off")
ASSISTANT_WAKE_PHRASE = os.environ.get("ASSISTANT_WAKE_PHRASE", "hey sense")
MIC_DEVICE = os.environ.get("MIC_DEVICE", "").strip() or None
ASSISTANT_SAMPLE_RATE = 16000
ASSISTANT_CHUNK_MS = 100
ASSISTANT_PRE_SPEECH_BUFFER_S = float(os.environ.get(
    "ASSISTANT_PRE_SPEECH_BUFFER_S", "0.55"))
ASSISTANT_SILENCE_TIMEOUT_S = float(os.environ.get(
    "ASSISTANT_SILENCE_TIMEOUT_S", "1.6"))
ASSISTANT_SPEECH_START_TIMEOUT_S = float(os.environ.get(
    "ASSISTANT_SPEECH_START_TIMEOUT_S", "8.0"))
ASSISTANT_MAX_RECORDING_S = float(os.environ.get(
    "ASSISTANT_MAX_RECORDING_S", "18.0"))
ASSISTANT_MIN_SPEECH_SECONDS = float(os.environ.get(
    "ASSISTANT_MIN_SPEECH_SECONDS", "0.35"))
ASSISTANT_ENERGY_THRESHOLD = int(os.environ.get(
    "ASSISTANT_ENERGY_THRESHOLD", "180"))
ASSISTANT_WAKE_THRESHOLD = float(os.environ.get(
    "ASSISTANT_WAKE_THRESHOLD", "1e-18"))
ASSISTANT_GEMINI_MODEL = os.environ.get(
    "ASSISTANT_GEMINI_MODEL", GEMINI_MODEL)
ASSISTANT_MAX_HISTORY_TURNS = int(os.environ.get(
    "ASSISTANT_MAX_HISTORY_TURNS", "3"))
ASSISTANT_SYSTEM_PROMPT = (
    "You are Sense, a concise voice assistant inside an assistive wearable. "
    "Respond naturally for spoken audio. Keep ordinary answers brief unless "
    "the user asks for more detail. Do not use markdown."
)
ASSISTANT_STT_PROMPT = (
    "Transcribe the user's spoken request accurately. Return only the words "
    "spoken by the user. Do not answer the request, describe the audio, add "
    "timestamps, or add commentary."
)
ASSISTANT_ACK_TONE = "warning"
