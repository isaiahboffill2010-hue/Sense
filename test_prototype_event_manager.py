import unittest
from prototype_event_manager import *
def e(i=1,typ="PERSON_AHEAD",priority=MEDIUM,t=0,source="person_awareness"):return Event(source,typ,"PERSON",i,t,"CENTER",typ,priority)
class T(unittest.TestCase):
 def test_new_duplicate_distinct(self):
  m=EventManager();self.assertEqual(m.process([e()],0)[0][0],"EMIT");self.assertEqual(m.process([e()],1)[0][0],"SUPPRESS");self.assertEqual(m.process([e(2)],1)[0][0],"EMIT")
 def test_escalation_budget(self):
  m=EventManager(1);m.process([e()],0);self.assertEqual(m.process([e(1,"POSSIBLE_APPROACH",HIGH)],1)[0][0],"EMIT");a=m.process([e(3),e(4),e(5)],2);self.assertEqual(sum(x[0]=="EMIT" for x in a),1)
 def test_ttl_uncertainty_payload(self):
  m=EventManager();m.process([e()],0);self.assertIn("ENDED",[x[0] for x in m.process([],5)]);u=Event("p","X","PERSON",1,0,None,"X",HIGH,reliability="UNCERTAIN");self.assertEqual(m.process([u],0)[0][0],"SUPPRESS");self.assertIn("entity_id",e().payload)
 def test_sources_do_not_collide_forbidden_simulation(self):
  self.assertNotEqual(e(source="vehicle_awareness").key,e().key);text=str(simulate(EventManager()));self.assertNotIn("SAFE TO CROSS",text);self.assertTrue(simulate(EventManager()))
