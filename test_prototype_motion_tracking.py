"""Hardware-free tests for the Phase 3 lightweight tracker."""
import unittest

from prototype_motion_tracking import (
    APPROACHING, CROSSING_LEFT, CROSSING_RIGHT, HIGH_PRIORITY, LOST, MOVING_RIGHT,
    RECEDING, STABLE, Detection, LightweightTracker, forward_distance, parse_args, priority_for,
)


def detection(label="person", x=100, y=100, width=50, height=100, position="CENTER", class_id=0):
    return Detection(label, .9, (x, y, x + width, y + height), position, class_id)


class TrackingTests(unittest.TestCase):
    def tracker(self):
        return LightweightTracker(max_center_distance_px=120, lost_timeout_s=1.0, priority_lost_timeout_s=1.0,
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

    def test_fast_moving_person_keeps_id(self):
        tracker = self.tracker()
        track = tracker.update([detection(x=80)], 0)[0]
        tracker.update([detection(x=190)], .25)
        track = tracker.update([detection(x=300)], .5)[0]
        self.assertEqual(track.track_id, 1)
        self.assertTrue(track.confirmed)

    def test_one_missed_person_detection_reacquires_same_id(self):
        tracker = self.tracker()
        tracker.update([detection(x=100)], 0)
        tracker.update([detection(x=150)], .25)
        tracker.update([], .5)
        track = tracker.update([detection(x=220)], .75)[0]
        self.assertEqual(track.track_id, 1)
        self.assertEqual(track.motion, STABLE)

    def test_short_miss_with_large_box_change_reacquires_confirmed_person(self):
        tracker = self.tracker()
        tracker.update([detection(x=100, width=60, height=100)], 0)
        tracker.update([detection(x=110, width=60, height=100)], .25)
        tracker.update([], .5)
        track = tracker.update([detection(x=120, width=25, height=55)], .75)[0]
        self.assertEqual(track.track_id, 1)
        self.assertEqual(tracker.reacquired_count, 1)

    def test_ambiguous_lost_people_do_not_reclaim_new_detection(self):
        tracker = self.tracker()
        tracker.update([detection(x=100), detection(x=160)], 0)
        tracker.update([detection(x=105), detection(x=165)], .25)
        tracker.update([], .5)
        tracks = tracker.update([detection(x=135, width=10, height=100)], .75)
        self.assertIn(3, {track.track_id for track in tracks})

    def test_person_can_reconnect_after_several_short_misses(self):
        tracker = LightweightTracker(lost_timeout_s=1.0, priority_lost_timeout_s=2.0)
        tracker.update([detection(x=100)], 0)
        tracker.update([detection(x=150)], .25)
        tracker.update([], .5)
        tracker.update([], .75)
        track = tracker.update([detection(x=230)], 1.0)[0]
        self.assertEqual(track.track_id, 1)

    def test_expired_person_gets_new_id(self):
        tracker = self.tracker()
        tracker.update([detection()], 0)
        tracker.update([detection(x=120)], .25)
        tracker.update([], 1.26)
        track = tracker.update([detection(x=120)], 1.5)[0]
        self.assertEqual(track.track_id, 2)

    def test_predicted_center_retains_moving_person(self):
        tracker = LightweightTracker(max_center_distance_px=120, priority_center_distance_px=200)
        tracker.update([detection(x=100)], 0)
        tracker.update([detection(x=200)], .25)
        track = tracker.update([detection(x=430)], .75)[0]
        self.assertEqual(track.track_id, 1)

    def test_impossible_large_jump_creates_new_person(self):
        tracker = LightweightTracker(priority_center_distance_px=200)
        tracker.update([detection(x=100)], 0)
        tracker.update([detection(x=150)], .25)
        tracks = tracker.update([detection(x=600)], .5)
        self.assertEqual({track.track_id for track in tracks}, {1, 2})

    def test_single_frame_normal_object_stays_tentative_and_hidden(self):
        tracker = self.tracker()
        track = tracker.update([detection("hair drier", class_id=78)], 0)[0]
        self.assertFalse(track.confirmed)
        self.assertEqual(tracker.tracks_for_display(), [])

    def test_repeated_normal_object_becomes_confirmed(self):
        tracker = self.tracker()
        tracker.update([detection("chair", x=100, class_id=56)], 0)
        track = tracker.update([detection("chair", x=105, class_id=56)], .25)[0]
        self.assertTrue(track.confirmed)
        self.assertEqual(tracker.tracks_for_display(), [track])

    def test_confirmed_person_suppresses_overlapping_duplicate(self):
        tracker = self.tracker()
        tracker.update([detection(x=100)], 0)
        tracker.update([detection(x=105)], .25)
        tracks = tracker.update([detection(x=110), detection(x=112)], .5)
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].track_id, 1)

    def test_initial_overlapping_person_detections_are_filtered(self):
        tracker = self.tracker()
        tracks = tracker.update([detection(x=100), detection(x=102)], 0)
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracker.duplicate_person_detections, 1)

    def test_two_stationary_people_survive_jitter_and_short_misses(self):
        tracker = self.tracker()
        for index in range(120):
            now = index * .4
            detections = []
            if index not in {20, 21, 75}:
                detections.append(detection(x=100 + (index % 3) - 1, position="LEFT"))
            if index not in {48, 49}:
                detections.append(detection(x=350 + (index % 3) - 1, position="RIGHT"))
            tracker.update(detections, now)
        self.assertEqual(tracker.created_count, 2)
        self.assertEqual(tracker.expired_count, 0)
        self.assertEqual({track.track_id for track in tracker.tracks}, {1, 2})
        self.assertGreaterEqual(tracker.reacquired_count, 2)

    def test_continuity_counters_record_real_empty_detection_cycle(self):
        tracker = self.tracker()
        tracker.update([], 0)
        tracker.update([detection()], .4)
        tracker.update([detection(), detection(x=350)], .8)
        report = tracker.continuity_summary()
        self.assertEqual((report["zero"], report["one"], report["two_plus"]), (1, 1, 1))

    def test_high_priority_confirmation_is_two_quick_hits(self):
        tracker = self.tracker()
        first = tracker.update([detection()], 0)[0]
        first_was_confirmed = first.confirmed
        second = tracker.update([detection(x=105)], .25)[0]
        self.assertFalse(first_was_confirmed)
        self.assertTrue(second.confirmed)

    def test_two_nearby_people_remain_separate_after_confirmation(self):
        tracker = self.tracker()
        tracker.update([detection(x=80), detection(x=220)], 0)
        tracks = tracker.update([detection(x=105), detection(x=195)], .25)
        self.assertEqual({track.track_id for track in tracks}, {1, 2})
        self.assertTrue(all(track.confirmed for track in tracks))

    def test_crossing_people_are_not_intentionally_merged(self):
        tracker = self.tracker()
        tracker.update([detection(x=80), detection(x=260)], 0)
        tracker.update([detection(x=140), detection(x=200)], .25)
        tracks = tracker.update([detection(x=200), detection(x=140)], .5)
        self.assertEqual(len(tracks), 2)
        self.assertEqual({track.track_id for track in tracks}, {1, 2})

    def test_lost_normal_track_is_hidden_without_debug(self):
        tracker = self.tracker()
        tracker.update([detection("chair", class_id=56)], 0)
        tracker.update([detection("chair", x=105, class_id=56)], .25)
        tracker.update([], .5)
        self.assertEqual(tracker.tracks_for_display(), [])
        self.assertEqual(len(tracker.tracks_for_display(debug=True)), 1)

    def test_benchmark_cli_accepts_numeric_duration(self):
        self.assertEqual(parse_args(["--benchmark-seconds", "60"]).benchmark_seconds, 60)
        self.assertEqual(parse_args(["--benchmark-seconds", "0"]).benchmark_seconds, 0)


if __name__ == "__main__":
    unittest.main()
