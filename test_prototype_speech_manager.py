import unittest
from prototype_event_manager import Event,HIGH,MEDIUM
from prototype_speech_manager import SpeechManager,phrase_for
def e(t="PERSON_AHEAD",p=MEDIUM,state="PERSON AHEAD"):return Event("person_awareness",t,"PERSON",1,0,"CENTER",state,p)
class T(unittest.TestCase):
 def test_mapping(self):self.assertEqual(phrase_for(e()),"Person ahead.");self.assertEqual(phrase_for(e("POSSIBLE_APPROACH",HIGH)),"Someone appears to be approaching.")
 def test_emit_dedup_priority_stale(self):
  m=SpeechManager(dry_run=True);self.assertTrue(m.submit("EMIT",e(),0));self.assertFalse(m.submit("EMIT",e(),0));self.assertTrue(m.submit("EMIT",e("POSSIBLE_APPROACH",HIGH),0));self.assertTrue(m.process(1));self.assertFalse(m.submit("SUPPRESS",e(),1));self.assertTrue(m.submit("EMIT",e("CROSSING",HIGH,"CROSSING LEFT→RIGHT"),2));self.assertFalse(m.process(8))
 def test_failure(self):
  m=SpeechManager(type("P",(),{"say":lambda s,t:(_ for _ in ()).throw(RuntimeError())})());m.submit("EMIT",e(),0);self.assertFalse(m.process(1));self.assertEqual(m.stats["failures"],1)
