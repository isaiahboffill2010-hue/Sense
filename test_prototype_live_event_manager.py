import inspect,unittest
from prototype_event_manager import Event,EventManager
class T(unittest.TestCase):
 def test_single_pipeline_delegate(self):
  import prototype_live_event_manager as m;self.assertIn("prototype_person_awareness",inspect.getsource(m));self.assertIn("--event-manager",inspect.getsource(m))
 def test_live_escalation(self):
  manager=EventManager();ahead=Event("person_awareness","PERSON_AHEAD","PERSON",1,0,"CENTER","PERSON AHEAD","MEDIUM");approach=Event("person_awareness","POSSIBLE_APPROACH","PERSON",1,1,"CENTER","POSSIBLE APPROACH","HIGH");self.assertEqual(manager.process([ahead],0)[0][0],"EMIT");self.assertEqual(manager.process([approach],1)[0][0],"EMIT")
