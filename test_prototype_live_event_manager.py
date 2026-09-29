import inspect,time,unittest
from unittest import mock
from prototype_event_manager import Event,EventManager


class FakeSafety:
 def __init__(self):self.stopped=False
 def stop(self):self.stopped=True


class FakeMonitor:
 def __init__(self,distance=90.0):self.distance=distance;self.count=0
 def snapshot(self):
  self.count+=1
  return {"distance_cm":self.distance,"healthy":True,"out_of_range":False,"reading_count":self.count}


class FakeBeeper:
 def __init__(self):self.intervals=[];self.tones=[]
 def set_interval(self,value):self.intervals.append(value)
 def play_once(self,tone):self.tones.append(tone)


class FakeResource:
 def __init__(self):self.stopped=False;self.closed=False
 def stop(self):self.stopped=True
 def close(self):self.closed=True


class T(unittest.TestCase):
 def test_single_pipeline_delegate(self):
  import prototype_live_event_manager as m
  source=inspect.getsource(m)
  self.assertIn("prototype_person_awareness",source);self.assertIn("--event-manager",source)
  self.assertNotIn("CameraReader(",source);self.assertNotIn("LiteRTDetector(",source)
 def test_live_escalation(self):
  manager=EventManager();ahead=Event("person_awareness","PERSON_AHEAD","PERSON",1,0,"CENTER","PERSON AHEAD","MEDIUM");approach=Event("person_awareness","POSSIBLE_APPROACH","PERSON",1,1,"CENTER","POSSIBLE APPROACH","HIGH");self.assertEqual(manager.process([ahead],0)[0][0],"EMIT");self.assertEqual(manager.process([approach],1)[0][0],"EMIT")
 def test_ultrasonic_starts_by_default_and_stops_cleanly(self):
  import prototype_live_event_manager as m
  safety=FakeSafety()
  with mock.patch.object(m,"start_ultrasonic",return_value=safety) as start, mock.patch.object(m,"person_awareness_main",return_value=0) as live:
   self.assertEqual(m.main(["--speech"]),0)
  start.assert_called_once_with();live.assert_called_once_with(["--event-manager","--speech"]);self.assertTrue(safety.stopped)
 def test_no_ultrasonic_disables_it(self):
  import prototype_live_event_manager as m
  with mock.patch.object(m,"start_ultrasonic") as start, mock.patch.object(m,"person_awareness_main",return_value=0) as live:
   self.assertEqual(m.main(["--speech","--no-ultrasonic"]),0)
  start.assert_not_called();live.assert_called_once_with(["--event-manager","--speech"])
 def test_warning_runs_without_ssd_or_camera_detection(self):
  import prototype_live_event_manager as m
  monitor=FakeMonitor(90.0);beeper=FakeBeeper();worker=m.UltrasonicSafety(monitor,beeper)
  worker.start()
  try:
   deadline=time.monotonic()+0.5
   while not beeper.intervals and time.monotonic()<deadline:time.sleep(.01)
  finally:worker.stop()
  self.assertTrue(beeper.intervals);self.assertIsNotNone(beeper.intervals[-1])
 def test_ultrasonic_failure_keeps_live_pipeline_running(self):
  import prototype_live_event_manager as m
  with mock.patch.object(m.UltrasonicSensor,"open",side_effect=RuntimeError("GPIO busy")), mock.patch.object(m,"person_awareness_main",return_value=0) as live:
   self.assertEqual(m.main([]),0)
  live.assert_called_once()
 def test_clean_shutdown_stops_every_ultrasonic_resource(self):
  import prototype_live_event_manager as m
  sensor=FakeResource();monitor=FakeResource();player=FakeResource();beeper=FakeResource();safety=FakeResource()
  m.UltrasonicResources(sensor,monitor,player,beeper,safety).stop()
  self.assertTrue(safety.stopped);self.assertTrue(beeper.stopped);self.assertTrue(monitor.stopped)
  self.assertTrue(player.closed);self.assertTrue(sensor.closed)
