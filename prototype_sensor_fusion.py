#!/usr/bin/env python3
"""Phase 2: isolated local camera + forward-ultrasonic fusion prototype.

This is diagnostic software only. It makes no navigation decision, produces no
audio, and makes no Gemini/cloud request. A single forward ultrasonic beam is
never treated as per-object depth.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from collections import deque
from pathlib import Path
import statistics
import sys
import time
from typing import Iterable, Sequence

from prototype_vision import (
    DEFAULT_LABELS,
    DEFAULT_MODEL,
    Detection,
    LiteRTDetector,
    NewestFrameGate,
    SystemMonitor,
    format_metric,
    percentile,
)


ASSOCIATION_NONE = "NO ASSOCIATION"
ASSOCIATION_POSSIBLE = "POSSIBLE ASSOCIATION"
ASSOCIATION_AMBIGUOUS = "AMBIGUOUS"
ASSOCIATION_STALE = "ASSOCIATION UNAVAILABLE (STALE)"
ASSOCIATION_DISTANCE_UNAVAILABLE = "DISTANCE UNAVAILABLE"


@dataclass(frozen=True)
class FusionResult:
    forward_distance_cm: float | None
    distance_band: str | None
    association: str
    candidates: tuple[Detection, ...]
    ultrasonic_age_s: float | None
    vision_age_s: float | None
    reason: str | None = None


def association_zone(width: int, fraction: float) -> tuple[int, int]:
    """Return the centred horizontal beam zone as [left, right] pixels."""
    if width <= 0 or not 0.0 < fraction <= 1.0:
        raise ValueError("association-zone fraction must be in (0, 1]")
    zone_width = max(1, round(width * fraction))
    left = (width - zone_width) // 2
    return left, left + zone_width


def zone_overlap_ratio(box: tuple[int, int, int, int], zone: tuple[int, int]) -> float:
    """Horizontal fraction of a detection box inside the ultrasonic beam zone."""
    left, _, right, _ = box
    zone_left, zone_right = zone
    box_width = right - left
    if box_width <= 0:
        return 0.0
    overlap = max(0, min(right, zone_right) - max(left, zone_left))
    return overlap / box_width


def distance_band(distance_cm: float | None) -> str | None:
    if distance_cm is None:
        return None
    if distance_cm > 100.0:
        return "FAR"
    if distance_cm >= 50.0:
        return "NEAR"
    if distance_cm >= 25.0:
        return "CLOSE"
    return "VERY CLOSE"


def fuse(
    detections: Iterable[Detection],
    ultrasonic_snapshot: dict | None,
    vision_age_s: float | None,
    image_width: int,
    zone_fraction: float,
    min_overlap: float,
    min_detection_confidence: float,
    vision_max_age_s: float,
    ultrasonic_max_age_s: float,
) -> FusionResult:
    """Conservatively relate fresh visual detections to one forward beam.

    The returned forward distance never depends on detection success. Candidates
    only mean a visual object overlaps the beam; they are not assigned a range.
    """
    snapshot = ultrasonic_snapshot or {}
    distance = snapshot.get("distance_cm")
    ultrasonic_age = snapshot.get("age_s")
    healthy = bool(snapshot.get("healthy", False))
    valid_distance = (
        isinstance(distance, (int, float)) and not isinstance(distance, bool)
        and distance >= 0 and healthy and not snapshot.get("out_of_range", False)
    )
    fresh_distance = valid_distance and ultrasonic_age is not None and ultrasonic_age <= ultrasonic_max_age_s
    fresh_vision = vision_age_s is not None and vision_age_s <= vision_max_age_s
    forward = float(distance) if valid_distance else None
    if not valid_distance:
        return FusionResult(
            forward, distance_band(forward), ASSOCIATION_DISTANCE_UNAVAILABLE, (),
            ultrasonic_age, vision_age_s, snapshot.get("error") or "no valid ultrasonic reading",
        )
    if not fresh_distance or not fresh_vision:
        stale_parts = []
        if not fresh_distance:
            stale_parts.append("ultrasonic")
        if not fresh_vision:
            stale_parts.append("vision")
        return FusionResult(
            forward, distance_band(forward), ASSOCIATION_STALE, (), ultrasonic_age,
            vision_age_s, "stale " + " and ".join(stale_parts),
        )

    zone = association_zone(image_width, zone_fraction)
    candidates = tuple(
        detection for detection in detections
        if detection.confidence >= min_detection_confidence
        and zone_overlap_ratio(detection.box, zone) >= min_overlap
    )
    if not candidates:
        association = ASSOCIATION_NONE
    elif len(candidates) == 1:
        association = ASSOCIATION_POSSIBLE
    else:
        association = ASSOCIATION_AMBIGUOUS
    return FusionResult(forward, distance_band(forward), association, candidates, ultrasonic_age, vision_age_s)


@dataclass
class FusionStats:
    started_at: float = field(default_factory=time.monotonic)
    inference_ms: list[float] = field(default_factory=list)
    total_ms: list[float] = field(default_factory=list)
    fusion_ms: list[float] = field(default_factory=list)
    ultrasonic_age_ms: list[float] = field(default_factory=list)
    cpu: list[float] = field(default_factory=list)
    rss: list[float] = field(default_factory=list)
    temperature: list[float] = field(default_factory=list)
    associations: dict[str, int] = field(default_factory=lambda: {
        ASSOCIATION_POSSIBLE: 0, ASSOCIATION_AMBIGUOUS: 0, ASSOCIATION_NONE: 0,
    })

    def add(self, inference_ms: float, total_ms: float, fusion_ms: float, snapshot: dict, system: dict, result: FusionResult) -> None:
        self.inference_ms.append(inference_ms)
        self.total_ms.append(total_ms)
        self.fusion_ms.append(fusion_ms)
        if result.ultrasonic_age_s is not None:
            self.ultrasonic_age_ms.append(result.ultrasonic_age_s * 1000.0)
        for key, target in (("cpu", self.cpu), ("rss", self.rss), ("temp", self.temperature)):
            if system.get(key) is not None:
                target.append(float(system[key]))
        if result.association in self.associations:
            self.associations[result.association] += 1

    @staticmethod
    def _value(value: float | None, suffix: str = "") -> str:
        return "n/a" if value is None else f"{value:.1f}{suffix}"

    def summary(self, elapsed_s: float, reading_count: int) -> str:
        enough = len(self.inference_ms) >= 5
        return "\n".join((
            "\n=== SENSE PHASE 2 BENCHMARK ===",
            f"Frames analyzed: {len(self.inference_ms)} in {elapsed_s:.1f}s",
            f"Detection FPS: {self._value(len(self.inference_ms) / elapsed_s if elapsed_s > 0 else None)}",
            f"Average inference: {self._value(statistics.fmean(self.inference_ms) if self.inference_ms else None, ' ms')}",
            f"P95 inference: {self._value(percentile(self.inference_ms, 95) if enough else None, ' ms')}",
            f"Average total cycle: {self._value(statistics.fmean(self.total_ms) if self.total_ms else None, ' ms')}",
            f"Ultrasonic update rate: {self._value(reading_count / elapsed_s if elapsed_s > 0 else None, ' Hz')}",
            f"Average ultrasonic age: {self._value(statistics.fmean(self.ultrasonic_age_ms) if self.ultrasonic_age_ms else None, ' ms')}",
            f"P95 ultrasonic age: {self._value(percentile(self.ultrasonic_age_ms, 95) if len(self.ultrasonic_age_ms) >= 5 else None, ' ms')}",
            f"Average fusion time: {self._value(statistics.fmean(self.fusion_ms) if self.fusion_ms else None, ' ms')}",
            f"Average CPU: {self._value(statistics.fmean(self.cpu) if self.cpu else None, '%')}",
            f"Peak RAM: {self._value(max(self.rss) if self.rss else None, ' MiB')}",
            f"Maximum CPU temperature: {self._value(max(self.temperature) if self.temperature else None, ' C')}",
            f"Possible associations: {self.associations[ASSOCIATION_POSSIBLE]}",
            f"Ambiguous associations: {self.associations[ASSOCIATION_AMBIGUOUS]}",
            f"No associations: {self.associations[ASSOCIATION_NONE]}",
            "================================",
        ))


def draw_preview(frame, detections: Iterable[Detection], result: FusionResult, metrics: dict, zone: tuple[int, int], cv2):
    height, width = frame.shape[:2]
    overlay = frame.copy()
    zone_left, zone_right = zone
    cv2.rectangle(overlay, (zone_left, 0), (zone_right, height), (255, 170, 0), -1)
    cv2.addWeighted(overlay, 0.13, frame, 0.87, 0, frame)
    cv2.rectangle(frame, (zone_left, 0), (zone_right, height - 1), (255, 170, 0), 2)
    cv2.putText(frame, "ULTRASONIC BEAM", (zone_left + 4, 18), cv2.FONT_HERSHEY_SIMPLEX, .45, (255, 170, 0), 1)
    for boundary in (width // 3, 2 * width // 3):
        cv2.line(frame, (boundary, 0), (boundary, height), (90, 90, 90), 1)
    for detection in detections:
        left, top, right, bottom = detection.box
        color = (50, 220, 50) if detection.position == "CENTER" else (0, 190, 255)
        cv2.rectangle(frame, (left, top), (right, bottom), color, 2)
        cv2.putText(frame, f"{detection.label} {detection.confidence:.0%} {detection.position}",
                    (left, max(38, top - 6)), cv2.FONT_HERSHEY_SIMPLEX, .52, color, 2)
    distance_text = "FORWARD: DISTANCE UNAVAILABLE" if result.forward_distance_cm is None else (
        f"FORWARD OBSTACLE: {result.forward_distance_cm:.0f} cm ({result.distance_band})"
    )
    candidate_text = ", ".join(item.label.upper() for item in result.candidates)
    association_text = result.association + (f": {candidate_text}" if candidate_text else "")
    lines = (
        distance_text,
        association_text,
        "AI {}ms cycle {}ms detect {}fps | ultrasonic age {}ms {}Hz | fusion {}ms".format(
            format_metric(metrics["inference"]), format_metric(metrics["total"]), format_metric(metrics["detection_fps"]),
            format_metric(metrics["ultrasonic_age"]), format_metric(metrics["ultrasonic_hz"]),
            format_metric(metrics["fusion"]),
        ),
        "vision age {}ms | CPU {} RSS {} temp {}".format(
            format_metric(metrics["vision_age"]), format_metric(metrics["cpu"], "%"),
            format_metric(metrics["rss"], "MiB"), format_metric(metrics["temp"], "C"),
        ),
    )
    for index, text in enumerate(lines):
        y = 43 + index * 20
        cv2.putText(frame, text, (7, y), cv2.FONT_HERSHEY_SIMPLEX, .45, (0, 0, 0), 3)
        cv2.putText(frame, text, (7, y), cv2.FONT_HERSHEY_SIMPLEX, .45, (255, 255, 255), 1)
    return frame


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS)
    parser.add_argument("--confidence", type=float, default=.50)
    parser.add_argument("--max-detection-fps", type=float, default=5.0)
    parser.add_argument("--benchmark-seconds", type=float, default=60.0, help="0 runs until Q/Ctrl-C")
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--camera-width", type=int, default=640)
    parser.add_argument("--camera-height", type=int, default=480)
    parser.add_argument("--association-zone-width", type=float, default=.34, help="fraction of image width")
    parser.add_argument("--association-min-overlap", type=float, default=.25, help="required detection-box overlap")
    parser.add_argument("--vision-max-age", type=float, default=.75, help="seconds")
    parser.add_argument("--ultrasonic-max-age", type=float, default=.50, help="seconds")
    parser.add_argument("--no-preview", action="store_true")
    args = parser.parse_args(argv)
    if not 0 <= args.confidence <= 1 or not 0 < args.association_zone_width <= 1 or not 0 <= args.association_min_overlap <= 1:
        parser.error("confidence/overlap must be [0,1]; association-zone-width must be (0,1]")
    if args.max_detection_fps < 0 or args.benchmark_seconds < 0 or args.threads < 1 or args.vision_max_age <= 0 or args.ultrasonic_max_age <= 0:
        parser.error("invalid timing or thread value")
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
        print(f"PHASE 2 STARTUP FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    sensor = ultrasonic = camera = reader = None
    ultrasonic_start_error = None
    try:
        sensor = UltrasonicSensor().open()
        ultrasonic = UltrasonicMonitor(sensor)
        ultrasonic.start()
        print(f"Ultrasonic: {sensor.description} (background monitor started)", flush=True)
        print("Forward distance uses Sense's existing median-of-pings filter; raw pings remain diagnostic-only.", flush=True)
    except UltrasonicError as exc:
        ultrasonic_start_error = str(exc)
        print(f"ULTRASONIC UNAVAILABLE: {exc}\nVision will continue; no distance will be invented.", flush=True)

    stats = FusionStats()
    system_monitor = SystemMonitor()
    gate = NewestFrameGate()
    detection_times: deque[float] = deque(maxlen=30)
    last_started = next_log = 0.0
    print("SENSE PHASE 2 LOCAL SENSOR FUSION PROTOTYPE", flush=True)
    print("Gemini: DISABLED (zero API/cloud calls); audio/navigation: DISABLED", flush=True)
    print(f"Association zone: center {args.association_zone_width:.0%}; min object overlap: {args.association_min_overlap:.0%}", flush=True)
    print(f"Freshness limits: vision {args.vision_max_age:.2f}s; ultrasonic {args.ultrasonic_max_age:.2f}s", flush=True)
    try:
        camera = Camera(resolution=(args.camera_width, args.camera_height), pixel_format="RGB888").open()
        reader = CameraReader(camera)
        reader.start()
        stats = FusionStats()
        print(f"Camera: {camera.info}; starting local detection and fusion (Q/Esc exits)", flush=True)
        while True:
            now = time.monotonic()
            if args.benchmark_seconds and now - stats.started_at >= args.benchmark_seconds:
                break
            if args.max_detection_fps and now - last_started < 1.0 / args.max_detection_fps:
                if not args.no_preview and detector.cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                    break
                time.sleep(.002)
                continue
            frame, captured_age_s = reader.latest()
            if frame is None or not gate.accept(id(frame)):
                reader.report_stall_once()
                if not args.no_preview and detector.cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                    break
                time.sleep(.002)
                continue
            last_started = time.monotonic()
            cycle_started = time.perf_counter()
            detections, inference_ms = detector.detect(frame, args.confidence)
            vision_age_s = captured_age_s + (time.monotonic() - last_started)
            snapshot = ultrasonic.snapshot() if ultrasonic is not None else {
                "distance_cm": None, "age_s": None, "healthy": False,
                "reading_count": 0, "error": ultrasonic_start_error,
            }
            fusion_started = time.perf_counter()
            result = fuse(
                detections, snapshot, vision_age_s, frame.shape[1], args.association_zone_width,
                args.association_min_overlap, args.confidence, args.vision_max_age,
                args.ultrasonic_max_age,
            )
            fusion_ms = (time.perf_counter() - fusion_started) * 1000.0
            total_ms = (time.perf_counter() - cycle_started) * 1000.0
            detection_times.append(time.monotonic())
            detection_fps = None
            if len(detection_times) >= 2 and detection_times[-1] > detection_times[0]:
                detection_fps = (len(detection_times) - 1) / (detection_times[-1] - detection_times[0])
            system = system_monitor.sample()
            ultrasonic_hz = snapshot["reading_count"] / max(.001, time.monotonic() - stats.started_at)
            metrics = {
                "inference": inference_ms, "total": total_ms, "detection_fps": detection_fps, "fusion": fusion_ms,
                "vision_age": vision_age_s * 1000.0,
                "ultrasonic_age": None if snapshot["age_s"] is None else snapshot["age_s"] * 1000.0,
                "ultrasonic_hz": ultrasonic_hz, **system,
            }
            stats.add(inference_ms, total_ms, fusion_ms, snapshot, system, result)
            if time.monotonic() >= next_log:
                objects = ", ".join(f"{item.label} {item.confidence:.0%} {item.position}" for item in detections) or "no recognized object"
                forward = "DISTANCE UNAVAILABLE" if result.forward_distance_cm is None else f"{result.forward_distance_cm:.0f} cm ({result.distance_band})"
                candidates = ", ".join(item.label for item in result.candidates)
                print(f"VISION: {objects} | FORWARD OBSTACLE: {forward} | {result.association}{(': ' + candidates) if candidates else ''}", flush=True)
                next_log = time.monotonic() + 1.0
            if not args.no_preview:
                zone = association_zone(frame.shape[1], args.association_zone_width)
                detector.cv2.imshow("Sense Phase 2 sensor fusion prototype", draw_preview(frame.copy(), detections, result, metrics, zone, detector.cv2))
                if detector.cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                    break
    except KeyboardInterrupt:
        print("Stopping on Ctrl-C.", flush=True)
    except CameraError as exc:
        print(f"CAMERA FAILED: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:
        print(f"PHASE 2 FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    finally:
        elapsed = time.monotonic() - stats.started_at
        reading_count = 0 if ultrasonic is None else ultrasonic.snapshot()["reading_count"]
        if reader is not None:
            reader.stop()
        if camera is not None:
            camera.close()
        if ultrasonic is not None:
            ultrasonic.stop()
        if sensor is not None:
            sensor.close()
        if not args.no_preview:
            detector.cv2.destroyAllWindows()
        print(stats.summary(elapsed, reading_count), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
