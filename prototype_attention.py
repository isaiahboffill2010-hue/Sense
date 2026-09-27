#!/usr/bin/env python3
"""Phase 5 isolated local relevance and attention prototype (no speech or cloud)."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
import statistics
import sys
import time
from typing import Iterable, Sequence

from prototype_camera_motion import CAMERA_UNCERTAIN, CameraMotion, MotionSmoother, SparseCameraMotionEstimator, corrected_motion
from prototype_motion_tracking import HIGH_PRIORITY, LightweightTracker, forward_distance
from prototype_vision import DEFAULT_LABELS, DEFAULT_MODEL, LiteRTDetector, NewestFrameGate, SystemMonitor, format_metric, percentile


HIGH, MEDIUM, LOW = "HIGH", "MEDIUM", "LOW"
LEVELS = (LOW, MEDIUM, HIGH)


@dataclass(frozen=True)
class AttentionObservation:
    track: object
    level: str
    score: int
    reasons: tuple[str, ...]
    corrected: str


def entering_center(track) -> bool:
    positions = [item.position for item in track.history]
    return len(positions) >= 2 and positions[-1] == "CENTER" and positions[-2] in {"LEFT", "RIGHT"}


class AttentionEngine:
    """Small explainable scorer; it never estimates distance or collision risk."""
    # Score weights. Thresholds: HIGH >= 8, MEDIUM >= 4, otherwise LOW.
    CLASS = 3
    CENTER = 2
    CORRECTED_MOVING = 2
    ENTERING_CENTER = 3
    CROSSING = 2
    CONFIRMED = 1

    def __init__(self):
        self.levels: dict[tuple[str, int], str] = {}
        self.low_streaks: dict[tuple[str, int], int] = {}

    @staticmethod
    def _key(track) -> tuple[str, int]: return (track.label, track.track_id)

    def _raw(self, track, camera: CameraMotion) -> tuple[int, list[str], str]:
        if not track.confirmed or not track.seen_this_update:
            return 0, ["tentative or lost track"], "UNCERTAIN"
        score, reasons = self.CONFIRMED, ["persistent track"]
        if track.label.lower() in HIGH_PRIORITY:
            score += self.CLASS; reasons.append("high-priority class")
        if track.position == "CENTER":
            score += self.CENTER; reasons.append("center")
        corrected = corrected_motion(track, camera)
        if camera.state == CAMERA_UNCERTAIN:
            reasons.append("camera motion uncertain")
        elif corrected.startswith("MOVING"):
            score += self.CORRECTED_MOVING; reasons.append("corrected movement")
        if entering_center(track):
            score += self.ENTERING_CENTER; reasons.append("entering center")
        if track.motion.startswith("CROSSING") and track.label.lower() in HIGH_PRIORITY:
            score += self.CROSSING; reasons.append("crossing")
        return score, reasons, corrected

    @staticmethod
    def _level(score: int) -> str:
        return HIGH if score >= 8 else MEDIUM if score >= 4 else LOW

    def score(self, tracks: Iterable[object], camera: CameraMotion) -> list[AttentionObservation]:
        observations: list[AttentionObservation] = []
        active = set()
        for track in tracks:
            key = self._key(track); active.add(key)
            score, reasons, corrected = self._raw(track, camera)
            desired = self._level(score)
            previous = self.levels.get(key, LOW)
            # Escalate immediately for fresh evidence. De-escalate one level only
            # after two consecutive lower samples, preventing HIGH/LOW flicker.
            if LEVELS.index(desired) > LEVELS.index(previous):
                level = desired; self.low_streaks[key] = 0
            elif LEVELS.index(desired) < LEVELS.index(previous):
                self.low_streaks[key] = self.low_streaks.get(key, 0) + 1
                level = LEVELS[max(0, LEVELS.index(previous) - 1)] if self.low_streaks[key] >= 2 else previous
                if level != previous: self.low_streaks[key] = 0
            else:
                level = previous; self.low_streaks[key] = 0
            self.levels[key] = level
            observations.append(AttentionObservation(track, level, score, tuple(reasons), corrected))
        for key in set(self.levels) - active:
            self.levels.pop(key, None); self.low_streaks.pop(key, None)
        return sorted(observations, key=lambda item: (-LEVELS.index(item.level), -item.score, item.track.label, item.track.track_id))


@dataclass
class AttentionStats:
    started_at: float = field(default_factory=time.monotonic)
    inference: list[float] = field(default_factory=list)
    motion: list[float] = field(default_factory=list)
    tracking: list[float] = field(default_factory=list)
    attention: list[float] = field(default_factory=list)
    cpu: list[float] = field(default_factory=list)
    rss: list[float] = field(default_factory=list)
    temp: list[float] = field(default_factory=list)
    freshness: list[float] = field(default_factory=list)

    @staticmethod
    def value(item, suffix=""): return "n/a" if item is None else f"{item:.1f}{suffix}"

    def summary(self, elapsed: float) -> str:
        def summary(values): return (statistics.fmean(values) if values else None, percentile(values, 50) if len(values) >= 5 else None, percentile(values, 95) if len(values) >= 5 else None)
        attention = summary(self.attention)
        return "\n".join(("\n=== SENSE PHASE 5 BENCHMARK ===", f"Detection FPS: {self.value(len(self.inference)/elapsed if elapsed else None)}", f"Attention avg/p50/p95: {self.value(attention[0], ' ms')}/{self.value(attention[1], ' ms')}/{self.value(attention[2], ' ms')}", f"Inference avg: {self.value(statistics.fmean(self.inference) if self.inference else None, ' ms')}", f"Camera motion avg: {self.value(statistics.fmean(self.motion) if self.motion else None, ' ms')}", f"Tracking avg: {self.value(statistics.fmean(self.tracking) if self.tracking else None, ' ms')}", f"Frame freshness avg: {self.value(statistics.fmean(self.freshness) if self.freshness else None, ' ms')}", f"Average CPU: {self.value(statistics.fmean(self.cpu) if self.cpu else None, '%')}", f"Peak RAM: {self.value(max(self.rss) if self.rss else None, ' MiB')}", f"Maximum CPU temperature: {self.value(max(self.temp) if self.temp else None, ' C')}", "================================"))


def parse_args(argv: Sequence[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL); parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS)
    parser.add_argument("--confidence", type=float, default=.50); parser.add_argument("--max-detection-fps", type=float, default=5.); parser.add_argument("--benchmark-seconds", type=float, default=0.)
    parser.add_argument("--threads", type=int, default=2); parser.add_argument("--camera-width", type=int, default=640); parser.add_argument("--camera-height", type=int, default=480)
    parser.add_argument("--no-preview", action="store_true"); parser.add_argument("--debug-attention", action="store_true")
    args = parser.parse_args(argv)
    if not 0 <= args.confidence <= 1 or args.max_detection_fps < 0 or args.benchmark_seconds < 0 or args.threads < 1: parser.error("invalid attention option")
    return args


def attention_line(item: AttentionObservation) -> str:
    reasons = ", ".join(reason for reason in item.reasons if reason != "persistent track") or "persistent track"
    return f"{item.track.display_id} | {item.level} | {item.track.position} | {reasons}"


def draw_preview(frame, observations, forward, forward_state, metrics, cv2):
    for item in observations[:3]:
        track = item.track; left, top, right, bottom = track.box; color = {HIGH:(0,80,255), MEDIUM:(0,220,220), LOW:(90,190,90)}[item.level]
        cv2.rectangle(frame, (left, top), (right, bottom), color, 2)
        cv2.putText(frame, f"{track.display_id} {item.level} {track.position}", (left, max(20, top-8)), cv2.FONT_HERSHEY_SIMPLEX, .48, color, 2)
    forward_text = "FORWARD OBSTACLE: DISTANCE UNAVAILABLE" if forward is None else f"FORWARD OBSTACLE: {forward:.0f} cm ({forward_state})"
    lines = ["TOP ATTENTION"] + [f"{i+1}. {attention_line(item)}" for i, item in enumerate(observations[:3])] + [forward_text, "attention {}ms | motion {}ms | inference {}ms".format(format_metric(metrics["attention"]),format_metric(metrics["motion"]),format_metric(metrics["inference"]))]
    for i, text in enumerate(lines):
        y=20+i*18; cv2.putText(frame,text,(7,y),cv2.FONT_HERSHEY_SIMPLEX,.42,(0,0,0),3); cv2.putText(frame,text,(7,y),cv2.FONT_HERSHEY_SIMPLEX,.42,(255,255,255),1)
    return frame


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.model.is_file() or not args.labels.is_file(): print("MISSING MODEL ASSET: run python3 download_prototype_model.py", file=sys.stderr); return 2
    try:
        from hardware.camera import Camera, CameraError, CameraReader
        from hardware.ultrasonic import UltrasonicError, UltrasonicMonitor, UltrasonicSensor
        detector=LiteRTDetector(args.model,args.labels,args.threads)
    except Exception as exc: print(f"PHASE 5 STARTUP FAILED: {type(exc).__name__}: {exc}",file=sys.stderr); return 2
    sensor=ultrasonic=camera=reader=None; ultrasonic_error=None; stats=AttentionStats()
    try:
        try: sensor=UltrasonicSensor().open(); ultrasonic=UltrasonicMonitor(sensor); ultrasonic.start()
        except UltrasonicError as exc: ultrasonic_error=str(exc); print(f"ULTRASONIC UNAVAILABLE: {exc}",flush=True)
        tracker=LightweightTracker(); engine=AttentionEngine(); system=SystemMonitor(); gate=NewestFrameGate(); smoother=MotionSmoother(); last_started=next_log=0.
        print("SENSE PHASE 5 ATTENTION PROTOTYPE | Gemini/cloud/audio/navigation: DISABLED",flush=True)
        camera=Camera(resolution=(args.camera_width,args.camera_height),pixel_format="RGB888").open(); reader=CameraReader(camera); reader.start(); estimator=SparseCameraMotionEstimator(detector.cv2,detector.np)
        while True:
            now=time.monotonic()
            if args.benchmark_seconds and now-stats.started_at >= args.benchmark_seconds: break
            if args.max_detection_fps and now-last_started < 1/args.max_detection_fps:
                if not args.no_preview and detector.cv2.waitKey(1)&0xFF in (ord("q"),27): break
                time.sleep(.002); continue
            frame,age=reader.latest()
            if frame is None or not gate.accept(id(frame)): time.sleep(.002); continue
            last_started=time.monotonic(); detections,inference_ms=detector.detect(frame,args.confidence)
            started=time.perf_counter(); camera_motion=smoother.update(estimator.update(frame,detections)[0]); motion_ms=(time.perf_counter()-started)*1000
            started=time.perf_counter(); tracker.update(detections,time.monotonic()); tracking_ms=(time.perf_counter()-started)*1000
            started=time.perf_counter(); observations=engine.score(tracker.tracks_for_display(),camera_motion); attention_ms=(time.perf_counter()-started)*1000
            snapshot=ultrasonic.snapshot() if ultrasonic else {"distance_cm":None,"age_s":None,"healthy":False,"error":ultrasonic_error}; forward,forward_state=forward_distance(snapshot,.5)
            sample=system.sample(); stats.inference.append(inference_ms); stats.motion.append(motion_ms); stats.tracking.append(tracking_ms); stats.attention.append(attention_ms); stats.freshness.append((age+time.monotonic()-last_started)*1000)
            for key,target in (("cpu",stats.cpu),("rss",stats.rss),("temp",stats.temp)):
                if sample.get(key) is not None: target.append(float(sample[key]))
            if time.monotonic()>=next_log:
                visible=observations if args.debug_attention else observations[:3]
                print("TOP ATTENTION: " + ("; ".join(f"{i+1}. {attention_line(item)}" for i,item in enumerate(visible)) or "none") + (f" | FORWARD OBSTACLE: {forward:.0f} cm ({forward_state})" if forward is not None else " | FORWARD OBSTACLE: DISTANCE UNAVAILABLE"),flush=True); next_log=time.monotonic()+1
            if not args.no_preview:
                detector.cv2.imshow("Sense Phase 5 attention prototype",draw_preview(frame.copy(),observations,forward,forward_state,{"attention":attention_ms,"motion":motion_ms,"inference":inference_ms},detector.cv2))
                if detector.cv2.waitKey(1)&0xFF in (ord("q"),27): break
    except KeyboardInterrupt: print("Stopping on Ctrl-C.",flush=True)
    except CameraError as exc: print(f"CAMERA FAILED: {exc}",file=sys.stderr); return 3
    except Exception as exc: print(f"PHASE 5 FAILED: {type(exc).__name__}: {exc}",file=sys.stderr); return 3
    finally:
        elapsed=time.monotonic()-stats.started_at
        if reader: reader.stop()
        if camera: camera.close()
        if ultrasonic: ultrasonic.stop()
        if sensor: sensor.close()
        if not args.no_preview: detector.cv2.destroyAllWindows()
        print(stats.summary(elapsed),flush=True)
    return 0


if __name__ == "__main__": raise SystemExit(main())
