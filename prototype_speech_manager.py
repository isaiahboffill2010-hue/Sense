"""Phase 9 bounded, event-driven prototype speech policy."""
from dataclasses import dataclass
import time
@dataclass(frozen=True)
class SpeechRequest: text:str;priority:str;key:tuple;created_at:float;expires_at:float
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
 def __init__(self,player=None,dry_run=False):self.player=player;self.dry_run=dry_run;self.pending=None;self.seen=set();self.stats={"submitted":0,"spoken":0,"dedup":0,"stale":0,"failures":0}
 def submit(self,action,event,now=None):
  now=time.monotonic() if now is None else now
  if action!="EMIT" or event.priority=="LOW":return False
  text=phrase_for(event)
  if not text:return False
  key=(event.key,text)
  if key in self.seen:self.stats["dedup"]+=1;return False
  request=SpeechRequest(text,event.priority,key,now,now+5);self.seen.add(key);self.stats["submitted"]+=1
  if self.pending is None or {"MEDIUM":2,"HIGH":3,"CRITICAL":4}.get(request.priority,1)>={"MEDIUM":2,"HIGH":3,"CRITICAL":4}.get(self.pending.priority,1):self.pending=request
  return True
 def process(self,now=None):
  now=time.monotonic() if now is None else now;request,self.pending=self.pending,None
  if request is None:return False
  if now>request.expires_at:self.stats["stale"]+=1;return False
  try:
   if self.dry_run:print(f'SPEECH: "{request.text}"',flush=True)
   else:self.player.say(request.text)
   self.stats["spoken"]+=1;return True
  except Exception:self.stats["failures"]+=1;return False
