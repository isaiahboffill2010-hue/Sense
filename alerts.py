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


def proximity_level(distance_cm, closing_rate_cm_s=None):
    if distance_cm is None:
        return "UNKNOWN"
    if (closing_rate_cm_s is not None and
            closing_rate_cm_s >= config.APPROACH_WARNING_RATE_CM_S and
            distance_cm <= config.APPROACH_WARNING_DISTANCE_CM):
        return "APPROACH"
    if distance_cm > config.PROXIMITY_TRACK_DISTANCE_CM:
        return "TRACK"
    if distance_cm >= config.PROXIMITY_SLOW_DISTANCE_CM:
        return "SLOW"
    if distance_cm >= config.PROXIMITY_FAST_DISTANCE_CM:
        return "FAST"
    return "DANGER"


def beep_interval_for(distance_cm, closing_rate_cm_s=None):
    """Return a continuous local beep interval, or None when tracking only."""
    if isinstance(distance_cm, str):
        return config.BEEP_INTERVAL_DANGER_S if distance_cm == DANGER else None
    if distance_cm is None:
        return None
    if (closing_rate_cm_s is not None and
            closing_rate_cm_s >= config.APPROACH_WARNING_RATE_CM_S and
            distance_cm <= config.APPROACH_WARNING_DISTANCE_CM and
            distance_cm > config.PROXIMITY_TRACK_DISTANCE_CM):
        return config.PROXIMITY_SLOW_INTERVAL_S
    if distance_cm > config.PROXIMITY_TRACK_DISTANCE_CM:
        return None
    if distance_cm >= config.PROXIMITY_SLOW_DISTANCE_CM:
        fraction = ((config.PROXIMITY_TRACK_DISTANCE_CM - distance_cm) /
                    (config.PROXIMITY_TRACK_DISTANCE_CM -
                     config.PROXIMITY_SLOW_DISTANCE_CM))
        return config.PROXIMITY_SLOW_INTERVAL_S - fraction * (
            config.PROXIMITY_SLOW_INTERVAL_S - config.PROXIMITY_FAST_INTERVAL_S)
    if distance_cm >= config.PROXIMITY_FAST_DISTANCE_CM:
        fraction = ((config.PROXIMITY_SLOW_DISTANCE_CM - distance_cm) /
                    (config.PROXIMITY_SLOW_DISTANCE_CM -
                     config.PROXIMITY_FAST_DISTANCE_CM))
        return config.PROXIMITY_FAST_INTERVAL_S - fraction * (
            config.PROXIMITY_FAST_INTERVAL_S - config.PROXIMITY_DANGER_INTERVAL_S)
    return config.PROXIMITY_DANGER_INTERVAL_S


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
    ["status", "repeat_interval", "play_warning_tone", "request_ai",
     "ai_reason", "proximity_level", "closing_rate_cm_s"],
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
        self._last_ai_distance_cm = None
        self._samples = collections.deque()
        self._filtered_distance_cm = None
        self._closing_rate_cm_s = None
        self._last_log_at = None
        self._last_log_level = None
        # Injectable clock so tests do not have to sleep in real time.
        self._now = now or time.monotonic

    @property
    def status(self):
        return self._status

    def update(self, distance_cm):
        raw_distance = distance_cm
        filtered_distance = self._filter_distance(distance_cm)
        self._log_proximity(filtered_distance)
        previous = self._status
        status = classify_with_hysteresis(raw_distance, previous)
        self._status = status

        # The tone fires on the TRANSITION into WARNING, not while sitting
        # in it. That covers both replay rules on its own: re-entry from
        # CAUTION (moved away then came back) and re-entry from DANGER
        # (came closer then backed off) are both transitions.
        entering_warning = status == WARNING and previous != WARNING
        play_tone = entering_warning and self._warning_tone_is_armed()
        if play_tone:
            self._last_warning_tone_at = self._now()

        ai_reason = self._ai_reason(status, previous, raw_distance)
        if ai_reason:
            self._last_ai_request_at = self._now()
            self._last_ai_distance_cm = distance_cm

        return AlertDecision(
            status=status,
            repeat_interval=beep_interval_for(
                filtered_distance, self._closing_rate_cm_s),
            play_warning_tone=play_tone,
            request_ai=bool(ai_reason),
            ai_reason=ai_reason,
            proximity_level=proximity_level(
                filtered_distance, self._closing_rate_cm_s),
            closing_rate_cm_s=self._closing_rate_cm_s,
        )

    @property
    def closing_rate_cm_s(self):
        return self._closing_rate_cm_s

    def _filter_distance(self, distance_cm):
        now = self._now()
        if distance_cm is None:
            self._closing_rate_cm_s = None
            return None
        if (self._filtered_distance_cm is not None and
                abs(distance_cm - self._filtered_distance_cm) >
                config.APPROACH_OUTLIER_CM):
            return self._filtered_distance_cm
        self._samples.append((now, float(distance_cm)))
        cutoff = now - config.APPROACH_HISTORY_S
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()
        self._filtered_distance_cm = float(distance_cm)
        self._closing_rate_cm_s = None
        if len(self._samples) >= config.APPROACH_MIN_SAMPLES:
            first_time, first_distance = self._samples[0]
            last_time, last_distance = self._samples[-1]
            elapsed = last_time - first_time
            samples = list(self._samples)
            decreases = sum(
                left > right + 1.0
                for (_, left), (_, right) in zip(samples, samples[1:]))
            if elapsed > 0 and decreases >= config.APPROACH_MIN_SAMPLES - 1:
                self._closing_rate_cm_s = max(
                    0.0, (first_distance - last_distance) / elapsed)
        return self._filtered_distance_cm

    def _log_proximity(self, distance_cm):
        level = proximity_level(distance_cm, self._closing_rate_cm_s)
        now = self._now()
        if (level == self._last_log_level and self._last_log_at is not None and
                now - self._last_log_at < config.ALERT_LOG_INTERVAL_S):
            return
        self._last_log_at = now
        self._last_log_level = level
        if distance_cm is None:
            print("DISTANCE: unavailable | PROXIMITY LEVEL: UNKNOWN", flush=True)
            return
        rate = ("n/a" if self._closing_rate_cm_s is None else
                "{:.0f} cm/s".format(self._closing_rate_cm_s))
        interval = beep_interval_for(distance_cm, self._closing_rate_cm_s)
        print("DISTANCE: {:.0f} cm | CLOSING RATE: {} | PROXIMITY LEVEL: {} | "
              "BEEP INTERVAL: {}".format(
                  distance_cm, rate, level,
                  "silent" if interval is None else "{:.2f}s".format(interval)),
              flush=True)

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
    def _ai_reason(self, status, previous, distance_cm):
        """Why a fresh Gemini look is warranted, or None to stay quiet.

        Returns a short reason string rather than a bare bool, so the
        terminal log and the tests can tell the three triggers apart.

        There are three, all gated by the same single global cooldown:

        "band"      An obstacle moved into a CLOSER band. Driving this off
                    severity rather than naming one band matters: readings
                    arrive every ~90 ms and walking pace covers ~13 cm in
                    that time, so a fast approach - or just turning your
                    head - can jump SAFE -> WARNING or SAFE -> DANGER and
                    skip a band entirely. Keying on any increase catches
                    those. In an ordinary walk-up only the first entry
                    reaches the API; the cooldown absorbs the rest.

        "moved"     The distance changed substantially without changing
                    band. The threshold is far above ultrasonic jitter, so
                    noise cannot trigger it, but walking from 95 cm to 60 cm
                    is a genuinely different scene.

        "refresh"   An obstacle has persisted long enough that the old
                    description may no longer reflect where you are looking.

        Moving AWAY never asks on its own: backing out of DANGER into
        WARNING is a decrease in severity.
        """
        approaching = (self._closing_rate_cm_s is not None and
                       self._closing_rate_cm_s >= config.APPROACH_WARNING_RATE_CM_S)
        if status not in (CAUTION, WARNING, DANGER) and not (
                approaching and distance_cm is not None and
                distance_cm <= config.APPROACH_WARNING_DISTANCE_CM):
            return None
        if not self._ai_is_armed():
            return None

        if approaching and distance_cm <= config.APPROACH_WARNING_DISTANCE_CM:
            return "approaching obstacle"

        if SEVERITY[status] > SEVERITY[previous]:
            return "band"

        # From here on the band is unchanged (or improved but still a
        # tracked band), so only a real change of scene justifies a request.
        if distance_cm is not None and self._last_ai_distance_cm is not None:
            moved = abs(distance_cm - self._last_ai_distance_cm)
            if moved >= config.GEMINI_DISTANCE_CHANGE_CM:
                return "moved {:.0f}cm".format(moved)

        refresh = config.GEMINI_REFRESH_S
        if refresh > 0 and self._last_ai_request_at is not None:
            if (self._now() - self._last_ai_request_at) >= refresh:
                return "refresh"

        return None

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
