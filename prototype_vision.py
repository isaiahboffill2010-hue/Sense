#!/usr/bin/env python3
"""Isolated real-time local object-detection prototype for Sense.

This file deliberately has no Gemini, ultrasonic, beeper, microphone, or
production assistant integration. Heavy Pi-only imports are lazy so its pure
post-processing and metrics logic can be unit tested on any development PC.
"""

from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass, field
import importlib.metadata
from pathlib import Path
import statistics
import sys
import time
from typing import Iterable, Sequence


ROOT = Path(__file__).resolve().parent
DEFAULT_MODEL = ROOT / "models" / "ssd_mobilenet_v1_coco_quant_postprocess.tflite"
DEFAULT_LABELS = ROOT / "models" / "coco_labels.txt"


@dataclass(frozen=True)
class Detection:
    label: str
    confidence: float
    box: tuple[int, int, int, int]
    position: str
    class_id: int


def horizontal_position(center_x: float) -> str:
    """Map a normalized horizontal centre into the preview's three zones."""
    if center_x < 1.0 / 3.0:
        return "LEFT"
    if center_x > 2.0 / 3.0:
        return "RIGHT"
    return "CENTER"


def normalized_box_to_pixels(
    box: Sequence[float], width: int, height: int
) -> tuple[int, int, int, int] | None:
    """Convert [ymin, xmin, ymax, xmax] to a clamped OpenCV rectangle."""
    if len(box) != 4 or width <= 0 or height <= 0:
        return None
    ymin, xmin, ymax, xmax = (float(value) for value in box)
    xmin = min(1.0, max(0.0, xmin))
    xmax = min(1.0, max(0.0, xmax))
    ymin = min(1.0, max(0.0, ymin))
    ymax = min(1.0, max(0.0, ymax))
    left, right = round(xmin * (width - 1)), round(xmax * (width - 1))
    top, bottom = round(ymin * (height - 1)), round(ymax * (height - 1))
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def postprocess_detections(
    boxes: Sequence[Sequence[float]],
    classes: Sequence[float],
    scores: Sequence[float],
    count: float,
    labels: Sequence[str],
    threshold: float,
    width: int,
    height: int,
) -> list[Detection]:
    accepted: list[Detection] = []
    limit = min(max(0, int(count)), len(boxes), len(classes), len(scores))
    for index in range(limit):
        confidence = float(scores[index])
        if confidence < threshold:
            continue
        class_id = int(round(float(classes[index])))
        box = normalized_box_to_pixels(boxes[index], width, height)
        if box is None:
            continue
        label = labels[class_id] if 0 <= class_id < len(labels) else f"class_{class_id}"
        if not label or label.lower() == "n/a":
            continue
        left, _, right, _ = box
        accepted.append(
            Detection(
                label=label,
                confidence=confidence,
                box=box,
                position=horizontal_position(((left + right) / 2.0) / width),
                class_id=class_id,
            )
        )
    return accepted


class NewestFrameGate:
    """Reject a repeated capture object; never creates a frame backlog."""

    def __init__(self) -> None:
        self._last_token = None

    def accept(self, token: object) -> bool:
        if token == self._last_token:
            return False
        self._last_token = token
        return True


def percentile(values: Sequence[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    rank = min(100.0, max(0.0, percent)) / 100.0 * (len(ordered) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def validate_model_contract(input_details: Sequence[dict], output_details: Sequence[dict]) -> None:
    """Fail loudly if a different TFLite model is supplied by accident."""
    if len(input_details) != 1:
        raise ValueError(f"expected one input tensor, found {len(input_details)}")
    input_shape = tuple(int(value) for value in input_details[0]["shape"])
    dtype_name = getattr(input_details[0]["dtype"], "__name__", str(input_details[0]["dtype"]))
    if input_shape != (1, 300, 300, 3) or "uint8" not in dtype_name:
        raise ValueError(f"expected uint8 input [1,300,300,3], found {dtype_name} {input_shape}")
    names = {detail.get("name", "") for detail in output_details}
    required = {
        "TFLite_Detection_PostProcess",
        "TFLite_Detection_PostProcess:1",
        "TFLite_Detection_PostProcess:2",
        "TFLite_Detection_PostProcess:3",
    }
    if not required.issubset(names):
        raise ValueError("model does not expose the expected SSD post-processing tensors")


class SystemMonitor:
    def __init__(self) -> None:
        self._last_cpu: tuple[int, int] | None = None

    @staticmethod
    def _read_lines(path: str) -> list[str]:
        try:
            return Path(path).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            return []

    def cpu_percent(self) -> float | None:
        lines = self._read_lines("/proc/stat")
        if not lines or not lines[0].startswith("cpu "):
            return None
        try:
            values = [int(value) for value in lines[0].split()[1:]]
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            total = sum(values)
        except (ValueError, IndexError):
            return None
        previous, self._last_cpu = self._last_cpu, (idle, total)
        if previous is None or total <= previous[1]:
            return None
        return 100.0 * (1.0 - (idle - previous[0]) / (total - previous[1]))

    def memory(self) -> tuple[float | None, float | None]:
        rss_kib = available_kib = None
        for line in self._read_lines("/proc/self/status"):
            if line.startswith("VmRSS:"):
                try:
                    rss_kib = float(line.split()[1])
                except (ValueError, IndexError):
                    pass
                break
        for line in self._read_lines("/proc/meminfo"):
            if line.startswith("MemAvailable:"):
                try:
                    available_kib = float(line.split()[1])
                except (ValueError, IndexError):
                    pass
                break
        return (
            None if rss_kib is None else rss_kib / 1024.0,
            None if available_kib is None else available_kib / 1024.0,
        )

    def temperature_c(self) -> float | None:
        lines = self._read_lines("/sys/class/thermal/thermal_zone0/temp")
        try:
            return float(lines[0]) / 1000.0 if lines else None
        except ValueError:
            return None

    def sample(self) -> dict[str, float | None]:
        rss, available = self.memory()
        return {
            "cpu": self.cpu_percent(),
            "rss": rss,
            "available": available,
            "temp": self.temperature_c(),
        }


@dataclass
class BenchmarkStats:
    started_at: float = field(default_factory=time.monotonic)
    inference_ms: list[float] = field(default_factory=list)
    total_ms: list[float] = field(default_factory=list)
    freshness_ms: list[float] = field(default_factory=list)
    cpu: list[float] = field(default_factory=list)
    rss: list[float] = field(default_factory=list)
    available: list[float] = field(default_factory=list)
    temperature: list[float] = field(default_factory=list)

    def add(self, inference_ms: float, total_ms: float, freshness_ms: float, sample: dict) -> None:
        self.inference_ms.append(inference_ms)
        self.total_ms.append(total_ms)
        self.freshness_ms.append(freshness_ms)
        for key, target in (
            ("cpu", self.cpu), ("rss", self.rss), ("available", self.available),
            ("temp", self.temperature),
        ):
            value = sample.get(key)
            if value is not None:
                target.append(float(value))

    @staticmethod
    def _number(value: float | None, suffix: str = "") -> str:
        return "n/a" if value is None else f"{value:.1f}{suffix}"

    def summary(self, elapsed: float, model: Path, model_size: int, runtime: str) -> str:
        count = len(self.inference_ms)
        fps = count / elapsed if elapsed > 0 else None
        enough_for_percentiles = count >= 5
        lines = [
            "\n=== LOCAL VISION BENCHMARK ===",
            f"MODEL: {model.name}",
            f"MODEL SIZE: {model_size:,} bytes ({model_size / (1024 * 1024):.2f} MiB)",
            "MODEL INPUT: 300x300 uint8",
            f"RUNTIME: {runtime}",
            f"FRAMES ANALYZED: {count} in {elapsed:.1f}s",
            f"AVERAGE DETECTION FPS: {self._number(fps)}",
            f"AVERAGE INFERENCE: {self._number(statistics.fmean(self.inference_ms) if self.inference_ms else None, ' ms')}",
            f"P50 INFERENCE: {self._number(percentile(self.inference_ms, 50) if enough_for_percentiles else None, ' ms')}",
            f"P95 INFERENCE: {self._number(percentile(self.inference_ms, 95) if enough_for_percentiles else None, ' ms')}",
            f"AVERAGE TOTAL CYCLE: {self._number(statistics.fmean(self.total_ms) if self.total_ms else None, ' ms')}",
            f"AVERAGE FRAME FRESHNESS: {self._number(statistics.fmean(self.freshness_ms) if self.freshness_ms else None, ' ms')}",
            f"PEAK PROCESS RAM: {self._number(max(self.rss) if self.rss else None, ' MiB')}",
            f"MINIMUM SYSTEM RAM AVAILABLE: {self._number(min(self.available) if self.available else None, ' MiB')}",
            f"AVERAGE SYSTEM CPU: {self._number(statistics.fmean(self.cpu) if self.cpu else None, '%')}",
            f"MAX CPU TEMP: {self._number(max(self.temperature) if self.temperature else None, ' C')}",
            "==============================",
        ]
        return "\n".join(lines)


class LiteRTDetector:
    def __init__(self, model: Path, labels: Path, threads: int) -> None:
        try:
            import cv2
            import numpy as np
            from ai_edge_litert.interpreter import Interpreter
        except ImportError as exc:
            raise RuntimeError(
                f"local vision dependency missing: {exc}. Install prototype_requirements.txt "
                "inside a venv created with --system-site-packages"
            ) from exc
        self.cv2, self.np = cv2, np
        self.labels = [line.strip() for line in labels.read_text(encoding="utf-8").splitlines()]
        self.interpreter = Interpreter(model_path=str(model), num_threads=threads)
        self.interpreter.allocate_tensors()
        inputs = self.interpreter.get_input_details()
        outputs = self.interpreter.get_output_details()
        validate_model_contract(inputs, outputs)
        self.input_index = inputs[0]["index"]
        self.output_indices = {detail["name"]: detail["index"] for detail in outputs}
        try:
            version = importlib.metadata.version("ai-edge-litert")
        except importlib.metadata.PackageNotFoundError:
            version = "unknown"
        self.runtime = f"ai-edge-litert {version}"

    def detect(self, frame, threshold: float) -> tuple[list[Detection], float]:
        if getattr(frame, "ndim", None) != 3 or frame.shape[2] != 3:
            raise ValueError(
                f"unsupported camera frame shape {getattr(frame, 'shape', None)}; expected HxWx3 BGR"
            )
        rgb = self.cv2.cvtColor(frame, self.cv2.COLOR_BGR2RGB)
        resized = self.cv2.resize(rgb, (300, 300), interpolation=self.cv2.INTER_AREA)
        tensor = self.np.expand_dims(resized.astype(self.np.uint8, copy=False), axis=0)
        self.interpreter.set_tensor(self.input_index, tensor)
        started = time.perf_counter()
        self.interpreter.invoke()
        inference_ms = (time.perf_counter() - started) * 1000.0
        output = lambda name: self.interpreter.get_tensor(self.output_indices[name])[0]
        boxes = output("TFLite_Detection_PostProcess")
        classes = output("TFLite_Detection_PostProcess:1")
        scores = output("TFLite_Detection_PostProcess:2")
        count = float(output("TFLite_Detection_PostProcess:3"))
        height, width = frame.shape[:2]
        return postprocess_detections(
            boxes, classes, scores, count, self.labels, threshold, width, height
        ), inference_ms


def format_metric(value: float | None, suffix: str = "") -> str:
    return "n/a" if value is None else f"{value:.1f}{suffix}"


def draw_preview(frame, detections: Iterable[Detection], metrics: dict, cv2):
    height, width = frame.shape[:2]
    for boundary in (width // 3, 2 * width // 3):
        cv2.line(frame, (boundary, 0), (boundary, height), (90, 90, 90), 1)
    for detection in detections:
        left, top, right, bottom = detection.box
        color = (50, 220, 50) if detection.position == "CENTER" else (0, 190, 255)
        cv2.rectangle(frame, (left, top), (right, bottom), color, 2)
        text = f"{detection.label} {detection.confidence:.0%} {detection.position}"
        cv2.putText(frame, text, (left, max(18, top - 6)), cv2.FONT_HERSHEY_SIMPLEX, .52, color, 2)
    lines = [
        "infer {}ms  cycle {}ms  detect {}fps  camera {}fps".format(
            format_metric(metrics["inference"]), format_metric(metrics["total"]),
            format_metric(metrics["detection_fps"]), format_metric(metrics["camera_fps"]),
        ),
        "fresh {}ms  CPU {}  RSS {}  avail {}  temp {}".format(
            format_metric(metrics["freshness"]), format_metric(metrics["cpu"], "%"),
            format_metric(metrics["rss"], "MiB"), format_metric(metrics["available"], "MiB"),
            format_metric(metrics["temp"], "C"),
        ),
    ]
    for index, text in enumerate(lines):
        y = height - 34 + index * 20
        cv2.putText(frame, text, (7, y), cv2.FONT_HERSHEY_SIMPLEX, .43, (0, 0, 0), 3)
        cv2.putText(frame, text, (7, y), cv2.FONT_HERSHEY_SIMPLEX, .43, (255, 255, 255), 1)
    return frame


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS)
    parser.add_argument("--confidence", type=float, default=0.50)
    parser.add_argument("--max-detection-fps", type=float, default=5.0, help="0 means unlimited")
    parser.add_argument("--benchmark-seconds", type=float, default=60.0, help="0 runs until Q/Ctrl-C")
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--camera-width", type=int, default=640)
    parser.add_argument("--camera-height", type=int, default=480)
    parser.add_argument("--no-preview", action="store_true", help="benchmark without an X/Wayland window")
    args = parser.parse_args(argv)
    if not 0.0 <= args.confidence <= 1.0:
        parser.error("--confidence must be between 0 and 1")
    if args.max_detection_fps < 0 or args.benchmark_seconds < 0 or args.threads < 1:
        parser.error("FPS/seconds must be non-negative and threads must be at least 1")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    for required in (args.model, args.labels):
        if not required.is_file():
            print(f"MISSING MODEL ASSET: {required}\nRun: python3 download_prototype_model.py", file=sys.stderr)
            return 2
    try:
        detector = LiteRTDetector(args.model, args.labels, args.threads)
        from hardware.camera import Camera, CameraError, CameraReader
    except Exception as exc:
        print(f"LOCAL VISION STARTUP FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    camera = reader = None
    stats = BenchmarkStats()
    monitor = SystemMonitor()
    gate = NewestFrameGate()
    detection_times: deque[float] = deque(maxlen=30)
    last_started = 0.0
    next_log = 0.0
    print("SENSE LOCAL VISION PROTOTYPE", flush=True)
    print(f"Runtime: {detector.runtime} (CPU, {args.threads} threads)", flush=True)
    print(f"Model: {args.model.name} ({args.model.stat().st_size:,} bytes)", flush=True)
    print("Input tensor: 1x300x300x3 uint8", flush=True)
    print(f"Confidence threshold: {args.confidence:.2f}", flush=True)
    print(f"Detection rate limit: {args.max_detection_fps:g} FPS", flush=True)
    print("Gemini: DISABLED (local inference only; zero API calls)", flush=True)
    try:
        camera = Camera(
            resolution=(args.camera_width, args.camera_height), pixel_format="RGB888"
        ).open()
        reader = CameraReader(camera)
        reader.start()
        print(f"Camera: {camera.info}; press Q or Esc to stop", flush=True)
        print("Starting detection...", flush=True)
        # Benchmark time starts after model/camera startup and sensor warm-up.
        stats = BenchmarkStats()
        while True:
            now = time.monotonic()
            if args.benchmark_seconds and now - stats.started_at >= args.benchmark_seconds:
                break
            if args.max_detection_fps and now - last_started < 1.0 / args.max_detection_fps:
                if not args.no_preview and detector.cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                    break
                time.sleep(0.002)
                continue
            frame, age = reader.latest()
            if frame is None or not gate.accept(id(frame)):
                reader.report_stall_once()
                if not args.no_preview and detector.cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                    break
                time.sleep(0.002)
                continue

            last_started = time.monotonic()
            cycle_started = time.perf_counter()
            detections, inference_ms = detector.detect(frame, args.confidence)
            total_ms = (time.perf_counter() - cycle_started) * 1000.0
            detection_times.append(time.monotonic())
            detection_fps = None
            if len(detection_times) >= 2 and detection_times[-1] > detection_times[0]:
                detection_fps = (len(detection_times) - 1) / (detection_times[-1] - detection_times[0])
            system = monitor.sample()
            camera_state = reader.snapshot()
            metrics = {
                "inference": inference_ms, "total": total_ms,
                "detection_fps": detection_fps, "camera_fps": camera_state["fps"],
                "freshness": age * 1000.0, **system,
            }
            stats.add(inference_ms, total_ms, age * 1000.0, system)
            if time.monotonic() >= next_log:
                objects = ", ".join(
                    f"{item.label} {item.confidence:.0%} {item.position}" for item in detections
                ) or "none"
                print(
                    f"DETECT: {objects} | infer={inference_ms:.1f}ms "
                    f"fps={format_metric(detection_fps)} fresh={age * 1000:.1f}ms",
                    flush=True,
                )
                next_log = time.monotonic() + 1.0
            if not args.no_preview:
                preview = draw_preview(frame.copy(), detections, metrics, detector.cv2)
                detector.cv2.imshow("Sense local vision prototype", preview)
                if detector.cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                    break
    except KeyboardInterrupt:
        print("Stopping on Ctrl-C.", flush=True)
    except CameraError as exc:
        print(f"CAMERA FAILED: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:
        print(f"LOCAL VISION FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    finally:
        elapsed = time.monotonic() - stats.started_at
        if reader is not None:
            reader.stop()
        if camera is not None:
            camera.close()
        if not args.no_preview:
            detector.cv2.destroyAllWindows()
        print(stats.summary(elapsed, args.model, args.model.stat().st_size, detector.runtime), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
