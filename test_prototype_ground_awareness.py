"""Hardware-free tests for Phase 6 conservative ground-awareness decisions."""
from collections import deque
from types import SimpleNamespace
import unittest

from prototype_camera_motion import CAMERA_RIGHT, CAMERA_STABLE, CAMERA_UNCERTAIN, CameraMotion
from prototype_ground_awareness import (NOT_OBSERVED, OCCUPIED, OPEN, OTHER_GROUND, POSSIBLE, ROAD, SIDEWALK, UNKNOWN, GroundEvidence, TransitionHistory, classify_ground, concise, ground_analysis_reliable, parse_args, spaces_from_fractions)
from prototype_motion_tracking import forward_distance


def evidence(visible=.8, sidewalk=.1, road=.1, conflict=0, boundary=0): return GroundEvidence(visible,sidewalk,road,conflict,boundary)
def track(box, *, confirmed=True, seen=True): return SimpleNamespace(box=box,confirmed=confirmed,seen_this_update=seen)
def camera(state=CAMERA_STABLE): return CameraMotion(0,0,state,"HIGH",20,20)

class GroundTests(unittest.TestCase):
    def test_strong_sidewalk_evidence(self): self.assertEqual(classify_ground(evidence(sidewalk=.85,road=.15)),SIDEWALK)
    def test_strong_road_evidence(self): self.assertEqual(classify_ground(evidence(sidewalk=.1,road=.88)),ROAD)
    def test_weak_or_conflicting_evidence_is_unknown(self): self.assertEqual(classify_ground(evidence(sidewalk=.5,road=.48,conflict=.4)),UNKNOWN)
    def test_insufficient_ground_is_unknown(self): self.assertEqual(classify_ground(evidence(visible=.2,sidewalk=.9)),UNKNOWN)
    def test_nonsemantic_pavement_evidence_is_other_ground(self): self.assertEqual(classify_ground(evidence(visible=.75,sidewalk=.5,road=.4)),OTHER_GROUND)
    def test_left_center_right_open_from_ground_mask(self): self.assertEqual(spaces_from_fractions((1,1,1),[],90,100),(OPEN,OPEN,OPEN))
    def test_confirmed_contact_object_occupies_its_region(self): self.assertEqual(spaces_from_fractions((1,1,1),[track((32,20,55,85))],90,100),(OPEN,OCCUPIED,OPEN))
    def test_tentative_or_high_object_does_not_block(self): self.assertEqual(spaces_from_fractions((1,1,1),[track((32,20,55,85),confirmed=False)],90,100)[1],OPEN)
    def test_transition_requires_persistence(self):
        history=TransitionHistory(); self.assertEqual(history.update(True,True),UNKNOWN); self.assertEqual(history.update(True,True),POSSIBLE)
    def test_one_frame_transition_does_not_promote(self):
        history=TransitionHistory(); history.update(True,True); self.assertEqual(history.update(False,True),NOT_OBSERVED)
    def test_uncertainty_suppresses_transition(self):
        history=TransitionHistory(); history.update(True,True); self.assertEqual(history.update(True,False),UNKNOWN)
    def test_significant_camera_motion_is_unreliable(self): self.assertFalse(ground_analysis_reliable(camera(CAMERA_RIGHT)))
    def test_camera_uncertainty_is_unreliable(self): self.assertFalse(ground_analysis_reliable(camera(CAMERA_UNCERTAIN)))
    def test_no_exact_curb_distance_is_invented(self): self.assertFalse(hasattr(TransitionHistory(),"distance_cm"))
    def test_ultrasonic_is_independent(self): self.assertEqual(forward_distance({"distance_cm":52,"age_s":.01,"healthy":True,"out_of_range":False},.5),(52.0,"NEAR"))
    def test_output_has_no_movement_authorization(self):
        result=SimpleNamespace(category=SIDEWALK,spaces=(OPEN,UNKNOWN,UNKNOWN),transition=NOT_OBSERVED)
        text=concise(result,camera()); self.assertNotIn("SAFE TO WALK",text); self.assertNotIn("CROSS",text)
    def test_identical_inputs_are_deterministic(self): self.assertEqual(classify_ground(evidence(sidewalk=.8)),classify_ground(evidence(sidewalk=.8)))
    def test_numeric_benchmark_cli(self): self.assertEqual(parse_args(["--benchmark-seconds","60","--no-preview"]).benchmark_seconds,60)

if __name__=="__main__": unittest.main()
