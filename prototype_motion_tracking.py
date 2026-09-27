#!/usr/bin/env python3
"""Phase 3 isolated visual object tracking and apparent-motion prototype.

No production integration, Gemini/cloud request, audio, navigation decision, or
per-object ultrasonic range assignment is made here.
"""
from __future__ import annotations

import argparse
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
import statistics
import sys
import time
from typing import Iterable, Sequence

from prototype_vision import (
    DEFAULT_LABELS, DEFAULT_MODEL, Detection, LiteRTDetector, NewestFrameGate,
    SystemMonitor, format_metric, percentile,
)


HIGH_PRIORITY = frozenset({"person", "car", "truck", "bus", "motorcycle", "bicycle"})
STABLE, MOVING_LEFT, MOVING_RIGHT = "STABLE", "MOVING LEFT", "MOVING RIGHT"
APPROACHING, RECEDING, UNCERTAIN = "APPROACHING", "RECEDING", "UNCERTAIN"
CROSSING_LEFT, CROSSING_RIGHT, LOST = "CROSSING LEFT", "CROSSING RIGHT", "LOST"


def box_center(box: tuple[int, int, int, int]) -> tuple[float, float]:
    left, top, right, bottom = box
    return (left + right) / 2.0, (top + bottom) / 2.0


def box_area(box: tuple[int, int, int, int]) -> float:
    left, top, right, bottom = box
    return max(0, right - left) * max(0, bottom - top)


def iou(first: tuple[int, int, int, int], second: tuple[int, int, int, int]) -> float:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[2], second[2])
    bottom = min(first[3], second[3])
    intersection = max(0, right - left) * max(0, bottom - top)
    union = box_area(first) + box_area(second) - intersection
    return 0.0 if union <= 0 else intersection / union


def priority_for(label: str) -> str:
    return "HIGH" if label.lower() in HIGH_PRIORITY else "NORMAL"


@dataclass(frozen=True)
class Observation:
    timestamp: float
    center_x: float
    center_y: float
    area: float
    confidence: float
    position: str


@dataclass
class Track:
    track_id: int
    label: str
    class_id: int
    box: tuple[int, int, int, int]
    priority: str
    created_at: float
    last_seen_at: float
    confidence: float
    position: str
    history: deque[Observation]
    hit_times: deque[float]
    confirmed: bool = False
    motion: str = STABLE
    seen_this_update: bool = True
    was_lost: bool = False

    @property
    def display_id(self) -> str:
        return f"{self.label.upper()} #{self.track_id}"


class LightweightTracker:
    """Short-lived class-aware IoU + centre-distance object tracker."""
    def __init__(self, *, max_center_distance_px: float = 120.0, priority_center_distance_px: float = 200.0,
                 min_iou: float = .10, lost_timeout_s: float = 1.25,
                 priority_lost_timeout_s: float = 2.0, confirmation_hits: int = 2,
                 confirmation_window_s: float = .75, history_size: int = 6,
                 horizontal_dead_zone_px: float = 35.0, area_change_ratio: float = 1.25):
        self.max_center_distance_px = max_center_distance_px
        self.priority_center_distance_px = priority_center_distance_px
        self.min_iou = min_iou
        self.lost_timeout_s = lost_timeout_s
        self.priority_lost_timeout_s = priority_lost_timeout_s
        self.confirmation_hits = confirmation_hits
        self.confirmation_window_s = confirmation_window_s
        self.history_size = history_size
        self.horizontal_dead_zone_px = horizontal_dead_zone_px
        self.area_change_ratio = area_change_ratio
        self.tracks: list[Track] = []
        self._next_id: defaultdict[str, int] = defaultdict(int)
        self.created_count = 0
        self.expired_count = 0

    def _new_track(self, detection: Detection, now: float) -> Track:
        self._next_id[detection.label] += 1
        center_x, center_y = box_center(detection.box)
        track = Track(
            self._next_id[detection.label], detection.label, detection.class_id, detection.box,
            priority_for(detection.label), now, now, detection.confidence, detection.position,
            deque([Observation(now, center_x, center_y, box_area(detection.box), detection.confidence,
                               detection.position)], maxlen=self.history_size),
            deque([now], maxlen=self.confirmation_hits),
        )
        self.created_count += 1
        return track

    def _update_track(self, track: Track, detection: Detection, now: float) -> None:
        center_x, center_y = box_center(detection.box)
        reacquired = track.was_lost
        track.box = detection.box
        track.confidence = detection.confidence
        track.position = detection.position
        track.last_seen_at = now
        track.seen_this_update = True
        if reacquired:
            # Do not turn a blind interval into an invented huge movement.
            track.history.clear()
            track.motion = STABLE
        track.history.append(Observation(now, center_x, center_y, box_area(detection.box),
                                         detection.confidence, detection.position))
        track.hit_times.append(now)
        while track.hit_times and now - track.hit_times[0] > self.confirmation_window_s:
            track.hit_times.popleft()
        if len(track.hit_times) >= self.confirmation_hits:
            track.confirmed = True
        if not reacquired:
            track.motion = self._motion(track)

    @staticmethod
    def _predicted_center(track: Track, now: float) -> tuple[float, float]:
        history = list(track.history)
        current_x, current_y = box_center(track.box)
        if len(history) < 2:
            return current_x, current_y
        previous, last = history[-2], history[-1]
        elapsed = max(.001, last.timestamp - previous.timestamp)
        ahead = min(1.0, max(0.0, now - last.timestamp))
        return last.center_x + (last.center_x - previous.center_x) / elapsed * ahead, last.center_y + (last.center_y - previous.center_y) / elapsed * ahead

    def _match_score(self, track: Track, detection: Detection, now: float) -> float | None:
        if track.class_id != detection.class_id or track.label != detection.label:
            return None
        current_x, current_y = box_center(track.box)
        detected_x, detected_y = box_center(detection.box)
        predicted_x, predicted_y = self._predicted_center(track, now)
        current_distance = ((detected_x - current_x) ** 2 + (detected_y - current_y) ** 2) ** .5
        predicted_distance = ((detected_x - predicted_x) ** 2 + (detected_y - predicted_y) ** 2) ** .5
        tolerance = self.priority_center_distance_px if track.priority == "HIGH" else self.max_center_distance_px
        overlap = iou(track.box, detection.box)
        old_area, new_area = box_area(track.box), box_area(detection.box)
        size_ratio = 1.0 if not old_area or not new_area else min(old_area, new_area) / max(old_area, new_area)
        minimum_size_ratio = .25 if track.priority == "HIGH" else .35
        if size_ratio < minimum_size_ratio or (overlap < self.min_iou and min(current_distance, predicted_distance) > tolerance):
            return None
        # Prediction dominates when a moving track is briefly lost; confirmed
        # tracks get a small, bounded preference over a new tentative identity.
        score = (.25 * current_distance / tolerance + .45 * predicted_distance / tolerance +
                 .20 * (1.0 - overlap) + .10 * (1.0 - size_ratio))
        if track.confirmed:
            score -= .08
        return score

    def _is_duplicate_person(self, detection: Detection) -> bool:
        if detection.label != "person":
            return False
        for track in self.tracks:
            if track.label != "person" or not track.confirmed or not track.seen_this_update:
                continue
            if iou(track.box, detection.box) >= .65:
                return True
        return False

    def _motion(self, track: Track) -> str:
        history = list(track.history)
        if len(history) < 3:
            return STABLE
        first, last = history[0], history[-1]
        x_deltas = [later.center_x - earlier.center_x for earlier, later in zip(history, history[1:])]
        net_x = last.center_x - first.center_x
        meaningful = [value for value in x_deltas if abs(value) >= self.horizontal_dead_zone_px / 4.0]
        consistent_right = len(meaningful) >= 2 and all(value > 0 for value in meaningful)
        consistent_left = len(meaningful) >= 2 and all(value < 0 for value in meaningful)
        positions = [item.position for item in history]
        if consistent_right and abs(net_x) >= self.horizontal_dead_zone_px:
            if track.priority == "HIGH" and "LEFT" in positions and "CENTER" in positions and "RIGHT" in positions:
                return CROSSING_RIGHT
            return MOVING_RIGHT
        if consistent_left and abs(net_x) >= self.horizontal_dead_zone_px:
            if track.priority == "HIGH" and "RIGHT" in positions and "CENTER" in positions and "LEFT" in positions:
                return CROSSING_LEFT
            return MOVING_LEFT
        area_ratio = last.area / first.area if first.area > 0 else 1.0
        area_deltas = [later.area - earlier.area for earlier, later in zip(history, history[1:])]
        if area_ratio >= self.area_change_ratio and sum(value > 0 for value in area_deltas) >= 2:
            return APPROACHING
        if area_ratio <= 1.0 / self.area_change_ratio and sum(value < 0 for value in area_deltas) >= 2:
            return RECEDING
        if abs(net_x) < self.horizontal_dead_zone_px:
            return STABLE
        return UNCERTAIN

    def update(self, detections: Iterable[Detection], now: float) -> list[Track]:
        detections = list(detections)
        for track in self.tracks:
            track.was_lost = not track.seen_this_update
            track.seen_this_update = False
        pairs: list[tuple[float, int, int]] = []
        for track_index, track in enumerate(self.tracks):
            for detection_index, detection in enumerate(detections):
                score = self._match_score(track, detection, now)
                if score is not None:
                    pairs.append((score, track_index, detection_index))
        used_tracks: set[int] = set()
        used_detections: set[int] = set()
        for _, track_index, detection_index in sorted(pairs):
            if track_index in used_tracks or detection_index in used_detections:
                continue
            self._update_track(self.tracks[track_index], detections[detection_index], now)
            used_tracks.add(track_index)
            used_detections.add(detection_index)
        for index, detection in enumerate(detections):
            if index not in used_detections:
                if not self._is_duplicate_person(detection):
                    self.tracks.append(self._new_track(detection, now))
        surviving = []
        for track in self.tracks:
            timeout = self.priority_lost_timeout_s if track.priority == "HIGH" else self.lost_timeout_s
            if now - track.last_seen_at > timeout:
                self.expired_count += 1
            else:
                surviving.append(track)
        self.tracks = surviving
        return list(self.tracks)

    def visible_motion(self, track: Track) -> str:
        return track.motion if track.seen_this_update else LOST

    def tracks_for_display(self, debug: bool = False) -> list[Track]:
        """Default display is current confirmed objects, not stale clutter."""
        if debug:
            return list(self.tracks)
        return [track for track in self.tracks if track.confirmed and track.seen_this_update]


def forward_distance(snapshot: dict | None, max_age_s: float) -> tuple[float | None, str]:
    snapshot = snapshot or {}
    distance, age = snapshot.get("distance_cm"), snapshot.get("age_s")
    if not snapshot.get("healthy", False) or snapshot.get("out_of_range", False) or not isinstance(distance, (int, float)):
        return None, "DISTANCE UNAVAILABLE"
    if age is None or age > max_age_s:
        return None, "DISTANCE STALE"
    if distance > 100:
        return float(distance), "FAR"
    if distance >= 50:
        return float(distance), "NEAR"
    if distance >= 25:
        return float(distance), "CLOSE"
    return float(distance), "VERY CLOSE"


@dataclass
class MotionStats:
    started_at: float = field(default_factory=time.monotonic)
    inference_ms: list[float] = field(default_factory=list)
    tracking_ms: list[float] = field(default_factory=list)
    cpu: list[float] = field(default_factory=list)
    rss: list[float] = field(default_factory=list)
    temp: list[float] = field(default_factory=list)
    peak_tracks: int = 0

    def add(self, inference: float, tracking: float, system: dict, active_tracks: int) -> None:
        self.inference_ms.append(inference)
        self.tracking_ms.append(tracking)
        self.peak_tracks = max(self.peak_tracks, active_tracks)
        for key, values in (("cpu", self.cpu), ("rss", self.rss), ("temp", self.temp)):
            if system.get(key) is not None:
                values.append(float(system[key]))

    @staticmethod
    def _value(value: float | None, suffix: str = "") -> str:
        return "n/a" if value is None else f"{value:.1f}{suffix}"

    def summary(self, elapsed: float, tracker: LightweightTracker) -> str:
        enough = len(self.inference_ms) >= 5
        return "\n".join((
            "\n=== SENSE PHASE 3 BENCHMARK ===",
            f"Average detection FPS: {self._value(len(self.inference_ms) / elapsed if elapsed > 0 else None)}",
            f"Average inference: {self._value(statistics.fmean(self.inference_ms) if self.inference_ms else None, ' ms')}",
            f"P50 inference: {self._value(percentile(self.inference_ms, 50) if enough else None, ' ms')}",
            f"P95 inference: {self._value(percentile(self.inference_ms, 95) if enough else None, ' ms')}",
            f"Average tracking time: {self._value(statistics.fmean(self.tracking_ms) if self.tracking_ms else None, ' ms')}",
            f"P95 tracking time: {self._value(percentile(self.tracking_ms, 95) if len(self.tracking_ms) >= 5 else None, ' ms')}",
            f"Peak active tracks: {self.peak_tracks}",
            f"Tracks created: {tracker.created_count}",
            f"Tracks expired: {tracker.expired_count}",
            f"Average CPU: {self._value(statistics.fmean(self.cpu) if self.cpu else None, '%')}",
            f"Peak RAM: {self._value(max(self.rss) if self.rss else None, ' MiB')}",
            f"Maximum CPU temperature: {self._value(max(self.temp) if self.temp else None, ' C')}",
            "================================",
        ))


def draw_preview(frame, tracks: Iterable[Track], forward: float | None, forward_state: str, metrics: dict, cv2):
    height, width = frame.shape[:2]
    center_left, center_right = width // 3, 2 * width // 3
    overlay = frame.copy()
    cv2.rectangle(overlay, (center_left, 0), (center_right, height), (80, 180, 80), -1)
    cv2.addWeighted(overlay, .10, frame, .90, 0, frame)
    for boundary in (center_left, center_right):
        cv2.line(frame, (boundary, 0), (boundary, height), (90, 180, 90), 1)
    for track in tracks:
        left, top, right, bottom = track.box
        motion = track.motion if track.seen_this_update else LOST
        color = (0, 80, 255) if track.priority == "HIGH" else (50, 220, 50)
        if not track.seen_this_update:
            color = (110, 110, 110)
        cv2.rectangle(frame, (left, top), (right, bottom), color, 2)
        cv2.putText(frame, track.display_id, (left, max(18, top - 25)), cv2.FONT_HERSHEY_SIMPLEX, .52, color, 2)
        cv2.putText(frame, f"{track.position} | {motion}", (left, max(36, top - 7)), cv2.FONT_HERSHEY_SIMPLEX, .48, color, 2)
    forward_text = "FORWARD OBSTACLE: DISTANCE UNAVAILABLE" if forward is None else f"FORWARD OBSTACLE: {forward:.0f} cm ({forward_state})"
    lines = (
        forward_text,
        "AI {}ms | detection {}fps | tracking {}ms | active tracks {}".format(
            format_metric(metrics["inference"]), format_metric(metrics["detection_fps"]),
            format_metric(metrics["tracking"]), metrics["tracks"],
        ),
        "frame age {}ms | ultrasonic age {}ms | CPU {} RSS {} temp {}".format(
            format_metric(metrics["frame_age"]), format_metric(metrics["ultrasonic_age"]),
            format_metric(metrics["cpu"], "%"), format_metric(metrics["rss"], "MiB"), format_metric(metrics["temp"], "C"),
        ),
    )
    for index, text in enumerate(lines):
        y = 22 + index * 20
        cv2.putText(frame, text, (7, y), cv2.FONT_HERSHEY_SIMPLEX, .45, (0, 0, 0), 3)
        cv2.putText(frame, text, (7, y), cv2.FONT_HERSHEY_SIMPLEX, .45, (255, 255, 255), 1)
    return frame


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS)
    parser.add_argument("--confidence", type=float, default=.50)
    parser.add_argument("--max-detection-fps", type=float, default=5.0)
    parser.add_argument("--benchmark-seconds", type=float, default=0.0,
                        help="numeric duration; 0 runs until Q/Esc/Ctrl-C")
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--camera-width", type=int, default=640)
    parser.add_argument("--camera-height", type=int, default=480)
    parser.add_argument("--max-center-distance", type=float, default=120.0)
    parser.add_argument("--lost-timeout", type=float, default=1.50)
    parser.add_argument("--priority-lost-timeout", type=float, default=2.0)
    parser.add_argument("--confirmation-hits", type=int, default=2)
    parser.add_argument("--confirmation-window", type=float, default=.75)
    parser.add_argument("--horizontal-dead-zone", type=float, default=35.0)
    parser.add_argument("--area-change-ratio", type=float, default=1.25)
    parser.add_argument("--ultrasonic-max-age", type=float, default=.50)
    parser.add_argument("--no-preview", action="store_true")
    parser.add_argument("--debug-tracks", action="store_true", help="show tentative and LOST internal tracks")
    args = parser.parse_args(argv)
    if not 0 <= args.confidence <= 1 or args.max_detection_fps < 0 or args.benchmark_seconds < 0:
        parser.error("invalid confidence, FPS, or benchmark duration")
    if args.threads < 1 or args.confirmation_hits < 1 or min(args.max_center_distance, args.lost_timeout, args.priority_lost_timeout, args.confirmation_window, args.horizontal_dead_zone, args.ultrasonic_max_age) <= 0 or args.area_change_ratio <= 1:
        parser.error("tracking thresholds must be positive; area-change-ratio must exceed 1")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    for asset in (args.model, args.labels):
        if not asset.is_file():
            print(f"MISSING MODEL ASSET: {asset}\nRun: python3 download_prototype_model.py", file=sys.stderr)
            return 2
    try:
        from hardware.camera import Camera, CameraError, CameraReader
        from hardware.ultrasonic import UltrasonicError, UltrasonicMonitor, UltrasonicSensor
        detector = LiteRTDetector(args.model, args.labels, args.threads)
    except Exception as exc:
        print(f"PHASE 3 STARTUP FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    sensor = ultrasonic = camera = reader = None
    ultrasonic_error = None
    try:
        sensor = UltrasonicSensor().open()
        ultrasonic = UltrasonicMonitor(sensor)
        ultrasonic.start()
        print(f"Ultrasonic: {sensor.description} (independent background monitor)", flush=True)
    except UltrasonicError as exc:
        ultrasonic_error = str(exc)
        print(f"ULTRASONIC UNAVAILABLE: {exc}\nVision/tracking will continue without distance.", flush=True)
    tracker = LightweightTracker(max_center_distance_px=args.max_center_distance,
                                 lost_timeout_s=args.lost_timeout,
                                 priority_lost_timeout_s=args.priority_lost_timeout,
                                 confirmation_hits=args.confirmation_hits,
                                 confirmation_window_s=args.confirmation_window,
                                 horizontal_dead_zone_px=args.horizontal_dead_zone,
                                 area_change_ratio=args.area_change_ratio)
    stats, monitor, gate = MotionStats(), SystemMonitor(), NewestFrameGate()
    timestamps: deque[float] = deque(maxlen=30)
    last_started = next_log = 0.0
    print("SENSE PHASE 3 MOTION TRACKING PROTOTYPE", flush=True)
    print("Gemini: DISABLED (zero API/cloud calls); audio/navigation: DISABLED", flush=True)
    print("Motion is apparent relative to the camera image; head movement is not compensated.", flush=True)
    try:
        camera = Camera(resolution=(args.camera_width, args.camera_height), pixel_format="RGB888").open()
        reader = CameraReader(camera)
        reader.start()
        stats = MotionStats()
        print(f"Camera: {camera.info}; tracking starts (Q/Esc exits)", flush=True)
        while True:
            now = time.monotonic()
            if args.benchmark_seconds and now - stats.started_at >= args.benchmark_seconds:
                break
            if args.max_detection_fps and now - last_started < 1 / args.max_detection_fps:
                if not args.no_preview and detector.cv2.waitKey(1) & 0xFF in (ord("q"), 27): break
                time.sleep(.002); continue
            frame, frame_age = reader.latest()
            if frame is None or not gate.accept(id(frame)):
                reader.report_stall_once()
                if not args.no_preview and detector.cv2.waitKey(1) & 0xFF in (ord("q"), 27): break
                time.sleep(.002); continue
            last_started = time.monotonic()
            detections, inference_ms = detector.detect(frame, args.confidence)
            tracking_started = time.perf_counter()
            tracks = tracker.update(detections, time.monotonic())
            tracking_ms = (time.perf_counter() - tracking_started) * 1000.0
            snapshot = ultrasonic.snapshot() if ultrasonic is not None else {"distance_cm": None, "age_s": None, "healthy": False, "error": ultrasonic_error}
            forward, forward_state = forward_distance(snapshot, args.ultrasonic_max_age)
            timestamps.append(time.monotonic())
            fps = None if len(timestamps) < 2 else (len(timestamps) - 1) / (timestamps[-1] - timestamps[0])
            system = monitor.sample()
            metrics = {"inference": inference_ms, "tracking": tracking_ms, "detection_fps": fps,
                       "tracks": len(tracks), "frame_age": (frame_age + (time.monotonic() - last_started)) * 1000,
                       "ultrasonic_age": None if snapshot["age_s"] is None else snapshot["age_s"] * 1000, **system}
            display_tracks = tracker.tracks_for_display(args.debug_tracks)
            stats.add(inference_ms, tracking_ms, system, len(tracks))
            if time.monotonic() >= next_log:
                visual = ", ".join(f"{track.display_id} {track.position} {tracker.visible_motion(track)}" for track in display_tracks) or "no confirmed recognized object"
                distance = "DISTANCE UNAVAILABLE" if forward is None else f"{forward:.0f} cm ({forward_state})"
                print(f"TRACKS: {visual} | FORWARD OBSTACLE: {distance}", flush=True)
                next_log = time.monotonic() + 1
            if not args.no_preview:
                detector.cv2.imshow("Sense Phase 3 motion tracking prototype", draw_preview(frame.copy(), display_tracks, forward, forward_state, metrics, detector.cv2))
                if detector.cv2.waitKey(1) & 0xFF in (ord("q"), 27): break
    except KeyboardInterrupt:
        print("Stopping on Ctrl-C.", flush=True)
    except CameraError as exc:
        print(f"CAMERA FAILED: {exc}", file=sys.stderr); return 3
    except Exception as exc:
        print(f"PHASE 3 FAILED: {type(exc).__name__}: {exc}", file=sys.stderr); return 3
    finally:
        elapsed = time.monotonic() - stats.started_at
        if reader is not None: reader.stop()
        if camera is not None: camera.close()
        if ultrasonic is not None: ultrasonic.stop()
        if sensor is not None: sensor.close()
        if not args.no_preview: detector.cv2.destroyAllWindows()
        print(stats.summary(elapsed, tracker), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
