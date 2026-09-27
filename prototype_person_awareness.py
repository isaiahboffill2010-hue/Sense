#!/usr/bin/env python3
"""Phase 7.5 local person-awareness logic; observations only."""
from __future__ import annotations
import argparse, statistics, sys, time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence
from prototype_camera_motion import CAMERA_UNCERTAIN, MotionSmoother, SparseCameraMotionEstimator, corrected_motion
from prototype_motion_tracking import LightweightTracker
from prototype_vision import DEFAULT_LABELS, DEFAULT_MODEL, LiteRTDetector, NewestFrameGate

HIGH,MEDIUM,LOW="HIGH","MEDIUM","LOW"
NO_APPROACH,POSSIBLE_APPROACH,APPROACH_UNCERTAIN="NO APPROACH EVIDENCE","POSSIBLE APPROACH","APPROACH UNCERTAIN"
MIN_APPROACH_SAMPLES=4; MIN_SCALE_GROWTH=.18; AHEAD_MIN_AREA=.045; AHEAD_MIN_WIDTH=.16
@dataclass(frozen=True)
class PersonObservation:
 track:object;level:str;score:int;motion:str;transition:str;crossing:str|None;approach:str;ahead:bool;forward_attention:bool;reasons:tuple[str,...];event:str
def transition(previous,current):
 if previous in {"LEFT","RIGHT"} and current=="CENTER":return f"ENTERING CENTER FROM {previous}"
 if previous=="CENTER" and current in {"LEFT","RIGHT"}:return f"LEAVING CENTER TO {current}"
 return current
def crossing(hist):
 z=[x.position for x in hist]
 if len(z)>=3 and z[-3:]==["LEFT","CENTER","RIGHT"]:return "CROSSING LEFT→RIGHT"
 if len(z)>=3 and z[-3:]==["RIGHT","CENTER","LEFT"]:return "CROSSING RIGHT→LEFT"
class PersonEngine:
 def __init__(self):self.previous={};self.events={}
 def observe(self,tracks:Iterable[object],camera,frame=(640,480)):
  out=[]
  for t in tracks:
   if t.label!="person" or not t.confirmed or not t.seen_this_update:continue
   key=(t.label,t.track_id);hist=list(t.history);prev=self.previous.get(key,hist[-2].position if len(hist)>1 else None);tr=transition(prev,t.position);self.previous[key]=t.position;cross=crossing(hist);motion=corrected_motion(t,camera)
   approach=APPROACH_UNCERTAIN if camera.state==CAMERA_UNCERTAIN else NO_APPROACH
   if camera.state!=CAMERA_UNCERTAIN and len(hist)>=MIN_APPROACH_SAMPLES:
    a=[x.area for x in hist];g=(statistics.median(a[-2:])-statistics.median(a[:2]))/max(1,statistics.median(a[:2]))
    if g>=MIN_SCALE_GROWTH:approach=POSSIBLE_APPROACH
   l,top,r,b=t.box;w,h=frame;ahead=t.position=="CENTER" and (r-l)/w>=AHEAD_MIN_WIDTH and (r-l)*(b-top)/(w*h)>=AHEAD_MIN_AREA;forward=ahead and approach==POSSIBLE_APPROACH
   score=1;reasons=["persistent track"]
   if t.position=="CENTER":score+=2;reasons+= ["center"]
   if motion.startswith("MOVING"):score+=2;reasons += ["corrected movement"]
   elif motion=="UNCERTAIN":reasons += ["motion uncertain"]
   if tr.startswith("ENTERING"):score+=3;reasons += ["entering center"]
   if cross:score+=2;reasons += ["crossing center"]
   if approach==POSSIBLE_APPROACH:score+=3;reasons += ["possible approach"]
   if ahead:score+=2;reasons += ["person ahead"]
   if forward:score+=2;reasons += ["forward person attention"]
   level=HIGH if score>=8 else MEDIUM if score>=4 else LOW;sig=(level,tr,cross,approach,ahead);old=self.events.get(key);event="NEW" if old is None else "ONGOING" if old==sig else "CHANGED";self.events[key]=sig
   out.append(PersonObservation(t,level,score,motion,tr,cross,approach,ahead,forward,tuple(reasons),event))
  return sorted(out,key=lambda x:(-{LOW:0,MEDIUM:1,HIGH:2}[x.level],-x.score,x.track.track_id))
def group_motion(items,camera):
 if camera.state==CAMERA_UNCERTAIN:return None
 m=[x.motion for x in items if x.motion in {"MOVING LEFT","MOVING RIGHT"}]
 for d in ("MOVING LEFT","MOVING RIGHT"):
  if m.count(d)>=2 and m.count(d)>len(m)-m.count(d):return "multiple people "+d.lower()
def format_top(items):return [f"{x.track.display_id} | {x.level} | {x.transition} | {x.approach}" for x in items[:3]]
def console(items,camera):return f"PEOPLE: {len(items)} tracked | TOP: "+("; ".join(format_top(items)) if items else "none")+(" | MOTION: UNCERTAIN" if camera.state==CAMERA_UNCERTAIN else "")
def parse_args(argv:Sequence[str]|None=None):
 p=argparse.ArgumentParser();p.add_argument("--model",type=Path,default=DEFAULT_MODEL);p.add_argument("--labels",type=Path,default=DEFAULT_LABELS);p.add_argument("--confidence",type=float,default=.5);p.add_argument("--max-detection-fps",type=float,default=5);p.add_argument("--threads",type=int,default=2);p.add_argument("--camera-width",type=int,default=640);p.add_argument("--camera-height",type=int,default=480);p.add_argument("--benchmark-seconds",type=float,default=0);p.add_argument("--no-preview",action="store_true");p.add_argument("--debug-people",action="store_true");a=p.parse_args(argv)
 if a.benchmark_seconds<0:p.error("invalid benchmark duration")
 return a
def main(argv=None):
 a=parse_args(argv)
 if not a.model.is_file() or not a.labels.is_file():print("MISSING MODEL ASSET: run python3 download_prototype_model.py",file=sys.stderr);return 2
 try:
  from hardware.camera import Camera,CameraError,CameraReader
  detector=LiteRTDetector(a.model,a.labels,a.threads)
 except Exception as e:print(f"PHASE 7.5 STARTUP FAILED: {type(e).__name__}: {e}",file=sys.stderr);return 2
 camera=reader=None;started=time.monotonic();last=next_log=0.;engine=PersonEngine();tracker=LightweightTracker();smoother=MotionSmoother();gate=NewestFrameGate()
 print("SENSE PHASE 7.5 PERSON PROTOTYPE | Gemini/cloud/audio/navigation: DISABLED",flush=True)
 try:
  camera=Camera(resolution=(a.camera_width,a.camera_height),pixel_format="RGB888").open();reader=CameraReader(camera);reader.start();estimator=SparseCameraMotionEstimator(detector.cv2,detector.np)
  while True:
   now=time.monotonic()
   if a.benchmark_seconds and now-started>=a.benchmark_seconds:break
   if now-last<1/a.max_detection_fps:time.sleep(.002);continue
   frame,_=reader.latest()
   if frame is None or not gate.accept(id(frame)):time.sleep(.002);continue
   last=now;detections,_=detector.detect(frame,a.confidence);motion=smoother.update(estimator.update(frame,detections)[0]);tracker.update(detections,time.monotonic());items=engine.observe(tracker.tracks_for_display(),motion,(frame.shape[1],frame.shape[0]))
   if now>=next_log:
    print(console(items,motion),flush=True)
    if a.debug_people:
     for item in items:print(f"DEBUG {item.track.display_id}: box={item.track.box} zone={item.track.position} motion={item.motion} approach={item.approach} ahead={item.ahead} score={item.score} reasons={item.reasons} event={item.event}",flush=True)
    next_log=now+1
   if not a.no_preview:
    for item in items:
     l,t,r,b=item.track.box;detector.cv2.rectangle(frame,(l,t),(r,b),(0,220,220),2);detector.cv2.putText(frame,f"{item.track.display_id} {item.level} {item.approach}",(l,max(18,t-6)),detector.cv2.FONT_HERSHEY_SIMPLEX,.42,(0,220,220),1)
    detector.cv2.imshow("Sense Phase 7.5 person prototype",frame)
    if detector.cv2.waitKey(1)&0xFF in (ord('q'),27):break
 except KeyboardInterrupt:pass
 except CameraError as e:print(f"CAMERA FAILED: {e}",file=sys.stderr);return 3
 finally:
  if reader:reader.stop()
  if camera:camera.close()
  if not a.no_preview:detector.cv2.destroyAllWindows()
 return 0
if __name__=="__main__":raise SystemExit(main())
