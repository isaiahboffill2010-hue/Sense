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
# 2. DISTANCE THRESHOLDS AND BEEP RATES
# ==========================================================================
# Status bands, in centimetres:
#
#     distance  > 100          -> SAFE     (no beep)
#     50 <= distance <= 100    -> CAUTION  (slow beep)
#     25 <= distance <  50     -> WARNING  (faster beep)
#     distance  < 25           -> DANGER   (very fast beep)

SAFE_DISTANCE_CM = 100.0      # above this -> SAFE
CAUTION_DISTANCE_CM = 50.0    # at/above this (and <= SAFE) -> CAUTION
WARNING_DISTANCE_CM = 25.0    # at/above this (and < CAUTION) -> WARNING
                              # below WARNING_DISTANCE_CM     -> DANGER

# Seconds between beeps for each band. Smaller number = faster beeping.
BEEP_INTERVAL_CAUTION_S = 1.00
BEEP_INTERVAL_WARNING_S = 0.45
BEEP_INTERVAL_DANGER_S = 0.15


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

BEEP_FREQUENCY_HZ = 1000      # pitch of the warning beep
BEEP_DURATION_S = 0.12        # length of one beep
BEEP_VOLUME = 0.6             # 0.0 .. 1.0, baked into the WAV file itself
BEEP_SAMPLE_RATE = 44100      # CD quality
BEEP_CHANNELS = 2             # 2 = stereo, identical tone in left and right

BEEP_WAV_PATH = PROJECT_ROOT / "assets" / "beep.wav"

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
