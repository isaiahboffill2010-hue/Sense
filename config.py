"""
config.py
=========

Central configuration for the Phase 1 hardware test.

Edit THIS FILE (and only this file) to match your own wiring and preferences.
Nothing in here talks to hardware; it is plain data so it is safe to open and
read on any computer, including Windows.

--------------------------------------------------------------------------
GPIO NUMBERING
--------------------------------------------------------------------------
All pin numbers in this file are **BCM** (Broadcom) GPIO numbers, NOT the
physical 1-40 header positions.

    BCM 23  ==  physical pin 16
    BCM 24  ==  physical pin 18

Run `pinout` on the Raspberry Pi to see the mapping for your board.
"""

from pathlib import Path

# Absolute path to this project folder (used for the generated beep file).
PROJECT_ROOT = Path(__file__).resolve().parent


# ==========================================================================
# 1. HC-SR04 ULTRASONIC WIRING  <-- YOU MUST FILL THIS IN
# ==========================================================================
#
# These are intentionally left as None. The program will NOT guess your
# wiring: it will start, run the camera and the audio, and clearly report
#
#     Ultrasonic: NOT CONFIGURED
#
# until you set both pins below.
#
# Set them to the BCM numbers you actually wired, for example:
#
#     TRIG_PIN = 23        # example only - confirm against your own wiring
#     ECHO_PIN = 24        # example only - confirm against your own wiring
#
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# ELECTRICAL SAFETY - READ BEFORE WIRING ECHO
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# A standard HC-SR04 is powered from 5V and its ECHO pin drives roughly 5V.
# Raspberry Pi GPIO inputs are 3.3V logic and are NOT 5V tolerant.
#
# Do NOT connect a standard 5V ECHO output directly to a Pi GPIO pin.
#
# Put a resistor voltage divider (a common choice is 1k from ECHO to the
# GPIO, and 2k from that same GPIO node to GND) or a proper level shifter
# between ECHO and the Pi, UNLESS your specific module/breakout already
# performs 3.3V level shifting (some 3.3V-capable variants do).
#
# TRIG is an output from the Pi, so it does not need a divider.
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

TRIG_PIN = None   # BCM number of the pin wired to HC-SR04 TRIG
ECHO_PIN = None   # BCM number of the pin wired to the DIVIDED/SHIFTED ECHO

# lgpio needs to know which /dev/gpiochipN to open.
# Raspberry Pi 3 and 4 -> 0. (Pi 5 may need 4 on some OS versions.)
# If GPIO_CHIP fails to open, the program automatically tries 0 and 4.
GPIO_CHIP = 0


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

SENSOR_MIN_DISTANCE_CM = 2.0      # HC-SR04 datasheet minimum
SENSOR_MAX_DISTANCE_CM = 400.0    # HC-SR04 datasheet maximum

SENSOR_READ_INTERVAL_S = 0.06     # pause between measurement cycles (~16 Hz)
SENSOR_ECHO_TIMEOUT_S = 0.040     # 400 cm round trip is ~23 ms; 40 ms is safe
SENSOR_SETTLE_S = 0.002           # quiet time before firing TRIG
SENSOR_SAMPLES_PER_READING = 3    # median of N pings -> rejects random spikes
SENSOR_ERRORS_BEFORE_FAIL = 12    # consecutive failures before status -> FAIL
SPEED_OF_SOUND_CM_S = 34300.0     # ~20 degrees C


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
