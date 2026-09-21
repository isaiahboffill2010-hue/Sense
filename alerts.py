"""
alerts.py
=========

Turns a distance in centimetres into:

  * a status word  (SAFE / CAUTION / WARNING / DANGER / UNKNOWN)
  * a colour to draw it in
  * a decision about what to play: nothing, one subtle tone, or repeated
    beeps  (see AlertPolicy at the bottom)

This module is pure logic - no hardware, no threads - so it is easy to read,
easy to change, and fully testable on any computer.
"""

import collections
import time

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
    """Seconds between repeated beeps for a status, or None to stay silent.

    Only DANGER repeats. CAUTION and WARNING are silent bands - WARNING gets
    a single tone on entry instead, which AlertPolicy handles.

    UNKNOWN is silent on purpose: a broken sensor must not fake an alarm.
    """
    if status == DANGER:
        return config.BEEP_INTERVAL_DANGER_S
    return None


def color_for(status):
    """BGR colour tuple used by the on-screen overlay."""
    return STATUS_COLORS.get(status, STATUS_COLORS[UNKNOWN])


# ==========================================================================
# Alert state machine
# ==========================================================================
# How severe each band is. Used to tell "getting closer" (act immediately)
# from "backing away" (apply hysteresis before believing it).
SEVERITY = {UNKNOWN: -1, SAFE: 0, CAUTION: 1, WARNING: 2, DANGER: 3}

# What AlertPolicy.update() hands back to the main loop.
AlertDecision = collections.namedtuple(
    "AlertDecision",
    ["status", "repeat_interval", "play_warning_tone", "request_ai"],
)


def classify_with_hysteresis(distance_cm, previous_status):
    """Like classify(), but sticky when an obstacle moves away.

    Getting CLOSER is reported immediately - that is the safety-critical
    direction and must never be delayed. Moving further away only counts
    once the reading has cleared the band boundary by STATUS_HYSTERESIS_CM,
    which stops a few cm of sensor jitter from flipping bands every read.
    """
    if distance_cm is None:
        return UNKNOWN

    plain = classify(distance_cm)
    if previous_status is None or previous_status == UNKNOWN:
        return plain

    if SEVERITY[plain] >= SEVERITY[previous_status]:
        return plain

    # Backing away: re-classify as if the obstacle were still a little
    # closer than measured. If that still lands in a less severe band, the
    # move is real; otherwise stay where we are.
    relaxed = classify(distance_cm - config.STATUS_HYSTERESIS_CM)
    if SEVERITY[relaxed] < SEVERITY[previous_status]:
        return relaxed
    return previous_status


class AlertPolicy:
    """Decides what the device should sound like, one reading at a time.

    Call update() with each new distance; it returns an AlertDecision:

        status              band to display (hysteresis applied)
        repeat_interval     seconds between repeated beeps, or None
        play_warning_tone   True exactly once, on entering WARNING

    The policy is the only thing that remembers previous readings, which
    keeps the main loop stateless and makes this testable without hardware.
    """

    def __init__(self, now=None):
        self._status = UNKNOWN
        self._last_warning_tone_at = None
        self._last_ai_request_at = None
        # Injectable clock so tests do not have to sleep in real time.
        self._now = now or time.monotonic

    @property
    def status(self):
        return self._status

    def update(self, distance_cm):
        previous = self._status
        status = classify_with_hysteresis(distance_cm, previous)
        self._status = status

        # The tone fires on the TRANSITION into WARNING, not while sitting
        # in it. That covers both replay rules on its own: re-entry from
        # CAUTION (moved away then came back) and re-entry from DANGER
        # (came closer then backed off) are both transitions.
        entering_warning = status == WARNING and previous != WARNING
        play_tone = entering_warning and self._warning_tone_is_armed()
        if play_tone:
            self._last_warning_tone_at = self._now()

        request_ai = self._should_request_ai(status, previous)
        if request_ai:
            self._last_ai_request_at = self._now()

        return AlertDecision(
            status=status,
            repeat_interval=beep_interval_for(status),
            play_warning_tone=play_tone,
            request_ai=request_ai,
        )

    def _warning_tone_is_armed(self):
        """False if the tone sounded too recently to play again.

        Hysteresis alone cannot fully settle an obstacle hovering right on
        the 25 cm line, where jitter can genuinely cross both the entry and
        the exit threshold. This is the backstop that keeps such a case from
        turning into a stream of tones.
        """
        gap = config.WARNING_TONE_MIN_GAP_S
        if gap <= 0 or self._last_warning_tone_at is None:
            return True
        return (self._now() - self._last_warning_tone_at) >= gap

    # ---------------------------------------------------------- AI trigger
    def _should_request_ai(self, status, previous):
        """True when a fresh Gemini look is warranted.

        The rule is "an obstacle just moved into a CLOSER band": any step up
        in severity into CAUTION, WARNING or DANGER asks for one analysis.

        Driving it off severity rather than naming one band matters, because
        readings arrive roughly every 90 ms and a walking pace covers about
        13 cm in that time - so a fast approach, or simply turning your head,
        can jump SAFE -> WARNING or SAFE -> DANGER and skip a band entirely.
        Keying on any increase means those cases still get analysed.

        In the ordinary SAFE -> CAUTION -> WARNING -> DANGER walk-up, only
        the CAUTION entry actually reaches the API: the later steps follow
        within a second or two and the global cooldown absorbs them. That is
        the intended behaviour - CAUTION is where the camera framing is best
        and where there is the most time for a reply to come back.

        Moving AWAY never asks: backing out of DANGER into WARNING is a
        decrease in severity, so it returns False.
        """
        if status not in (CAUTION, WARNING, DANGER):
            return False
        if SEVERITY[status] <= SEVERITY[previous]:
            return False
        return self._ai_is_armed()

    def _ai_is_armed(self):
        """False while the global Gemini cooldown is still running.

        Deliberately independent of the warning-tone re-arm timer: that one
        exists to avoid annoying the user, this one exists to bound API
        usage. They are allowed to disagree, and when they do the local tone
        always wins - a tone with no description is fine, the reverse is not.
        """
        gap = config.GEMINI_COOLDOWN_S
        if gap <= 0 or self._last_ai_request_at is None:
            return True
        return (self._now() - self._last_ai_request_at) >= gap
