#!/usr/bin/env python3
"""Phase 7 isolated local vehicle-awareness prototype; never a crossing decision."""
from __future__ import annotations
import argparse
from dataclasses import dataclass, field
from pathlib import Path
import statistics, sys, time
from typing import Iterable, Sequence
from prototype_camera_motion import CAMERA_UNCERTAIN, MotionSmoother, SparseCameraMotionEstimator, corrected_motion
from prototype_motion_tracking import LightweightTracker, forward_distance
from prototype_vision import DEFAULT_LABELS, DEFAULT_MODEL, LiteRTDetector, NewestFrameGate, SystemMonitor, percentile

VEHICLES=frozenset({"car","truck","bus","motorcycle","bicycle"}); HIGH,MEDIUM,LOW="HIGH","MEDIUM","LOW"
LEVELS=(LOW,MEDIUM,HIGH)

@dataclass(frozen=True)
class VehicleObservation:
    track: object; level:str; score:int; motion:str; transition:str; reasons:tuple[str,...]; event:str; raw_dx:float|None; camera_dx:float|None; corrected_dx:float|None

def zone_transition(previous:str|None,current:str)->str:
    if previous in {"LEFT","RIGHT"} and current=="CENTER": return f"{previous}→CENTER"
    if previous=="CENTER" and current in {"LEFT","RIGHT"}: return f"CENTER→{current}"
    return current

class VehicleEngine:
    def __init__(self): self.previous={}; self.signatures={}; self.levels={}
    @staticmethod
    def key(track): return track.label,track.track_id
    def observe(self,tracks:Iterable[object],camera)->list[VehicleObservation]:
        observations=[]; active=set()
        for track in tracks:
            if track.label.lower() not in VEHICLES: continue
            key=self.key(track); active.add(key); history=list(track.history)
            previous=self.previous.get(key, history[-2].position if len(history)>=2 else None); transition=zone_transition(previous,track.position); self.previous[key]=track.position
            if not track.confirmed or not track.seen_this_update: continue
            raw=None if len(history)<2 else history[-1].center_x-history[-2].center_x
            motion=corrected_motion(track,camera); camera_dx=None if camera.state==CAMERA_UNCERTAIN else camera.dx; corrected=None if raw is None or camera_dx is None else raw-camera_dx
            score=2; reasons=["persistent track","vehicle class"]
            if track.position=="CENTER": score+=2; reasons.append("center")
            if motion.startswith("MOVING"):
                score+=2; reasons.append("corrected movement")
            elif motion=="UNCERTAIN": reasons.append("motion uncertain")
            if transition.endswith("→CENTER"): score+=3; reasons.append("entering center")
            if track.motion.startswith("CROSSING"):
                score+=2; reasons.append("crossing center")
            wanted=HIGH if score>=7 else MEDIUM if score>=4 else LOW; prior=self.levels.get(key,LOW); level=wanted if LEVELS.index(wanted)>=LEVELS.index(prior) else prior; self.levels[key]=level
            signature=(level,transition,motion); old=self.signatures.get(key); event="NEW" if old is None else "ONGOING" if old==signature else "CHANGED"; self.signatures[key]=signature
            observations.append(VehicleObservation(track,level,score,motion,transition,tuple(reasons),event,raw,camera_dx,corrected))
        # Keep short-lived state for a tracker-reconnected occlusion; track IDs are
        # monotonically assigned, so an eventual new track will not reuse it.
        return sorted(observations,key=lambda item:(-LEVELS.index(item.level),-item.score,item.track.label,item.track.track_id))

def group_motion(observations:Iterable[VehicleObservation],camera)->str|None:
    if camera.state==CAMERA_UNCERTAIN:return None
    motions=[item.motion for item in observations if item.motion in {"MOVING LEFT","MOVING RIGHT"}]
    for direction in ("MOVING LEFT","MOVING RIGHT"):
        if len(motions)>=2 and motions.count(direction)>=2 and motions.count(direction)>len(motions)-motions.count(direction): return f"multiple vehicles {direction.lower().replace('moving ','moving ')}"
    return None

def format_top(observations): return [f"{item.track.display_id} | {item.level} | {item.transition} | {item.motion.lower()}" for item in observations[:3]]

@dataclass
class Stats:
    started:float=field(default_factory=time.monotonic); vehicle:list[float]=field(default_factory=list); inference:list[float]=field(default_factory=list); motion:list[float]=field(default_factory=list); tracking:list[float]=field(default_factory=list); cpu:list[float]=field(default_factory=list); rss:list[float]=field(default_factory=list); temp:list[float]=field(default_factory=list); fresh:list[float]=field(default_factory=list)
    def summary(self,elapsed):
        def v(x,s=""):return "n/a" if x is None else f"{x:.1f}{s}"
        return "\n".join(("\n=== SENSE PHASE 7 BENCHMARK ===",f"Vehicle awareness avg/p50/p95: {v(statistics.fmean(self.vehicle) if self.vehicle else None,' ms')}/{v(percentile(self.vehicle,50) if len(self.vehicle)>=5 else None,' ms')}/{v(percentile(self.vehicle,95) if len(self.vehicle)>=5 else None,' ms')}",f"Detector FPS: {v(len(self.inference)/elapsed if elapsed else None)}",f"Inference avg: {v(statistics.fmean(self.inference) if self.inference else None,' ms')}",f"Camera motion avg: {v(statistics.fmean(self.motion) if self.motion else None,' ms')}",f"Tracking avg: {v(statistics.fmean(self.tracking) if self.tracking else None,' ms')}",f"CPU/RAM/temp/freshness: {v(statistics.fmean(self.cpu) if self.cpu else None,'%')}/{v(max(self.rss) if self.rss else None,' MiB')}/{v(max(self.temp) if self.temp else None,' C')}/{v(statistics.fmean(self.fresh) if self.fresh else None,' ms')}","================================"))

def parse_args(argv:Sequence[str]|None=None):
    p=argparse.ArgumentParser(description=__doc__); p.add_argument("--model",type=Path,default=DEFAULT_MODEL);p.add_argument("--labels",type=Path,default=DEFAULT_LABELS);p.add_argument("--confidence",type=float,default=.5);p.add_argument("--max-detection-fps",type=float,default=5.);p.add_argument("--benchmark-seconds",type=float,default=0.);p.add_argument("--threads",type=int,default=2);p.add_argument("--camera-width",type=int,default=640);p.add_argument("--camera-height",type=int,default=480);p.add_argument("--no-preview",action="store_true");p.add_argument("--debug-vehicles",action="store_true");a=p.parse_args(argv)
    if not 0<=a.confidence<=1 or a.max_detection_fps<0 or a.benchmark_seconds<0 or a.threads<1:p.error("invalid vehicle option")
    return a

def console(observations,camera):
    moving=sum(item.motion.startswith("MOVING") for item in observations); base=f"VEHICLES: {len(observations)} tracked | {moving} moving"
    if camera.state==CAMERA_UNCERTAIN:return base+" | MOTION: UNCERTAIN — camera-motion estimate unreliable"
    return base+" | TOP: "+("; ".join(format_top(observations)) or "none")

def draw(frame,observations,camera,cv2):
    h,w=frame.shape[:2];cv2.rectangle(frame,(w//3,0),(2*w//3,h),(80,180,80),1)
    for item in observations[:3]:
        l,t,r,b=item.track.box;color={HIGH:(0,80,255),MEDIUM:(0,220,220),LOW:(90,190,90)}[item.level];cv2.rectangle(frame,(l,t),(r,b),color,2);cv2.putText(frame,f"{item.track.display_id} {item.level} {item.motion}",(l,max(18,t-7)),cv2.FONT_HERSHEY_SIMPLEX,.42,color,2)
        if item.motion.startswith("MOVING"): cv2.arrowedLine(frame,((l+r)//2,(t+b)//2),((l+r)//2+(30 if item.motion.endswith('RIGHT') else -30),(t+b)//2),color,2)
    cv2.putText(frame,f"CAMERA: {camera.state}",(7,20),cv2.FONT_HERSHEY_SIMPLEX,.5,(255,255,255),1);return frame

def main(argv:Sequence[str]|None=None)->int:
    a=parse_args(argv)
    if not a.model.is_file() or not a.labels.is_file():print("MISSING MODEL ASSET: run python3 download_prototype_model.py",file=sys.stderr);return 2
    try:
        from hardware.camera import Camera,CameraError,CameraReader
        from hardware.ultrasonic import UltrasonicError,UltrasonicMonitor,UltrasonicSensor
        detector=LiteRTDetector(a.model,a.labels,a.threads)
    except Exception as exc:print(f"PHASE 7 STARTUP FAILED: {type(exc).__name__}: {exc}",file=sys.stderr);return 2
    sensor=ultrasonic=camera=reader=None;stats=Stats();error=None
    try:
        try:sensor=UltrasonicSensor().open();ultrasonic=UltrasonicMonitor(sensor);ultrasonic.start()
        except UltrasonicError as exc:error=str(exc);print(f"ULTRASONIC UNAVAILABLE: {exc}",flush=True)
        tracker=LightweightTracker();engine=VehicleEngine();smoother=MotionSmoother();gate=NewestFrameGate();monitor=SystemMonitor();last=next_log=0.;print("SENSE PHASE 7 VEHICLE PROTOTYPE | Gemini/cloud/audio/navigation: DISABLED",flush=True)
        camera=Camera(resolution=(a.camera_width,a.camera_height),pixel_format="RGB888").open();reader=CameraReader(camera);reader.start();estimator=SparseCameraMotionEstimator(detector.cv2,detector.np)
        while True:
            now=time.monotonic()
            if a.benchmark_seconds and now-stats.started>=a.benchmark_seconds:break
            if a.max_detection_fps and now-last<1/a.max_detection_fps:time.sleep(.002);continue
            frame,age=reader.latest()
            if frame is None or not gate.accept(id(frame)):time.sleep(.002);continue
            last=time.monotonic();detections,infer=detector.detect(frame,a.confidence);started=time.perf_counter();motion=smoother.update(estimator.update(frame,detections)[0]);motion_ms=(time.perf_counter()-started)*1000;started=time.perf_counter();tracker.update(detections,time.monotonic());tracking_ms=(time.perf_counter()-started)*1000;started=time.perf_counter();items=engine.observe(tracker.tracks_for_display(),motion);vehicle_ms=(time.perf_counter()-started)*1000
            sample=monitor.sample();stats.inference.append(infer);stats.motion.append(motion_ms);stats.tracking.append(tracking_ms);stats.vehicle.append(vehicle_ms);stats.fresh.append((age+time.monotonic()-last)*1000)
            for key,target in (("cpu",stats.cpu),("rss",stats.rss),("temp",stats.temp)):
                if sample.get(key) is not None:target.append(float(sample[key]))
            if now>=next_log:
                print(console(items,motion),flush=True);group=group_motion(items,motion)
                if group:print("GROUP: "+group,flush=True)
                if a.debug_vehicles:
                    for item in items:print(f"DEBUG {item.track.display_id}: raw_dx={item.raw_dx} camera_dx={item.camera_dx} corrected_dx={item.corrected_dx} motion={item.motion} age={time.monotonic()-item.track.created_at:.1f}s zone={item.track.position} transition={item.transition} score={item.score} reasons={item.reasons} event={item.event}",flush=True)
                next_log=now+1
            if not a.no_preview:
                detector.cv2.imshow("Sense Phase 7 vehicle prototype",draw(frame.copy(),items,motion,detector.cv2));
                if detector.cv2.waitKey(1)&0xFF in (ord('q'),27):break
    except KeyboardInterrupt:pass
    except CameraError as exc:print(f"CAMERA FAILED: {exc}",file=sys.stderr);return 3
    except Exception as exc:print(f"PHASE 7 FAILED: {type(exc).__name__}: {exc}",file=sys.stderr);return 3
    finally:
        if reader:reader.stop()
        if camera:camera.close()
        if ultrasonic:ultrasonic.stop()
        if sensor:sensor.close()
        if not a.no_preview:detector.cv2.destroyAllWindows()
        print(stats.summary(time.monotonic()-stats.started),flush=True)
    return 0
if __name__=="__main__":raise SystemExit(main())
