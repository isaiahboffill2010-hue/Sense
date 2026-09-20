"""
alerts.py
=========

Turns a distance in centimetres into:

  * a status word  (SAFE / CAUTION / WARNING / DANGER / UNKNOWN)
  * a beep interval in seconds (or None for "do not beep")
  * a colour to draw it in

This module is pure arithmetic - no hardware, no threads - so it is easy to
read and easy to change.
"""

import config

# Status words
SAFE = "SAFE"
CAUTION = "CAUTION"
WARNING = "WARNING"
DANGER = "DANGER"
UNKNOWN = "UNKNOWN"   # no valid reading yet (sensor off, failing, or starting)

# OpenCV colours are (Blue, Green, Red), not (R, G, B).
STATUS_COLORS = {
    SAFE: (60, 200, 60),       # green
    CAUTION: (40, 215, 255),   # yellow
    WARNING: (20, 140, 255),   # orange
    DANGER: (40, 40, 255),     # red
    UNKNOWN: (170, 170, 170),  # grey
}


def classify(distance_cm):
    """Return the status word for a distance in cm.

    `distance_cm` may be None, which means "we have no valid reading",
    and that deliberately maps to UNKNOWN rather than to SAFE. We never
    invent a safe-looking value when the sensor is not answering.
    """
    if distance_cm is None:
        return UNKNOWN
    if distance_cm > config.SAFE_DISTANCE_CM:
        return SAFE
    if distance_cm >= config.CAUTION_DISTANCE_CM:
        return CAUTION
    if distance_cm >= config.WARNING_DISTANCE_CM:
        return WARNING
    return DANGER


def beep_interval_for(status):
    """Seconds between beeps for a status, or None to stay silent.

    UNKNOWN is silent on purpose: a broken sensor must not fake an alarm.
    """
    if status == CAUTION:
        return config.BEEP_INTERVAL_CAUTION_S
    if status == WARNING:
        return config.BEEP_INTERVAL_WARNING_S
    if status == DANGER:
        return config.BEEP_INTERVAL_DANGER_S
    return None


def color_for(status):
    """BGR colour tuple used by the on-screen overlay."""
    return STATUS_COLORS.get(status, STATUS_COLORS[UNKNOWN])
