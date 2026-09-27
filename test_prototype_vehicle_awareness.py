"""Hardware-free Phase 7 vehicle-awareness tests."""
from collections import deque
from types import SimpleNamespace
import unittest
from prototype_camera_motion import CAMERA_STABLE,CAMERA_UNCERTAIN,CameraMotion
from prototype_vehicle_awareness import HIGH,LOW,MEDIUM,VehicleEngine,console,format_top,group_motion,parse_args,zone_transition
from prototype_motion_tracking import forward_distance

def camera(state=CAMERA_STABLE,dx=0):return CameraMotion(dx,0,state,"HIGH",20,20)
def vehicle(label="car",zone="LEFT",*,ident=1,confirmed=True,seen=True,motion="STABLE",positions=None,centers=None):
    positions=positions or [zone,zone];centers=centers or [0,0];history=deque([SimpleNamespace(position=p,center_x=x) for p,x in zip(positions,centers)])
    return SimpleNamespace(label=label,track_id=ident,display_id=f"{label.upper()} #{ident}",position=zone,confirmed=confirmed,seen_this_update=seen,motion=motion,history=history,created_at=0)

class VehicleTests(unittest.TestCase):
    def test_one_confirmed_car(self):self.assertEqual(VehicleEngine().observe([vehicle()],camera())[0].track.display_id,"CAR #1")
    def test_multiple_tracks_independent(self):self.assertEqual([x.track.track_id for x in VehicleEngine().observe([vehicle(ident=2),vehicle(ident=1)],camera())],[1,2])
    def test_left_center(self):self.assertEqual(VehicleEngine().observe([vehicle("car","CENTER",positions=["LEFT","CENTER"])],camera())[0].transition,"LEFT→CENTER")
    def test_right_center(self):self.assertEqual(zone_transition("RIGHT","CENTER"),"RIGHT→CENTER")
    def test_center_left(self):self.assertEqual(zone_transition("CENTER","LEFT"),"CENTER→LEFT")
    def test_center_right(self):self.assertEqual(zone_transition("CENTER","RIGHT"),"CENTER→RIGHT")
    def test_reliable_corrected_motion(self):self.assertEqual(VehicleEngine().observe([vehicle(centers=[0,50])],camera())[0].motion,"MOVING RIGHT")
    def test_camera_uncertainty_suppresses_direction(self):self.assertEqual(VehicleEngine().observe([vehicle(centers=[0,50])],camera(CAMERA_UNCERTAIN))[0].motion,"UNCERTAIN")
    def test_stationary_side_vehicle_low(self):self.assertEqual(VehicleEngine().observe([vehicle()],camera())[0].level,LOW)
    def test_entering_center_increases_relevance(self):self.assertEqual(VehicleEngine().observe([vehicle("car","CENTER",positions=["LEFT","CENTER"])],camera())[0].level,HIGH)
    def test_crossing_increases_relevance(self):self.assertEqual(VehicleEngine().observe([vehicle("car","CENTER",motion="CROSSING RIGHT")],camera())[0].level,MEDIUM)
    def test_tentative_cannot_dominate(self):self.assertEqual(VehicleEngine().observe([vehicle(confirmed=False)],camera()),[])
    def test_lost_not_top(self):self.assertEqual(VehicleEngine().observe([vehicle(seen=False)],camera()),[])
    def test_parked_remains_low(self):self.assertEqual(VehicleEngine().observe([vehicle("bus","RIGHT")],camera())[0].level,LOW)
    def test_top_capped(self):self.assertEqual(len(format_top(VehicleEngine().observe([vehicle(ident=i) for i in range(8)],camera()))),3)
    def test_crowded_deterministic(self):
        result=VehicleEngine().observe([vehicle(ident=i) for i in range(8,0,-1)],camera());self.assertEqual([x.track.track_id for x in result],list(range(1,9)))
    def test_group_left_to_right(self):
        e=VehicleEngine();items=e.observe([vehicle(ident=1,centers=[0,50]),vehicle(ident=2,centers=[0,50])],camera());self.assertIn("moving right",group_motion(items,camera()) or "")
    def test_group_right_to_left(self):
        e=VehicleEngine();items=e.observe([vehicle(ident=1,centers=[50,0]),vehicle(ident=2,centers=[50,0])],camera());self.assertIn("moving left",group_motion(items,camera()))
    def test_group_disagreement_none(self):
        e=VehicleEngine();items=e.observe([vehicle(ident=1,centers=[0,50]),vehicle(ident=2,centers=[50,0])],camera());self.assertIsNone(group_motion(items,camera()))
    def test_uncertain_camera_no_group(self):self.assertIsNone(group_motion([],camera(CAMERA_UNCERTAIN)))
    def test_new_not_repeated(self):
        e=VehicleEngine();t=vehicle();self.assertEqual(e.observe([t],camera())[0].event,"NEW");self.assertEqual(e.observe([t],camera())[0].event,"ONGOING")
    def test_temporary_loss_no_duplicate_event(self):
        e=VehicleEngine();t=vehicle();e.observe([t],camera());e.observe([],camera());self.assertEqual(e.observe([t],camera())[0].event,"ONGOING")
    def test_ultrasonic_never_assigned(self):self.assertEqual(forward_distance({"distance_cm":40,"age_s":.01,"healthy":True,"out_of_range":False},.5),(40.0,"CLOSE"));self.assertFalse(hasattr(VehicleEngine().observe([vehicle()],camera())[0],"distance_cm"))
    def test_ground_unknown_does_not_stop(self):self.assertTrue(VehicleEngine().observe([vehicle()],camera()))
    def test_no_crossing_authorization(self):self.assertNotIn("SAFE TO CROSS",console([],camera()))
    def test_cli(self):self.assertEqual(parse_args(["--benchmark-seconds","60","--debug-vehicles"]).benchmark_seconds,60)
if __name__=="__main__":unittest.main()
