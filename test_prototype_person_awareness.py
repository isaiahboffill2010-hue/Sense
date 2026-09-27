from collections import deque
from types import SimpleNamespace
import unittest
import inspect
from prototype_camera_motion import CAMERA_STABLE,CAMERA_UNCERTAIN,CameraMotion
from prototype_person_awareness import *
def cam(s=CAMERA_STABLE,dx=0):return CameraMotion(dx,0,s,"HIGH",20,20)
def person(zone="LEFT",i=1,pos=None,areas=None,centers=None,ok=True):
 pos=pos or [zone,zone];areas=areas or [1000]*len(pos);centers=centers or [0]*len(pos);h=deque([SimpleNamespace(position=p,area=a,center_x=c) for p,a,c in zip(pos,areas,centers)])
 return SimpleNamespace(label="person",track_id=i,display_id=f"PERSON #{i}",position=zone,confirmed=ok,seen_this_update=ok,history=h,box=(200,100,400,350))
class T(unittest.TestCase):
 def test_confirmed_multiple(self):self.assertEqual(len(PersonEngine().observe([person(i=1),person(i=2)],cam())),2)
 def test_transition_cross(self):self.assertIn("ENTERING",PersonEngine().observe([person("CENTER",pos=["LEFT","CENTER"])],cam())[0].transition);self.assertEqual(crossing(person(pos=["LEFT","CENTER","RIGHT"]).history),"CROSSING LEFT→RIGHT")
 def test_camera(self):self.assertEqual(PersonEngine().observe([person(centers=[0,30])],cam(dx=30))[0].motion,"LOW MOTION");self.assertEqual(PersonEngine().observe([person(centers=[0,30])],cam(CAMERA_UNCERTAIN))[0].motion,"UNCERTAIN")
 def test_side_tentative(self):self.assertEqual(PersonEngine().observe([person()],cam())[0].level,LOW);self.assertFalse(PersonEngine().observe([person(ok=False)],cam()))
 def test_approach(self):
  self.assertNotEqual(PersonEngine().observe([person("CENTER",areas=[1000,1400])],cam())[0].approach,POSSIBLE_APPROACH);self.assertEqual(PersonEngine().observe([person("CENTER",pos=["CENTER"]*4,areas=[1000,1250,1500,1800])],cam())[0].approach,POSSIBLE_APPROACH)
 def test_lateral_crossing_growth_is_not_approach(self):
  p=person("RIGHT",pos=["LEFT","LEFT","CENTER","RIGHT"],areas=[1000,1300,1600,1900],centers=[60,120,300,500]);item=PersonEngine().observe([p],cam())[0];self.assertEqual(item.crossing,"CROSSING LEFT→RIGHT");self.assertEqual(item.approach,NO_APPROACH);self.assertIn("suppressed: strong lateral traversal",item.reasons)
 def test_diagonal_but_stable_center_can_approach(self):
  p=person("CENTER",pos=["CENTER"]*4,areas=[1000,1250,1500,1800],centers=[300,310,305,315]);self.assertEqual(PersonEngine().observe([p],cam())[0].approach,POSSIBLE_APPROACH)
 def test_ahead(self):self.assertTrue(PersonEngine().observe([person("CENTER")],cam())[0].ahead)
 def test_event_group_top(self):
  e=PersonEngine();x=person(centers=[0,30]);self.assertEqual(e.observe([x],cam())[0].event,"NEW");self.assertEqual(e.observe([x],cam())[0].event,"ONGOING");items=e.observe([person(i=1,centers=[0,30]),person(i=2,centers=[0,30]),person(i=3),person(i=4)],cam());self.assertEqual(len(format_top(items)),3);self.assertIn("moving right",group_motion(items,cam()))
 def test_forbidden_cli(self):self.assertNotIn("SAFE TO CROSS",console([],cam()));self.assertEqual(parse_args(["--benchmark-seconds","60"]).benchmark_seconds,60)
 def test_runtime_wiring_exists(self):
  import prototype_person_awareness as module
  source=inspect.getsource(module.main);self.assertIn("CameraReader",source);self.assertIn("LiteRTDetector",source);self.assertIn("LightweightTracker",source);self.assertIn("SparseCameraMotionEstimator",source);self.assertIn("while True",source)
