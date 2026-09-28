#!/usr/bin/env python3
"""Phase 7.5 local person-awareness logic; observations only."""
from __future__ import annotations
import argparse, statistics, sys, time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence
from prototype_camera_motion import CAMERA_UNCERTAIN, MotionSmoother, SparseCameraMotionEstimator, corrected_motion
from prototype_motion_tracking import LightweightTracker
from prototype_vision import DEFAULT_LABELS, DEFAULT_MODEL, LiteRTDetector, NewestFrameGate

HIGH,MEDIUM,LOW="HIGH","MEDIUM","LOW"
NO_APPROACH,POSSIBLE_APPROACH,APPROACH_UNCERTAIN="NO APPROACH EVIDENCE","POSSIBLE APPROACH","APPROACH UNCERTAIN"
MIN_APPROACH_SAMPLES=4; MIN_SCALE_GROWTH=.18; AHEAD_MIN_AREA=.045; AHEAD_MIN_WIDTH=.16
MAX_CENTER_SPAN_FOR_APPROACH=.22; STRONG_LATERAL_SPAN=.45; EDGE_MARGIN_FRACTION=.08
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
def approach_evidence(hist,motion,camera,frame_width):
 if camera.state==CAMERA_UNCERTAIN:return APPROACH_UNCERTAIN,("suppressed: camera motion uncertain",)
 if len(hist)<MIN_APPROACH_SAMPLES:return NO_APPROACH,("insufficient scale history",)
 areas=[x.area for x in hist];growth=(statistics.median(areas[-2:])-statistics.median(areas[:2]))/max(1,statistics.median(areas[:2]))
 centers=[x.center_x/frame_width for x in hist];span=max(centers)-min(centers);zones=[x.position for x in hist];cross=crossing(hist)
 edge_entry=min(centers[:2])<=EDGE_MARGIN_FRACTION or max(centers[:2])>=1-EDGE_MARGIN_FRACTION
 if growth<MIN_SCALE_GROWTH:return NO_APPROACH,("no sustained scale growth",)
 if cross or (motion.startswith("MOVING") and span>=STRONG_LATERAL_SPAN):return NO_APPROACH,("scale growth","suppressed: strong lateral traversal")
 if edge_entry and span>MAX_CENTER_SPAN_FOR_APPROACH:return NO_APPROACH,("scale growth","suppressed: edge entry")
 if span<=MAX_CENTER_SPAN_FOR_APPROACH and zones[-1]=="CENTER":return POSSIBLE_APPROACH,("sustained scale growth","center stable","persistent center")
 return NO_APPROACH,("scale growth","insufficient center stability")
class PersonEngine:
 def __init__(self):self.previous={};self.events={}
 def observe(self,tracks:Iterable[object],camera,frame=(640,480)):
  out=[]
  for t in tracks:
   if t.label!="person" or not t.confirmed or not t.seen_this_update:continue
   key=(t.label,t.track_id);hist=list(t.history);prev=self.previous.get(key,hist[-2].position if len(hist)>1 else None);tr=transition(prev,t.position);self.previous[key]=t.position;cross=crossing(hist);motion=corrected_motion(t,camera)
   approach,approach_reasons=approach_evidence(hist,motion,camera,frame[0])
   l,top,r,b=t.box;w,h=frame;ahead=t.position=="CENTER" and (r-l)/w>=AHEAD_MIN_WIDTH and (r-l)*(b-top)/(w*h)>=AHEAD_MIN_AREA;forward=ahead and approach==POSSIBLE_APPROACH
   score=1;reasons=["persistent track"]
   if t.position=="CENTER":score+=2;reasons+= ["center"]
   if motion.startswith("MOVING"):score+=2;reasons += ["corrected movement"]
   elif motion=="UNCERTAIN":reasons += ["motion uncertain"]
   if tr.startswith("ENTERING"):score+=3;reasons += ["entering center"]
   if cross:score+=2;reasons += ["crossing center"]
   if approach==POSSIBLE_APPROACH:score+=3;reasons += ["possible approach"]
   reasons += list(approach_reasons)
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
 p=argparse.ArgumentParser();p.add_argument("--model",type=Path,default=DEFAULT_MODEL);p.add_argument("--labels",type=Path,default=DEFAULT_LABELS);p.add_argument("--confidence",type=float,default=.5);p.add_argument("--max-detection-fps",type=float,default=5);p.add_argument("--threads",type=int,default=2);p.add_argument("--camera-width",type=int,default=640);p.add_argument("--camera-height",type=int,default=480);p.add_argument("--benchmark-seconds",type=float,default=0);p.add_argument("--no-preview",action="store_true");p.add_argument("--debug-people",action="store_true");p.add_argument("--event-manager",action="store_true");p.add_argument("--debug-events",action="store_true");p.add_argument("--speech",action="store_true");p.add_argument("--speech-dry-run",action="store_true");p.add_argument("--debug-speech",action="store_true");a=p.parse_args(argv)
 if a.benchmark_seconds<0:p.error("invalid benchmark duration")
 return a
def main(argv=None):
 a=parse_args(argv)
 if not a.model.is_file() or not a.labels.is_file():print("MISSING MODEL ASSET: run python3 download_prototype_model.py",file=sys.stderr);return 2
 try:
  from hardware.camera import Camera,CameraError,CameraReader
  detector=LiteRTDetector(a.model,a.labels,a.threads)
 except Exception as e:print(f"PHASE 7.5 STARTUP FAILED: {type(e).__name__}: {e}",file=sys.stderr);return 2
 camera=reader=None;controller=None;executor=None;future=None;started=time.monotonic();last=next_log=0.;engine=PersonEngine();tracker=LightweightTracker();smoother=MotionSmoother();gate=NewestFrameGate();manager=None
 preview_frames=ai_frames=0;inference_ms=[];motion_ms=[];tracking_ms=[];people_ms=[];event_ms=[];latest_items=[];latest_motion=None
 speech=None
 if a.event_manager or a.speech or a.speech_dry_run:
  from prototype_event_manager import EventManager
  manager=EventManager()
  if a.speech or a.speech_dry_run:
   from prototype_speech_manager import SpeechManager
   player=None
   if a.speech and not a.speech_dry_run:
    try:
     import config
     from hardware.audio import GeminiSpeechPlayer,SpeechController,SpeechPlayer
     from gemini_tts import GeminiTTSProvider
     fallback=SpeechPlayer().open(verify_playback=False)
     provider=GeminiTTSProvider(model=config.GEMINI_TTS_MODEL,voice=config.GEMINI_TTS_VOICE,style=config.GEMINI_TTS_STYLE,timeout_s=config.GEMINI_TTS_TIMEOUT_S,volume_boost=config.GEMINI_TTS_VOLUME_BOOST,peak_ceiling=config.GEMINI_TTS_PEAK_CEILING).open()
     controller=SpeechController(GeminiSpeechPlayer(provider,fallback));controller.start();player=controller
    except Exception as exc:print(f"SPEECH SETUP FAILED: {type(exc).__name__}: {exc}",flush=True)
   speech=SpeechManager(player,dry_run=a.speech_dry_run);speech.start()
 print("Sense Live Person/Event Prototype",flush=True)
 print(f"Speech: {'ENABLED (Despina)' if a.speech else 'DRY RUN' if a.speech_dry_run else 'DISABLED'}",flush=True)
 print("Gemini Vision: DISABLED\nNavigation: DISABLED",flush=True)
 try:
  camera=Camera(resolution=(a.camera_width,a.camera_height),pixel_format="RGB888").open();reader=CameraReader(camera);reader.start();estimator=SparseCameraMotionEstimator(detector.cv2,detector.np)
  # Exactly one worker owns SSD, motion, tracking, and events.  The main loop
  # only consumes CameraReader's latest-frame slot for responsive preview.
  executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix="sense-ai")
  def infer(inference_frame,event_time):
   began=time.monotonic();detections,_=detector.detect(inference_frame,a.confidence);inference_ms.append((time.monotonic()-began)*1000)
   began=time.monotonic();motion=smoother.update(estimator.update(inference_frame,detections)[0]);motion_ms.append((time.monotonic()-began)*1000)
   began=time.monotonic();tracker.update(detections,time.monotonic());tracking_ms.append((time.monotonic()-began)*1000)
   began=time.monotonic();items=engine.observe(tracker.tracks_for_display(),motion,(inference_frame.shape[1],inference_frame.shape[0]));people_ms.append((time.monotonic()-began)*1000)
   if manager is not None:
    from prototype_event_manager import person_events
    began=time.monotonic();event_now=time.monotonic()
    for action,event,reason in manager.process(person_events(items,event_now),event_now):
     if speech is not None and speech.submit(action,event,event_now) and a.debug_speech:print(f'SPEECH QUEUED: "{event.event_type}"',flush=True)
     if action=="EMIT" or a.debug_events:print(f"{action} [{event.priority}] {event.entity_type} #{event.entity_id} {event.event_type} {event.zone}: {reason}",flush=True)
    event_ms.append((time.monotonic()-began)*1000)
   return items,motion
  while True:
   now=time.monotonic();frame,_=reader.latest()
   if a.benchmark_seconds and now-started>=a.benchmark_seconds:break
   if not a.no_preview and frame is not None:
    preview_frames+=1
    display_frame=frame.copy()
    for item in latest_items:
     l,t,r,b=item.track.box;detector.cv2.rectangle(display_frame,(l,t),(r,b),(0,220,220),2);detector.cv2.putText(display_frame,f"{item.track.display_id} {item.level} {item.approach}",(l,max(18,t-6)),detector.cv2.FONT_HERSHEY_SIMPLEX,.42,(0,220,220),1)
    detector.cv2.imshow("Sense Live Person/Event Prototype",display_frame)
    if detector.cv2.waitKey(1)&0xFF in (ord('q'),27):break
   if future is not None and future.done():
    latest_items,latest_motion=future.result();future=None;ai_frames+=1
   if future is None and frame is not None and now-last>=1/a.max_detection_fps and gate.accept(id(frame)):
    last=now;future=executor.submit(infer,frame.copy(),now)
   if now>=next_log:
    print(console(latest_items,latest_motion) if latest_motion is not None else "PEOPLE: waiting for AI",flush=True)
    if a.debug_people:
     for item in latest_items:print(f"DEBUG {item.track.display_id}: box={item.track.box} zone={item.track.position} motion={item.motion} approach={item.approach} reasons={item.reasons} ahead={item.ahead} score={item.score} event={item.event}",flush=True)
    next_log=now+1
 except KeyboardInterrupt:pass
 except CameraError as e:print(f"CAMERA FAILED: {e}",file=sys.stderr);return 3
 finally:
  elapsed=max(.001,time.monotonic()-started)
  if a.benchmark_seconds:
   mean=lambda values:statistics.fmean(values) if values else 0.0
   capture=reader.snapshot().get("fps") if reader else None
   print(f"BENCHMARK capture FPS: {capture if capture is not None else 0.0:.1f} | preview FPS: {preview_frames/elapsed:.1f} | AI FPS: {ai_frames/elapsed:.1f} | inference: {mean(inference_ms):.1f}ms | motion: {mean(motion_ms):.1f}ms | tracking: {mean(tracking_ms):.1f}ms | people: {mean(people_ms):.1f}ms | events: {mean(event_ms):.1f}ms",flush=True)
   print(f"TRACKS created={tracker.created_count} expired={tracker.expired_count}",flush=True)
   if speech is not None:print(f"SPEECH {speech.stats}",flush=True)
  if executor:executor.shutdown(wait=True,cancel_futures=False)
  if speech:speech.stop()
  if controller:controller.stop()
  if reader:reader.stop()
  if camera:camera.close()
  if not a.no_preview:detector.cv2.destroyAllWindows()
 return 0
if __name__=="__main__":raise SystemExit(main())
