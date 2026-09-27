"""Hardware-free tests for Phase 5 local relevance ranking."""
from collections import deque
from types import SimpleNamespace
import unittest

from prototype_attention import AttentionEngine, HIGH, LOW, MEDIUM, entering_center, parse_args
from prototype_camera_motion import CAMERA_STABLE, CAMERA_UNCERTAIN, CameraMotion
from prototype_motion_tracking import CROSSING_RIGHT, LOST, forward_distance


def camera(state=CAMERA_STABLE, dx=0): return CameraMotion(dx, 0, state, "HIGH", 20, 20)
def track(label="person", position="LEFT", *, confirmed=True, seen=True, motion="STABLE", positions=None, ident=1):
    positions = positions or [position]
    history = deque([SimpleNamespace(position=value, center_x=i * 40) for i, value in enumerate(positions)], maxlen=6)
    return SimpleNamespace(label=label, track_id=ident, display_id=f"{label.upper()} #{ident}", position=position,
                           confirmed=confirmed, seen_this_update=seen, motion=motion, history=history)


class AttentionTests(unittest.TestCase):
    def test_center_person_outranks_identical_side_person(self):
        ranked=AttentionEngine().score([track(position="LEFT",ident=1),track(position="CENTER",ident=2)],camera())
        self.assertEqual(ranked[0].track.track_id,2); self.assertGreater(ranked[0].score,ranked[1].score)
    def test_moving_person_outranks_stationary_equivalent(self):
        engine=AttentionEngine(); stationary_track=track(positions=["CENTER","CENTER"]); stationary_track.history=deque([SimpleNamespace(position="CENTER",center_x=20),SimpleNamespace(position="CENTER",center_x=20)])
        still=engine.score([stationary_track],camera())[0]; moving=engine.score([track(positions=["CENTER","CENTER"])],camera(dx=-50))[0]
        self.assertGreater(moving.score,still.score)
    def test_entering_center_increases_attention(self):
        engine=AttentionEngine(); entering=engine.score([track(position="CENTER",positions=["LEFT","CENTER"])],camera())[0]; stable=engine.score([track(position="CENTER",positions=["CENTER","CENTER"],ident=2)],camera())[0]
        self.assertTrue(entering_center(entering.track)); self.assertGreater(entering.score,stable.score)
    def test_moving_away_from_center_does_not_get_entering_bonus(self):
        item=AttentionEngine().score([track(position="RIGHT",positions=["CENTER","RIGHT"])],camera())[0]
        self.assertNotIn("entering center",item.reasons)
    def test_crossing_person_increases_attention(self):
        crossing=AttentionEngine().score([track(position="CENTER",motion=CROSSING_RIGHT,positions=["LEFT","CENTER","RIGHT"])],camera())[0]
        self.assertIn("crossing",crossing.reasons); self.assertEqual(crossing.level,HIGH)
    def test_moving_vehicle_entering_center_is_high(self):
        item=AttentionEngine().score([track("car","CENTER",positions=["RIGHT","CENTER"])],camera(dx=50))[0]
        self.assertEqual(item.level,HIGH)
    def test_stationary_side_vehicle_is_not_automatically_high(self): self.assertNotEqual(AttentionEngine().score([track("car","LEFT")],camera())[0].level,HIGH)
    def test_tentative_detection_cannot_dominate(self):
        ranked=AttentionEngine().score([track("car","CENTER",confirmed=False,ident=1),track("chair","CENTER",ident=2)],camera())
        self.assertEqual(ranked[-1].track.track_id,1); self.assertEqual(ranked[-1].level,LOW)
    def test_lost_track_is_not_high(self): self.assertEqual(AttentionEngine().score([track("car","CENTER",seen=False,motion=LOST)],camera())[0].level,LOW)
    def test_camera_uncertainty_does_not_claim_corrected_evidence(self):
        item=AttentionEngine().score([track(position="CENTER",positions=["CENTER","CENTER"])],camera(CAMERA_UNCERTAIN))[0]
        self.assertIn("camera motion uncertain",item.reasons); self.assertNotIn("corrected movement",item.reasons)
    def test_multiple_people_are_ranked_independently(self):
        ranked=AttentionEngine().score([track(position="LEFT",ident=1),track(position="CENTER",positions=["RIGHT","CENTER"],ident=2),track(position="RIGHT",ident=3)],camera())
        self.assertEqual([item.track.track_id for item in ranked],[2,1,3])
    def test_ordinary_objects_remain_available(self):
        item=AttentionEngine().score([track("chair","CENTER")],camera())[0]; self.assertEqual(item.track.label,"chair"); self.assertEqual(item.level,LOW)
    def test_hysteresis_prevents_rapid_high_low_flicker(self):
        engine=AttentionEngine(); important=track("car","CENTER",positions=["LEFT","CENTER"]); self.assertEqual(engine.score([important],camera())[0].level,HIGH)
        important.confirmed=False; important.position="LEFT"; important.history=deque([SimpleNamespace(position="LEFT",center_x=0)])
        self.assertEqual(engine.score([important],camera())[0].level,HIGH); self.assertEqual(engine.score([important],camera())[0].level,MEDIUM)
    def test_attention_decays_one_step_at_a_time(self):
        engine=AttentionEngine(); subject=track("car","CENTER",positions=["LEFT","CENTER"]); engine.score([subject],camera()); subject.confirmed=False; subject.position="LEFT"; subject.history=deque([SimpleNamespace(position="LEFT",center_x=0)])
        levels=[engine.score([subject],camera())[0].level for _ in range(4)]; self.assertEqual(levels[-1],LOW); self.assertIn(MEDIUM,levels)
    def test_ultrasonic_is_independent_and_unassigned(self):
        item=AttentionEngine().score([track("chair","CENTER")],camera())[0]; distance,state=forward_distance({"distance_cm":38,"age_s":.01,"healthy":True,"out_of_range":False},.5)
        self.assertEqual((distance,state),(38.0,"CLOSE")); self.assertFalse(hasattr(item,"distance_cm"))
    def test_ranking_is_deterministic(self):
        tracks=[track("chair","LEFT",ident=2),track("chair","LEFT",ident=1)]
        self.assertEqual([item.track.track_id for item in AttentionEngine().score(tracks,camera())],[1,2])
    def test_numeric_benchmark_cli(self): self.assertEqual(parse_args(["--benchmark-seconds","60","--debug-attention"]).benchmark_seconds,60)


if __name__ == "__main__": unittest.main()
