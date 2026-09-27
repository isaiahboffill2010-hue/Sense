"""Hardware-free tests for the isolated local-vision prototype."""

import unittest

from prototype_vision import (
    BenchmarkStats,
    NewestFrameGate,
    horizontal_position,
    normalized_box_to_pixels,
    percentile,
    postprocess_detections,
    validate_model_contract,
)


class PositionTests(unittest.TestCase):
    def test_three_horizontal_zones_and_boundaries(self):
        self.assertEqual(horizontal_position(0.1), "LEFT")
        self.assertEqual(horizontal_position(1 / 3), "CENTER")
        self.assertEqual(horizontal_position(0.5), "CENTER")
        self.assertEqual(horizontal_position(2 / 3), "CENTER")
        self.assertEqual(horizontal_position(0.9), "RIGHT")

    def test_normalized_box_is_clamped(self):
        self.assertEqual(normalized_box_to_pixels((-0.2, -0.1, 1.2, 1.1), 101, 51), (0, 0, 100, 50))
        self.assertIsNone(normalized_box_to_pixels((0.5, 0.5, 0.4, 0.6), 100, 100))


class PostProcessingTests(unittest.TestCase):
    def test_threshold_labels_boxes_and_position(self):
        detections = postprocess_detections(
            boxes=[(0.1, 0.0, 0.5, 0.2), (0.2, 0.4, 0.8, 0.6), (0.1, 0.8, 0.5, 1.0)],
            classes=[0, 1, 99],
            scores=[0.8, 0.49, 0.75],
            count=3,
            labels=["person", "bicycle"],
            threshold=0.5,
            width=300,
            height=200,
        )
        self.assertEqual([item.label for item in detections], ["person", "class_99"])
        self.assertEqual([item.position for item in detections], ["LEFT", "RIGHT"])

    def test_na_label_is_not_shown(self):
        detections = postprocess_detections(
            [(0, 0, 1, 1)], [0], [0.9], 1, ["n/a"], 0.5, 100, 100
        )
        self.assertEqual(detections, [])


class FreshnessTests(unittest.TestCase):
    def test_same_frame_token_is_never_processed_twice(self):
        gate = NewestFrameGate()
        self.assertTrue(gate.accept(10))
        self.assertFalse(gate.accept(10))
        self.assertTrue(gate.accept(11))


class ModelContractTests(unittest.TestCase):
    @staticmethod
    def outputs():
        return [
            {"name": "TFLite_Detection_PostProcess"},
            {"name": "TFLite_Detection_PostProcess:1"},
            {"name": "TFLite_Detection_PostProcess:2"},
            {"name": "TFLite_Detection_PostProcess:3"},
        ]

    def test_expected_contract(self):
        validate_model_contract(
            [{"shape": [1, 300, 300, 3], "dtype": type("uint8", (), {})}], self.outputs()
        )

    def test_wrong_input_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "expected uint8 input"):
            validate_model_contract(
                [{"shape": [1, 224, 224, 3], "dtype": type("float32", (), {})}], self.outputs()
            )

    def test_missing_postprocess_output_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "post-processing"):
            validate_model_contract(
                [{"shape": [1, 300, 300, 3], "dtype": type("uint8", (), {})}], self.outputs()[:-1]
            )


class MetricsTests(unittest.TestCase):
    def test_percentile_interpolates(self):
        self.assertEqual(percentile([], 95), None)
        self.assertEqual(percentile([10], 50), 10)
        self.assertAlmostEqual(percentile([1, 2, 3, 4], 50), 2.5)
        self.assertAlmostEqual(percentile([1, 2, 3, 4], 95), 3.85)

    def test_summary_uses_real_samples(self):
        stats = BenchmarkStats(started_at=0)
        for inference in (100, 125, 150, 175, 200):
            stats.add(inference, inference + 10, 5, {"cpu": 25, "rss": 44, "available": 500, "temp": 52})
        report = stats.summary(2, __import__("pathlib").Path("model.tflite"), 1024 * 1024, "test runtime")
        self.assertIn("AVERAGE DETECTION FPS: 2.5", report)
        self.assertIn("AVERAGE INFERENCE: 150.0 ms", report)
        self.assertIn("P50 INFERENCE: 150.0 ms", report)
        self.assertIn("P95 INFERENCE: 195.0 ms", report)
        self.assertIn("PEAK PROCESS RAM: 44.0 MiB", report)
        self.assertIn("AVERAGE SYSTEM CPU: 25.0%", report)

    def test_percentiles_are_withheld_for_tiny_samples(self):
        stats = BenchmarkStats(started_at=0)
        stats.add(100, 110, 5, {"cpu": None, "rss": None, "available": None, "temp": None})
        report = stats.summary(1, __import__("pathlib").Path("model.tflite"), 1, "test runtime")
        self.assertIn("P50 INFERENCE: n/a", report)
        self.assertIn("P95 INFERENCE: n/a", report)


if __name__ == "__main__":
    unittest.main()
