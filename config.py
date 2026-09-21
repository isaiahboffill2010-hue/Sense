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
Robot HAT DIGITAL PORT ("D2", "D3") rather than by raw pin number. That is
what the robot_hat library expects.

Each port is hard-wired by the HAT to one **BCM** (Broadcom) GPIO number -
not to a physical 1-40 header position:

    "D2"  ==  BCM GPIO27  ==  physical pin 13
    "D3"  ==  BCM GPIO22  ==  physical pin 15

Run `pinout` on the Raspberry Pi to see the full map for your board.
"""

from pathlib import Path

# Absolute path to this project folder (used for the generated beep file).
PROJECT_ROOT = Path(__file__).resolve().parent


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
#     "D0"  ->  GPIO17
#     "D1"  ->  GPIO4
#     "D2"  ->  GPIO27     <- TRIG (yellow wire)
#     "D3"  ->  GPIO22     <- ECHO (white wire)
#
# Sensor cable colours:
#
#     RED    VCC   -> red power pin of the D2 port
#     YELLOW TRIG  -> yellow signal pin of the D2 port
#     WHITE  ECHO  -> yellow signal pin of the D3 port
#     BLACK  GND   -> black ground pin of the D3 port
#
# Splitting the 4-wire sensor across two 3-pin ports like this is fine: all
# of the Robot HAT's digital ports share the same VCC and GND rails, so the
# sensor still gets power from D2 and ground from D3.
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

TRIG_PIN = "D2"   # Robot HAT digital port for TRIG  (yellow wire, GPIO27)
ECHO_PIN = "D3"   # Robot HAT digital port for ECHO  (white wire,  GPIO22)

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

AUDIO_DEVICE = None


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

# --- Staleness -------------------------------------------------------------
# Age is measured from the moment the IMAGE WAS CAPTURED, not from when the
# reply arrived. A description older than this is discarded rather than
# shown, so "Chair ahead." can never appear after the user has walked past
# the chair.
GEMINI_RESULT_MAX_AGE_S = 4.0

# --- Timeouts --------------------------------------------------------------
# Two layers. The SDK timeout asks the HTTP client to give up, and the worker
# deadline is the wall-clock backstop that discards a reply arriving after a
# hung socket finally returns. The deadline is the real guarantee.
GEMINI_REQUEST_TIMEOUT_S = 8.0
GEMINI_DEADLINE_S = 10.0

# --- Image ---------------------------------------------------------------
# 640x480 fits inside a single 768x768 tile, which costs 258 image tokens,
# so there is nothing to gain from downscaling further.
GEMINI_JPEG_QUALITY = 80

# --- Output ----------------------------------------------------------------
GEMINI_MAX_DESCRIPTION_CHARS = 60

# Do not reprint the same error more often than this (seconds). Keeps a
# flapping network from filling the terminal.
GEMINI_ERROR_REPEAT_S = 30.0

# The prompt. {distance_cm} is filled in with the measured distance.
GEMINI_PROMPT = (
    "You are the vision system of a navigation aid for a blind user. "
    "An obstacle was detected about {distance_cm} cm ahead.\n\n"
    "Reply with ONE short phrase of at most six words naming the most "
    "important navigation obstacle or hazard directly ahead.\n\n"
    "Prioritise: people, chairs, tables, walls, doors, stairs, curbs, "
    "poles, vehicles, pathways, and immediate trip or collision hazards.\n\n"
    "Do not identify who anyone is. Do not describe appearance, clothing, "
    "age, gender, race or any other personal characteristic. Do not add "
    "commentary, explanation or punctuation beyond a final full stop.\n\n"
    'Examples: "Person ahead." "Two people ahead." "Chair directly ahead." '
    '"Closed door ahead." "Stairs descending ahead." "Wall ahead."\n\n'
    'If there is no meaningful navigation obstacle, reply exactly: "Path clear."'
)
