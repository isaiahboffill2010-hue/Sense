"""Hardware-free tests for Phase 4 camera-motion calculations."""
import unittest

from prototype_camera_motion import CAMERA_DOWN, CAMERA_LEFT, CAMERA_RIGHT, CAMERA_STABLE, CAMERA_UNCERTAIN, CAMERA_UP, CameraMotion, MotionSmoother, corrected_motion, feature_is_masked, parse_args, robust_camera_motion
from prototype_motion_tracking import LightweightTracker, forward_distance
from prototype_vision import Detection

def vectors(dx, dy, count=20): return [(dx, dy)] * count
def person(x): return Detection("person", .9, (x, 100, x + 50, 200), "CENTER", 0)

class CameraMotionTests(unittest.TestCase):
    def test_zero_and_small_displacement_are_stable(self): self.assertEqual(robust_camera_motion(vectors(1, -2)).state, CAMERA_STABLE)
    def test_global_left_scene_shift_means_camera_turning_right(self): self.assertEqual(robust_camera_motion(vectors(-18, 1)).state, CAMERA_RIGHT)
    def test_global_right_scene_shift_means_camera_turning_left(self): self.assertEqual(robust_camera_motion(vectors(18, 0)).state, CAMERA_LEFT)
    def test_vertical_shift_classification(self):
        self.assertEqual(robust_camera_motion(vectors(0, 16)).state, CAMERA_UP); self.assertEqual(robust_camera_motion(vectors(0, -16)).state, CAMERA_DOWN)
    def test_jitter_remains_stable_after_smoothing(self):
        smoother = MotionSmoother(size=3, dead_zone_px=4)
        for dx in (2, -3, 1): result = smoother.update(robust_camera_motion(vectors(dx, 0)))
        self.assertEqual(result.state, CAMERA_STABLE)
    def test_insufficient_features_are_uncertain(self): self.assertEqual(robust_camera_motion(vectors(-20, 0, 7)).state, CAMERA_UNCERTAIN)
    def test_outlier_vectors_do_not_dominate_median(self): self.assertEqual(robust_camera_motion(vectors(-16, 1, 20) + vectors(100, -90, 3)).state, CAMERA_RIGHT)
    def test_dynamic_object_feature_masking(self):
        self.assertTrue(feature_is_masked(75, 75, [(100, 100, 200, 200)], .5)); self.assertFalse(feature_is_masked(20, 20, [(100, 100, 200, 200)], .5))
    def test_same_object_and_camera_displacement_gives_low_corrected_motion(self):
        tracker = LightweightTracker(); tracker.update([person(100)], 0); track = tracker.update([person(80)], .25)[0]
        self.assertEqual(corrected_motion(track, CameraMotion(-20, 0, CAMERA_RIGHT, "HIGH", 20, 20)), "LOW MOTION")
    def test_residual_object_displacement_remains_visible(self):
        tracker = LightweightTracker(); tracker.update([person(100)], 0); track = tracker.update([person(150)], .25)[0]
        self.assertEqual(corrected_motion(track, CameraMotion(-20, 0, CAMERA_RIGHT, "HIGH", 20, 20)), "MOVING RIGHT")
    def test_track_ids_are_unaffected_by_compensation(self):
        tracker = LightweightTracker(); first = tracker.update([person(100)], 0)[0]; second = tracker.update([person(80)], .25)[0]
        corrected_motion(second, CameraMotion(-20, 0, CAMERA_RIGHT, "HIGH", 20, 20)); self.assertEqual(first.track_id, second.track_id)
    def test_ultrasonic_is_independent(self): self.assertEqual(forward_distance({"distance_cm": 14, "age_s": .01, "healthy": True, "out_of_range": False}, .5), (14.0, "VERY CLOSE"))
    def test_feature_loss_never_invents_direction(self): self.assertEqual(robust_camera_motion([]).state, CAMERA_UNCERTAIN)
    def test_numeric_benchmark_cli(self): self.assertEqual(parse_args(["--benchmark-seconds", "60"]).benchmark_seconds, 60)

if __name__ == "__main__": unittest.main()
