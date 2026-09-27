"""Hardware-free tests for conservative Phase 2 sensor fusion."""

import unittest

from prototype_sensor_fusion import (
    ASSOCIATION_AMBIGUOUS,
    ASSOCIATION_DISTANCE_UNAVAILABLE,
    ASSOCIATION_NONE,
    ASSOCIATION_POSSIBLE,
    ASSOCIATION_STALE,
    Detection,
    FusionStats,
    association_zone,
    distance_band,
    fuse,
    zone_overlap_ratio,
)


def detection(label: str, box: tuple[int, int, int, int], confidence: float = .9) -> Detection:
    return Detection(label, confidence, box, "CENTER", 0)


def snapshot(distance: float | None = 72, age: float | None = .02, **extra) -> dict:
    state = {
        "distance_cm": distance,
        "age_s": age,
        "healthy": True,
        "out_of_range": False,
        "error": None,
        "reading_count": 1,
    }
    state.update(extra)
    return state


class AssociationZoneTests(unittest.TestCase):
    def test_centered_zone_and_overlap(self):
        self.assertEqual(association_zone(300, .34), (99, 201))
        self.assertAlmostEqual(zone_overlap_ratio((75, 0, 125, 50), (100, 200)), .5)
        self.assertEqual(zone_overlap_ratio((0, 0, 50, 50), (100, 200)), 0)

    def test_distance_bands_are_visual_only(self):
        self.assertEqual(distance_band(101), "FAR")
        self.assertEqual(distance_band(100), "NEAR")
        self.assertEqual(distance_band(49), "CLOSE")
        self.assertEqual(distance_band(24), "VERY CLOSE")


class FusionTests(unittest.TestCase):
    def fuse(self, detections, sensor=None, vision_age=.02):
        return fuse(detections, snapshot() if sensor is None else sensor, vision_age, 300, .34, .25, .50, .75, .5)

    def test_center_detection_is_only_a_possible_target(self):
        result = self.fuse([detection("person", (120, 10, 180, 100))])
        self.assertEqual(result.association, ASSOCIATION_POSSIBLE)
        self.assertEqual([item.label for item in result.candidates], ["person"])
        self.assertEqual(result.forward_distance_cm, 72)

    def test_left_detection_does_not_receive_forward_distance(self):
        result = self.fuse([detection("chair", (0, 10, 70, 100))])
        self.assertEqual(result.association, ASSOCIATION_NONE)
        self.assertEqual(result.forward_distance_cm, 72)

    def test_low_confidence_center_detection_is_not_a_candidate(self):
        result = self.fuse([detection("person", (120, 10, 180, 100), .49)])
        self.assertEqual(result.association, ASSOCIATION_NONE)

    def test_multiple_center_detections_are_ambiguous(self):
        result = self.fuse([
            detection("person", (120, 10, 165, 100)),
            detection("chair", (170, 10, 220, 100)),
        ])
        self.assertEqual(result.association, ASSOCIATION_AMBIGUOUS)
        self.assertEqual(len(result.candidates), 2)

    def test_stale_ultrasonic_never_associates(self):
        result = self.fuse([detection("person", (120, 10, 180, 100))], snapshot(age=.51))
        self.assertEqual(result.association, ASSOCIATION_STALE)
        self.assertEqual(result.forward_distance_cm, 72)

    def test_stale_vision_never_associates(self):
        result = self.fuse([detection("person", (120, 10, 180, 100))], vision_age=.76)
        self.assertEqual(result.association, ASSOCIATION_STALE)

    def test_invalid_ultrasonic_does_not_invent_distance(self):
        result = self.fuse([detection("person", (120, 10, 180, 100))], snapshot(None, None, healthy=False, error="no echo"))
        self.assertEqual(result.association, ASSOCIATION_DISTANCE_UNAVAILABLE)
        self.assertIsNone(result.forward_distance_cm)

    def test_out_of_range_ultrasonic_is_not_presented_as_forward_obstacle(self):
        result = self.fuse([], snapshot(450, .02, out_of_range=True))
        self.assertEqual(result.association, ASSOCIATION_DISTANCE_UNAVAILABLE)
        self.assertIsNone(result.forward_distance_cm)

    def test_vision_never_vetoes_a_real_forward_obstacle(self):
        result = self.fuse([], snapshot(23, .02))
        self.assertEqual(result.forward_distance_cm, 23)
        self.assertEqual(result.distance_band, "VERY CLOSE")
        self.assertEqual(result.association, ASSOCIATION_NONE)


class FusionMetricsTests(unittest.TestCase):
    def test_benchmark_reports_total_cycle_and_fusion_time(self):
        stats = FusionStats(started_at=0)
        result = fuse([], snapshot(), .02, 300, .34, .25, .50, .75, .5)
        for _ in range(5):
            stats.add(200, 205, .1, snapshot(), {"cpu": 80, "rss": 42, "temp": 60}, result)
        report = stats.summary(1, 10)
        self.assertIn("Average total cycle: 205.0 ms", report)
        self.assertIn("Average fusion time: 0.1 ms", report)


if __name__ == "__main__":
    unittest.main()
