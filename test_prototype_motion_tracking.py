"""Hardware-free tests for the Phase 3 lightweight tracker."""
import unittest

from prototype_motion_tracking import (
    APPROACHING, CROSSING_LEFT, CROSSING_RIGHT, HIGH_PRIORITY, LOST, MOVING_RIGHT,
    RECEDING, STABLE, Detection, LightweightTracker, forward_distance, priority_for,
)


def detection(label="person", x=100, y=100, width=50, height=100, position="CENTER", class_id=0):
    return Detection(label, .9, (x, y, x + width, y + height), position, class_id)


class TrackingTests(unittest.TestCase):
    def tracker(self):
        return LightweightTracker(max_center_distance_px=120, lost_timeout_s=1.0,
                                  horizontal_dead_zone_px=35, area_change_ratio=1.25)

    def test_same_person_nearby_detections_retains_id(self):
        tracker = self.tracker()
        first = tracker.update([detection(x=100)], 0)[0]
        second = tracker.update([detection(x=130)], .25)[0]
        self.assertEqual(first.display_id, "PERSON #1")
        self.assertEqual(second.track_id, first.track_id)
        self.assertEqual(tracker.created_count, 1)

    def test_different_people_receive_distinct_ids(self):
        tracks = self.tracker().update([detection(x=10), detection(x=400)], 0)
        self.assertEqual([track.display_id for track in tracks], ["PERSON #1", "PERSON #2"])

    def test_temporary_miss_marks_lost_but_keeps_track(self):
        tracker = self.tracker()
        tracker.update([detection()], 0)
        tracks = tracker.update([], .25)
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracker.visible_motion(tracks[0]), LOST)
        self.assertEqual(tracker.update([detection(x=120)], .5)[0].track_id, 1)

    def test_stale_track_expires(self):
        tracker = self.tracker()
        tracker.update([detection()], 0)
        self.assertEqual(tracker.update([], 1.01), [])
        self.assertEqual(tracker.expired_count, 1)

    def test_left_center_right_becomes_crossing_right(self):
        tracker = self.tracker()
        tracker.update([detection(x=20, position="LEFT")], 0)
        tracker.update([detection(x=120, position="CENTER")], .25)
        track = tracker.update([detection(x=220, position="RIGHT")], .5)[0]
        self.assertEqual(track.motion, CROSSING_RIGHT)

    def test_right_center_left_becomes_crossing_left(self):
        tracker = self.tracker()
        tracker.update([detection(x=220, position="RIGHT")], 0)
        tracker.update([detection(x=120, position="CENTER")], .25)
        track = tracker.update([detection(x=20, position="LEFT")], .5)[0]
        self.assertEqual(track.motion, CROSSING_LEFT)

    def test_small_jitter_is_stable(self):
        tracker = self.tracker()
        for now, x in ((0, 200), (.25, 207), (.5, 196), (.75, 204)):
            track = tracker.update([detection(x=x)], now)[0]
        self.assertEqual(track.motion, STABLE)

    def test_increasing_area_can_be_approaching(self):
        tracker = self.tracker()
        tracker.update([detection(x=200, width=30, height=50)], 0)
        tracker.update([detection(x=195, width=40, height=65)], .25)
        track = tracker.update([detection(x=188, width=55, height=85)], .5)[0]
        self.assertEqual(track.motion, APPROACHING)

    def test_decreasing_area_can_be_receding(self):
        tracker = self.tracker()
        tracker.update([detection(x=180, width=60, height=90)], 0)
        tracker.update([detection(x=190, width=45, height=70)], .25)
        track = tracker.update([detection(x=195, width=30, height=50)], .5)[0]
        self.assertEqual(track.motion, RECEDING)

    def test_multiple_people_remain_separate(self):
        tracker = self.tracker()
        tracker.update([detection(x=20, position="LEFT"), detection(x=400, position="RIGHT")], 0)
        tracks = tracker.update([detection(x=50, position="LEFT"), detection(x=370, position="RIGHT")], .25)
        self.assertEqual({track.track_id for track in tracks}, {1, 2})

    def test_vehicle_tracking_and_priority(self):
        tracker = self.tracker()
        track = tracker.update([detection("car", 100, class_id=2)], 0)[0]
        self.assertEqual(track.priority, "HIGH")
        self.assertIn("car", HIGH_PRIORITY)
        self.assertEqual(priority_for("chair"), "NORMAL")

    def test_ultrasonic_stays_independent_of_tracks(self):
        distance, state = forward_distance({"distance_cm": 23, "age_s": .02, "healthy": True, "out_of_range": False}, .5)
        self.assertEqual((distance, state), (23.0, "VERY CLOSE"))
        distance, state = forward_distance({"distance_cm": None, "age_s": None, "healthy": False}, .5)
        self.assertIsNone(distance)
        self.assertEqual(state, "DISTANCE UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
