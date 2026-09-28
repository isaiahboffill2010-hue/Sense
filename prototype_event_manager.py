#!/usr/bin/env python3
"""Phase 8 isolated local event manager; no perception, speech, cloud, or navigation."""
from __future__ import annotations
import argparse,time
from dataclasses import dataclass,field
from typing import Mapping
LOW,MEDIUM,HIGH,CRITICAL="LOW","MEDIUM","HIGH","CRITICAL";RANK={LOW:1,MEDIUM:2,HIGH:3,CRITICAL:4}
COOLDOWNS={"PERSON_AHEAD":8.,"POSSIBLE_APPROACH":12.,"CROSSING":8.,"ULTRASONIC_CLOSE":3.,"WALKING_SPACE":20.};TTL={"PERSON_AHEAD":3.,"POSSIBLE_APPROACH":3.,"CROSSING":4.,"ULTRASONIC_CLOSE":2.,"WALKING_SPACE":5.};LOST_GRACE=1.;EVENT_BUDGET=2
@dataclass(frozen=True)
class Event:
 source:str;event_type:str;entity_type:str;entity_id:int|str|None;timestamp:float;zone:str|None;state:str;priority:str;reasons:tuple[str,...]=();reliability:str="SUPPORTED";metadata:Mapping[str,object]=field(default_factory=dict)
 @property
 def key(self):return self.source,self.event_type,self.entity_type,self.entity_id
 @property
 def payload(self):return {"event_type":self.event_type,"entity":self.entity_type,"entity_id":self.entity_id,"zone":self.zone,"priority":self.priority,"reasons":self.reasons}
@dataclass
class Record: event:Event;last_emit:float;last_seen:float
class EventManager:
 def __init__(self,budget=EVENT_BUDGET):self.active={};self.budget=budget
 def process(self,incoming,now=None):
  now=time.monotonic() if now is None else now;actions=[];candidates=[]
  for event in incoming:
   if event.reliability in {"UNCERTAIN","UNKNOWN"}:actions.append(("SUPPRESS",event,"uncertain source"));continue
   record=self.active.get(event.key);cool=COOLDOWNS.get(event.event_type,8.)
   if record:
    record.last_seen=now
    if RANK[event.priority]>RANK[record.event.priority] or event.state!=record.event.state: candidates.append((event,"meaningful escalation/change"))
    elif now-record.last_emit<cool:actions.append(("SUPPRESS",event,"duplicate active event"))
    else:actions.append(("SUPPRESS",event,"cooldown elapsed but state unchanged"))
   else:candidates.append((event,"new event"))
  for key,record in list(self.active.items()):
   if now-record.last_seen>TTL.get(record.event.event_type,3.)+LOST_GRACE:self.active.pop(key);actions.append(("ENDED",record.event,"expired"))
  for event,reason in sorted(candidates,key=lambda x:(-RANK[x[0].priority],x[0].source,str(x[0].entity_id)))[:self.budget]:self.active[event.key]=Record(event,now,now);actions.append(("EMIT",event,reason))
  for event,reason in sorted(candidates,key=lambda x:(-RANK[x[0].priority],x[0].source,str(x[0].entity_id)))[self.budget:]:actions.append(("DROP",event,"event budget"))
  return actions
def person_events(items,now):
 out=[]
 for x in items:
  if x.approach=="POSSIBLE APPROACH":out.append(Event("person_awareness","POSSIBLE_APPROACH","PERSON",x.track.track_id,now,x.track.position,x.approach,HIGH,x.reasons))
  elif x.crossing:out.append(Event("person_awareness","CROSSING","PERSON",x.track.track_id,now,x.track.position,x.crossing,HIGH,x.reasons))
  elif x.ahead:out.append(Event("person_awareness","PERSON_AHEAD","PERSON",x.track.track_id,now,x.track.position,"PERSON AHEAD",MEDIUM,x.reasons))
 return out
def simulate(manager):
 sequence=[Event("person_awareness","PERSON_AHEAD","PERSON",1,0,"CENTER","PERSON AHEAD",MEDIUM),Event("person_awareness","PERSON_AHEAD","PERSON",1,1,"CENTER","PERSON AHEAD",MEDIUM),Event("person_awareness","POSSIBLE_APPROACH","PERSON",1,2,"CENTER","POSSIBLE APPROACH",HIGH),Event("person_awareness","CROSSING","PERSON",2,3,"CENTER","CROSSING LEFT→RIGHT",HIGH),Event("person_awareness","POSSIBLE_APPROACH","PERSON",1,4,"CENTER","POSSIBLE APPROACH",HIGH)]
 return [manager.process([event],event.timestamp) for event in sequence]
def parse_args(argv=None):
 p=argparse.ArgumentParser();p.add_argument("--simulate-events",action="store_true");p.add_argument("--debug-events",action="store_true");p.add_argument("--people",action="store_true");return p.parse_args(argv)
def main(argv=None):
 a=parse_args(argv);manager=EventManager()
 if a.simulate_events:
  for actions in simulate(manager):
   for action,event,reason in actions:print(f"{action} [{event.priority}] {event.source} {event.entity_type} #{event.entity_id} {event.event_type}: {reason}")
  return 0
 print("Use --simulate-events for deterministic Event Manager validation. Live --people adapter is intentionally separate from production.");return 0
if __name__=="__main__":raise SystemExit(main())
