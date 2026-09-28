"""Bounded, event-driven prototype speech policy.

Only one request may wait behind audio.  The audio provider owns active speech,
so camera, detector, and event processing never wait on Gemini or playback.
"""
from dataclasses import dataclass
import threading
import time

PRIORITY = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
DEDUP_WINDOW_S = 1.0
TTL_S = {"MEDIUM": 4.0, "HIGH": 3.0, "CRITICAL": 3.0}

@dataclass(frozen=True)
class SpeechRequest:
 text:str;priority:str;key:tuple;created_at:float;expires_at:float

def phrase_for(event):
 if event.event_type=="PERSON_AHEAD":return "Person ahead."
 if event.event_type=="POSSIBLE_APPROACH":return "Someone appears to be approaching."
 if event.event_type=="CROSSING":
  state=event.state.replace("→","_").upper()
  if "LEFT" in state and "RIGHT" in state:return "Person crossing left to right."
  if "RIGHT" in state and "LEFT" in state:return "Person crossing right to left."
  return "Person crossing ahead."
 return None

class SpeechManager:
 """Condition-driven, one-pending-request speech dispatcher."""
 def __init__(self,player=None,dry_run=False):
  self.player=player;self.dry_run=dry_run;self.pending=None;self._seen={};self._condition=threading.Condition();self._running=False;self._worker=None
  self.stats={"submitted":0,"spoken":0,"dedup":0,"stale":0,"failures":0,"superseded":0,"dispatch_ms":[]}
 def start(self):
  with self._condition:
   if self._running:return
   self._running=True;self._worker=threading.Thread(target=self._run,name="sense-speech-dispatch",daemon=True);self._worker.start()
 def stop(self):
  with self._condition:self._running=False;self._condition.notify_all()
  if self._worker:self._worker.join(timeout=1.0)
 def submit(self,action,event,now=None):
  now=time.monotonic() if now is None else now
  if action!="EMIT" or event.priority=="LOW":return False
  text=phrase_for(event)
  if not text:return False
  key=(event.key,text)
  with self._condition:
   self._seen={old_key:timestamp for old_key,timestamp in self._seen.items() if now-timestamp<=DEDUP_WINDOW_S}
   if key in self._seen:self.stats["dedup"]+=1;return False
   self._seen[key]=now;request=SpeechRequest(text,event.priority,key,now,now+TTL_S.get(event.priority,4.0));self.stats["submitted"]+=1
   if self.pending is not None and PRIORITY.get(request.priority,1)>=PRIORITY.get(self.pending.priority,1):self.stats["superseded"]+=1;self.pending=request
   elif self.pending is None:self.pending=request
   else:self.stats["superseded"]+=1;return False
   self._condition.notify();return True
 def _dispatch(self,request,now):
  if now>request.expires_at:self.stats["stale"]+=1;return False
  self.stats["dispatch_ms"].append((now-request.created_at)*1000.0)
  try:
   if self.dry_run:print(f'SPEECH: "{request.text}"',flush=True)
   elif self.player is None:raise RuntimeError("no speech player configured")
   else:self.player.say(request.text)
   self.stats["spoken"]+=1;return True
  except Exception:self.stats["failures"]+=1;return False
 def _run(self):
  while True:
   with self._condition:
    self._condition.wait_for(lambda:not self._running or self.pending is not None)
    if not self._running:return
    request,self.pending=self.pending,None
   self._dispatch(request,time.monotonic())
 def process(self,now=None):
  """Synchronous test helper; live code uses the condition worker."""
  with self._condition:request,self.pending=self.pending,None
  return False if request is None else self._dispatch(request,time.monotonic() if now is None else now)
