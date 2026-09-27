#!/usr/bin/env python3
"""Phase 4 isolated sparse camera-motion and apparent object-motion prototype."""
from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
import statistics
import sys
import time
from typing import Iterable, Sequence

from prototype_motion_tracking import HIGH_PRIORITY, LightweightTracker, forward_distance
from prototype_vision import DEFAULT_LABELS, DEFAULT_MODEL, Detection, LiteRTDetector, NewestFrameGate, SystemMonitor, format_metric, percentile


CAMERA_STABLE = "STABLE"
CAMERA_LEFT = "TURNING LEFT"
CAMERA_RIGHT = "TURNING RIGHT"
CAMERA_UP = "MOVING UP"
CAMERA_DOWN = "MOVING DOWN"
CAMERA_UNCERTAIN = "MOTION UNCERTAIN"


@dataclass(frozen=True)
class CameraMotion:
    dx: float
    dy: float
    state: str
    confidence: str
    feature_count: int
    inlier_count: int


def classify_camera_motion(dx: float, dy: float, dead_zone_px: float, *, reliable: bool = True) -> str:
    if not reliable:
        return CAMERA_UNCERTAIN
    if abs(dx) <= dead_zone_px and abs(dy) <= dead_zone_px:
        return CAMERA_STABLE
    if abs(dx) >= abs(dy):
        return CAMERA_RIGHT if dx < 0 else CAMERA_LEFT
    return CAMERA_UP if dy > 0 else CAMERA_DOWN


def robust_camera_motion(vectors: Iterable[tuple[float, float]], *, min_features: int = 8,
                         inlier_radius_px: float = 5.0, dead_zone_px: float = 4.0) -> CameraMotion:
    values = [(float(dx), float(dy)) for dx, dy in vectors]
    if len(values) < min_features:
        return CameraMotion(0.0, 0.0, CAMERA_UNCERTAIN, "LOW", len(values), 0)
    xs, ys = sorted(dx for dx, _ in values), sorted(dy for _, dy in values)
    median_x, median_y = statistics.median(xs), statistics.median(ys)
    inliers = [(dx, dy) for dx, dy in values if (dx - median_x) ** 2 + (dy - median_y) ** 2 <= inlier_radius_px ** 2]
    if len(inliers) < min_features:
        return CameraMotion(0.0, 0.0, CAMERA_UNCERTAIN, "LOW", len(values), len(inliers))
    dx = statistics.median(value[0] for value in inliers)
    dy = statistics.median(value[1] for value in inliers)
    ratio = len(inliers) / len(values)
    confidence = "HIGH" if ratio >= .70 and len(inliers) >= 15 else "MEDIUM" if ratio >= .50 else "LOW"
    state = classify_camera_motion(dx, dy, dead_zone_px, reliable=confidence != "LOW")
    return CameraMotion(dx, dy, state, confidence, len(values), len(inliers))


class MotionSmoother:
    def __init__(self, size: int = 3, dead_zone_px: float = 4.0):
        self.values: deque[CameraMotion] = deque(maxlen=size)
        self.dead_zone_px = dead_zone_px

    def update(self, estimate: CameraMotion) -> CameraMotion:
        if estimate.state == CAMERA_UNCERTAIN:
            return estimate
        self.values.append(estimate)
        if not self.values:
            return estimate
        dx = statistics.median(item.dx for item in self.values)
        dy = statistics.median(item.dy for item in self.values)
        features = round(statistics.fmean(item.feature_count for item in self.values))
        inliers = round(statistics.fmean(item.inlier_count for item in self.values))
        confidence = "HIGH" if all(item.confidence == "HIGH" for item in self.values) else "MEDIUM"
        return CameraMotion(dx, dy, classify_camera_motion(dx, dy, self.dead_zone_px), confidence, features, inliers)


def feature_is_masked(x: float, y: float, dynamic_boxes: Iterable[tuple[int, int, int, int]], scale: float) -> bool:
    for left, top, right, bottom in dynamic_boxes:
        if left * scale <= x <= right * scale and top * scale <= y <= bottom * scale:
            return True
    return False


def corrected_motion(track, camera: CameraMotion, dead_zone_px: float = 20.0) -> str:
    history = list(track.history)
    if camera.state == CAMERA_UNCERTAIN or len(history) < 2 or not track.seen_this_update:
        return "UNCERTAIN"
    raw_dx = history[-1].center_x - history[-2].center_x
    residual = raw_dx - camera.dx
    if abs(residual) <= dead_zone_px:
        return "LOW MOTION"
    return "MOVING RIGHT" if residual > 0 else "MOVING LEFT"


class SparseCameraMotionEstimator:
    """Cheap detector-frame optical flow: newest pair only, never queued."""
    def __init__(self, cv2, np, *, scale: float = .5, max_features: int = 80,
                 min_features: int = 8, dead_zone_px: float = 4.0, mask_dynamic: bool = True):
        self.cv2, self.np = cv2, np
        self.scale, self.max_features, self.min_features = scale, max_features, min_features
        self.dead_zone_px, self.mask_dynamic = dead_zone_px, mask_dynamic
        self.previous_gray = self.previous_points = None

    def _gray_and_points(self, frame, detections: Iterable[Detection]):
        small = self.cv2.resize(frame, None, fx=self.scale, fy=self.scale, interpolation=self.cv2.INTER_AREA)
        gray = self.cv2.cvtColor(small, self.cv2.COLOR_BGR2GRAY)
        mask = self.np.full(gray.shape, 255, dtype=self.np.uint8)
        if self.mask_dynamic:
            for detection in detections:
                if detection.label.lower() in HIGH_PRIORITY:
                    left, top, right, bottom = detection.box
                    self.cv2.rectangle(mask, (round(left * self.scale), round(top * self.scale)),
                                       (round(right * self.scale), round(bottom * self.scale)), 0, -1)
        points = self.cv2.goodFeaturesToTrack(gray, maxCorners=self.max_features, qualityLevel=.01,
                                               minDistance=7, mask=mask, blockSize=7)
        return gray, points

    def update(self, frame, detections: Iterable[Detection]) -> tuple[CameraMotion, list[tuple[float, float]]]:
        gray, points = self._gray_and_points(frame, detections)
        tracked_points: list[tuple[float, float]] = []
        if self.previous_gray is None or self.previous_points is None or len(self.previous_points) < self.min_features:
            estimate = robust_camera_motion([], min_features=self.min_features, dead_zone_px=self.dead_zone_px)
        else:
            next_points, status, _ = self.cv2.calcOpticalFlowPyrLK(
                self.previous_gray, gray, self.previous_points, None,
                winSize=(15, 15), maxLevel=2,
                criteria=(self.cv2.TERM_CRITERIA_EPS | self.cv2.TERM_CRITERIA_COUNT, 12, .03),
            )
            vectors = []
            if next_points is not None and status is not None:
                for previous, current, valid in zip(self.previous_points.reshape(-1, 2), next_points.reshape(-1, 2), status.reshape(-1)):
                    if valid:
                        vectors.append(((current[0] - previous[0]) / self.scale, (current[1] - previous[1]) / self.scale))
                        tracked_points.append((float(current[0] / self.scale), float(current[1] / self.scale)))
            estimate = robust_camera_motion(vectors, min_features=self.min_features,
                                            dead_zone_px=self.dead_zone_px)
        self.previous_gray, self.previous_points = gray, points
        return estimate, tracked_points


@dataclass
class CameraMotionStats:
    started_at: float = field(default_factory=time.monotonic)
    inference: list[float] = field(default_factory=list)
    motion: list[float] = field(default_factory=list)
    tracking: list[float] = field(default_factory=list)
    feature_counts: list[float] = field(default_factory=list)
    uncertain: int = 0
    cpu: list[float] = field(default_factory=list)
    rss: list[float] = field(default_factory=list)
    temp: list[float] = field(default_factory=list)
    freshness: list[float] = field(default_factory=list)

    @staticmethod
    def _value(value, suffix=""):
        return "n/a" if value is None else f"{value:.1f}{suffix}"

    def summary(self, elapsed: float) -> str:
        enough = len(self.inference) >= 5
        return "\n".join((
            "\n=== SENSE PHASE 4 BENCHMARK ===",
            f"Object detection FPS: {self._value(len(self.inference) / elapsed if elapsed else None)}",
            f"Inference avg/p50/p95: {self._value(statistics.fmean(self.inference) if self.inference else None, ' ms')}/{self._value(percentile(self.inference, 50) if enough else None, ' ms')}/{self._value(percentile(self.inference, 95) if enough else None, ' ms')}",
            f"Camera motion avg/p50/p95: {self._value(statistics.fmean(self.motion) if self.motion else None, ' ms')}/{self._value(percentile(self.motion, 50) if len(self.motion) >= 5 else None, ' ms')}/{self._value(percentile(self.motion, 95) if len(self.motion) >= 5 else None, ' ms')}",
            f"Tracked visual features avg: {self._value(statistics.fmean(self.feature_counts) if self.feature_counts else None)}",
            f"Uncertain camera estimates: {self.uncertain}/{len(self.motion)} ({self._value(100 * self.uncertain / len(self.motion) if self.motion else None, '%')})",
            f"Object tracking avg: {self._value(statistics.fmean(self.tracking) if self.tracking else None, ' ms')}",
            f"Frame freshness avg: {self._value(statistics.fmean(self.freshness) if self.freshness else None, ' ms')}",
            f"Average CPU: {self._value(statistics.fmean(self.cpu) if self.cpu else None, '%')}",
            f"Peak RAM: {self._value(max(self.rss) if self.rss else None, ' MiB')}",
            f"Maximum CPU temperature: {self._value(max(self.temp) if self.temp else None, ' C')}",
            "================================",
        ))


def parse_args(argv: Sequence[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL); parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS)
    parser.add_argument("--confidence", type=float, default=.50); parser.add_argument("--max-detection-fps", type=float, default=5.)
    parser.add_argument("--benchmark-seconds", type=float, default=0., help="numeric duration; 0 runs until Q/Esc/Ctrl-C")
    parser.add_argument("--threads", type=int, default=2); parser.add_argument("--camera-width", type=int, default=640); parser.add_argument("--camera-height", type=int, default=480)
    parser.add_argument("--motion-scale", type=float, default=.5); parser.add_argument("--max-features", type=int, default=80)
    parser.add_argument("--motion-dead-zone", type=float, default=4.); parser.add_argument("--no-object-mask", action="store_true")
    parser.add_argument("--show-features", action="store_true"); parser.add_argument("--no-preview", action="store_true")
    args = parser.parse_args(argv)
    if not 0 <= args.confidence <= 1 or args.max_detection_fps < 0 or args.benchmark_seconds < 0 or args.threads < 1 or not 0 < args.motion_scale <= 1 or args.max_features < 8 or args.motion_dead_zone <= 0:
        parser.error("invalid motion/tracking option")
    return args


def draw_preview(frame, tracks, camera_motion, corrected, forward, forward_state, metrics, feature_points, show_features, cv2):
    h, w = frame.shape[:2]
    if show_features:
        for x, y in feature_points: cv2.circle(frame, (round(x), round(y)), 2, (255, 200, 0), -1)
    for track in tracks:
        left, top, right, bottom = track.box; color = (0, 80, 255) if track.priority == "HIGH" else (50, 220, 50)
        cv2.rectangle(frame, (left, top), (right, bottom), color, 2)
        cv2.putText(frame, f"{track.display_id} {track.position}", (left, max(22, top - 24)), cv2.FONT_HERSHEY_SIMPLEX, .5, color, 2)
        cv2.putText(frame, f"RAW {track.motion} | CORRECTED {corrected.get(id(track), 'UNCERTAIN')}", (left, max(40, top - 6)), cv2.FONT_HERSHEY_SIMPLEX, .42, color, 2)
    forward_text = "FORWARD OBSTACLE: DISTANCE UNAVAILABLE" if forward is None else f"FORWARD OBSTACLE: {forward:.0f} cm ({forward_state})"
    lines = (f"CAMERA: {camera_motion.state} dx={camera_motion.dx:.1f} dy={camera_motion.dy:.1f} | features={camera_motion.feature_count} inliers={camera_motion.inlier_count} {camera_motion.confidence}", forward_text,
             "motion {}ms | AI {}ms | track {}ms | fresh {}ms".format(format_metric(metrics['motion']), format_metric(metrics['inference']), format_metric(metrics['tracking']), format_metric(metrics['freshness'])))
    for i, text in enumerate(lines):
        y = 22 + 20 * i; cv2.putText(frame, text, (7, y), cv2.FONT_HERSHEY_SIMPLEX, .42, (0, 0, 0), 3); cv2.putText(frame, text, (7, y), cv2.FONT_HERSHEY_SIMPLEX, .42, (255,255,255), 1)
    return frame


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.model.is_file() or not args.labels.is_file(): print("MISSING MODEL ASSET: run python3 download_prototype_model.py", file=sys.stderr); return 2
    try:
        from hardware.camera import Camera, CameraError, CameraReader
        from hardware.ultrasonic import UltrasonicError, UltrasonicMonitor, UltrasonicSensor
        detector = LiteRTDetector(args.model, args.labels, args.threads)
    except Exception as exc: print(f"PHASE 4 STARTUP FAILED: {type(exc).__name__}: {exc}", file=sys.stderr); return 2
    sensor = ultrasonic = camera = reader = None; ultrasonic_error = None
    try:
        sensor = UltrasonicSensor().open(); ultrasonic = UltrasonicMonitor(sensor); ultrasonic.start()
    except UltrasonicError as exc: ultrasonic_error = str(exc); print(f"ULTRASONIC UNAVAILABLE: {exc}", flush=True)
    tracker = LightweightTracker(); stats, system, gate = CameraMotionStats(), SystemMonitor(), NewestFrameGate(); smoother = MotionSmoother(dead_zone_px=args.motion_dead_zone)
    last_started = next_log = 0.
    print("SENSE PHASE 4 CAMERA MOTION PROTOTYPE | Gemini: DISABLED | audio/navigation: DISABLED", flush=True)
    print("Sparse LK flow uses consecutive newest detector frames at downscaled resolution; dynamic people/vehicles are masked.", flush=True)
    try:
        camera = Camera(resolution=(args.camera_width, args.camera_height), pixel_format="RGB888").open(); reader = CameraReader(camera); reader.start()
        estimator = SparseCameraMotionEstimator(detector.cv2, detector.np, scale=args.motion_scale, max_features=args.max_features, dead_zone_px=args.motion_dead_zone, mask_dynamic=not args.no_object_mask)
        stats = CameraMotionStats()
        while True:
            now = time.monotonic()
            if args.benchmark_seconds and now - stats.started_at >= args.benchmark_seconds: break
            if args.max_detection_fps and now-last_started < 1/args.max_detection_fps:
                if not args.no_preview and detector.cv2.waitKey(1)&0xFF in (ord('q'),27): break
                time.sleep(.002); continue
            frame, age = reader.latest()
            if frame is None or not gate.accept(id(frame)):
                if not args.no_preview and detector.cv2.waitKey(1)&0xFF in (ord('q'),27): break
                time.sleep(.002); continue
            last_started=time.monotonic(); detections, inference_ms=detector.detect(frame,args.confidence)
            motion_started=time.perf_counter(); raw_camera, points=estimator.update(frame,detections); camera_motion=smoother.update(raw_camera); motion_ms=(time.perf_counter()-motion_started)*1000
            tracking_started=time.perf_counter(); tracks=tracker.update(detections,time.monotonic()); tracking_ms=(time.perf_counter()-tracking_started)*1000
            shown=tracker.tracks_for_display(); corrected={id(track): corrected_motion(track,camera_motion) for track in shown}
            snapshot=ultrasonic.snapshot() if ultrasonic is not None else {"distance_cm":None,"age_s":None,"healthy":False,"error":ultrasonic_error}; forward,forward_state=forward_distance(snapshot,.5)
            sample=system.sample(); freshness=(age+(time.monotonic()-last_started))*1000
            stats.inference.append(inference_ms); stats.motion.append(motion_ms); stats.tracking.append(tracking_ms); stats.feature_counts.append(camera_motion.feature_count); stats.freshness.append(freshness); stats.uncertain += camera_motion.state==CAMERA_UNCERTAIN
            for key,target in (("cpu",stats.cpu),("rss",stats.rss),("temp",stats.temp)):
                if sample.get(key) is not None: target.append(float(sample[key]))
            if time.monotonic() >= next_log:
                visible=", ".join(f"{t.display_id} {t.position} RAW {t.motion} CORR {corrected[id(t)]}" for t in shown) or "no confirmed recognized object"
                print(f"CAMERA: {camera_motion.state} dx={camera_motion.dx:.1f} dy={camera_motion.dy:.1f} features={camera_motion.feature_count} {camera_motion.confidence} | {visible}",flush=True); next_log=time.monotonic()+1
            if not args.no_preview:
                metrics={"motion":motion_ms,"inference":inference_ms,"tracking":tracking_ms,"freshness":freshness}
                detector.cv2.imshow("Sense Phase 4 camera motion prototype",draw_preview(frame.copy(),shown,camera_motion,corrected,forward,forward_state,metrics,points,args.show_features,detector.cv2))
                if detector.cv2.waitKey(1)&0xFF in (ord('q'),27): break
    except KeyboardInterrupt: print("Stopping on Ctrl-C.",flush=True)
    except CameraError as exc: print(f"CAMERA FAILED: {exc}",file=sys.stderr); return 3
    except Exception as exc: print(f"PHASE 4 FAILED: {type(exc).__name__}: {exc}",file=sys.stderr); return 3
    finally:
        elapsed=time.monotonic()-stats.started_at
        if reader is not None: reader.stop()
        if camera is not None: camera.close()
        if ultrasonic is not None: ultrasonic.stop()
        if sensor is not None: sensor.close()
        if not args.no_preview: detector.cv2.destroyAllWindows()
        print(stats.summary(elapsed),flush=True)
    return 0

if __name__ == "__main__": raise SystemExit(main())
