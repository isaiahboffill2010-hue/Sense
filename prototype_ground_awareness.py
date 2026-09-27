#!/usr/bin/env python3
"""Phase 6 isolated, conservative monocular ground-evidence prototype."""
from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
import statistics
import sys
import time
from typing import Iterable, Sequence

from prototype_camera_motion import CAMERA_STABLE, CAMERA_UNCERTAIN, MotionSmoother, SparseCameraMotionEstimator
from prototype_motion_tracking import LightweightTracker, forward_distance
from prototype_vision import DEFAULT_LABELS, DEFAULT_MODEL, LiteRTDetector, NewestFrameGate, SystemMonitor, format_metric, percentile

SIDEWALK, ROAD, OTHER_GROUND, UNKNOWN = "SIDEWALK", "ROAD", "OTHER_GROUND", "UNKNOWN"
OPEN, OCCUPIED = "APPARENTLY OPEN", "OCCUPIED"
POSSIBLE, NOT_OBSERVED = "POSSIBLE", "NOT OBSERVED"


@dataclass(frozen=True)
class GroundEvidence:
    visible_fraction: float
    sidewalk_score: float
    road_score: float
    conflict: float
    boundary_strength: float


@dataclass(frozen=True)
class GroundResult:
    category: str
    spaces: tuple[str, str, str]
    transition: str
    evidence: GroundEvidence
    ground_mask: object | None = None
    boundary_y: int | None = None


def classify_ground(evidence: GroundEvidence) -> str:
    if evidence.visible_fraction < .35 or evidence.conflict > .30: return UNKNOWN
    strongest, other = max(evidence.sidewalk_score, evidence.road_score), min(evidence.sidewalk_score, evidence.road_score)
    if strongest >= .70 and strongest - other >= .15: return SIDEWALK if evidence.sidewalk_score > evidence.road_score else ROAD
    return OTHER_GROUND if evidence.visible_fraction >= .60 else UNKNOWN


def ground_analysis_reliable(camera_motion) -> bool:
    """Head movement/unknown flow makes monocular boundary evidence unreliable."""
    return camera_motion.state == CAMERA_STABLE


def spaces_from_fractions(fractions: Iterable[float], tracks: Iterable[object], width: int, height: int) -> tuple[str, str, str]:
    output=[]
    for index, ground_fraction in enumerate(fractions):
        left, right = index * width // 3, (index + 1) * width // 3
        occupied=False
        for track in tracks:
            if not track.confirmed or not track.seen_this_update: continue
            box_left, _, box_right, box_bottom=track.box
            overlap=max(0, min(right,box_right)-max(left,box_left))
            if overlap and box_bottom >= height * .55: occupied=True; break
        output.append(OCCUPIED if occupied else OPEN if ground_fraction >= .55 else UNKNOWN)
    return tuple(output)


def occupancy_for_spaces(mask, tracks: Iterable[object], np) -> tuple[str, str, str]:
    height, width = mask.shape[:2]; fractions=[]
    for index in range(3):
        left, right = index * width // 3, (index + 1) * width // 3
        fractions.append(float(np.mean(mask[height//2:, left:right] > 0)))
    return spaces_from_fractions(fractions, tracks, width, height)


class TransitionHistory:
    """Promotes an edge only after two supporting analyses in the last three."""
    def __init__(self): self.values=deque(maxlen=3)
    def update(self, candidate: bool, reliable: bool) -> str:
        if not reliable: self.values.clear(); return UNKNOWN
        self.values.append(candidate)
        if len(self.values) < 2: return UNKNOWN
        return POSSIBLE if sum(self.values) >= 2 else NOT_OBSERVED


class GroundAnalyzer:
    """Classical CV evidence only: lower-frame colour consistency + edges, not a semantic model."""
    def __init__(self, cv2, np): self.cv2,self.np=cv2,np
    def analyze(self, frame, tracks, camera_motion, history: TransitionHistory) -> GroundResult:
        cv2,np=self.cv2,self.np; height,width=frame.shape[:2]; top=height//2; roi=frame[top:]
        small=cv2.resize(roi,(160,120),interpolation=cv2.INTER_AREA); hsv=cv2.cvtColor(small,cv2.COLOR_BGR2HSV); gray=cv2.cvtColor(small,cv2.COLOR_BGR2GRAY)
        saturation=hsv[:,:,1]; value=hsv[:,:,2]; edges=cv2.Canny(gray,50,120)
        # Ground evidence is intentionally weak: low/moderate saturation and no
        # extreme local texture. This may identify pavement-like regions, never safety.
        ground=((saturation < 105) & (value > 35) & (edges == 0)).astype(np.uint8)*255
        ground=cv2.morphologyEx(ground,cv2.MORPH_CLOSE,np.ones((5,5),np.uint8))
        visible=float(np.mean(ground > 0)); texture=float(np.mean(edges > 0)); mean_sat=float(np.mean(saturation))/255
        lines=cv2.HoughLinesP(edges,1,np.pi/180,threshold=28,minLineLength=45,maxLineGap=12)
        boundary_y=None; strength=0.
        if lines is not None:
            horizontal=[]
            for x1,y1,x2,y2 in lines.reshape(-1,4):
                if abs(y2-y1)<=5 and abs(x2-x1)>=55 and 25<=y1<=105: horizontal.append((abs(x2-x1),int((y1+y2)/2)))
            if horizontal:
                length,boundary_y=max(horizontal); strength=min(1.,length/160); boundary_y=top+round(boundary_y*roi.shape[0]/120)
        # Heuristic labels are deliberately strict and commonly return OTHER/UNKNOWN.
        road_score=max(0.,min(1., .55*visible + .25*(1-mean_sat) + .20*(1-texture)))
        sidewalk_score=max(0.,min(1., .45*visible + .35*texture + .20*strength))
        evidence=GroundEvidence(visible,sidewalk_score,road_score,abs(sidewalk_score-road_score)<.15,strength)
        reliable=ground_analysis_reliable(camera_motion)
        transition=history.update(strength >= .55 and visible >= .35,reliable)
        category=classify_ground(evidence) if reliable else UNKNOWN
        full_mask=np.zeros((height,width),dtype=np.uint8); full_mask[top:]=cv2.resize(ground,(width,height-top),interpolation=cv2.INTER_NEAREST)
        spaces=occupancy_for_spaces(full_mask,tracks,np) if reliable else (UNKNOWN,UNKNOWN,UNKNOWN)
        return GroundResult(category,spaces,transition,evidence,full_mask,boundary_y)


@dataclass
class GroundStats:
    started_at: float=field(default_factory=time.monotonic); ground:list[float]=field(default_factory=list); inference:list[float]=field(default_factory=list); cpu:list[float]=field(default_factory=list); rss:list[float]=field(default_factory=list); temp:list[float]=field(default_factory=list); freshness:list[float]=field(default_factory=list)
    def summary(self,elapsed):
        def val(value,s=""): return "n/a" if value is None else f"{value:.1f}{s}"
        return "\n".join(("\n=== SENSE PHASE 6 BENCHMARK ===",f"Ground analysis FPS: {val(len(self.ground)/elapsed if elapsed else None)}",f"Ground latency avg/p50/p95: {val(statistics.fmean(self.ground) if self.ground else None,' ms')}/{val(percentile(self.ground,50) if len(self.ground)>=5 else None,' ms')}/{val(percentile(self.ground,95) if len(self.ground)>=5 else None,' ms')}",f"Object detection FPS: {val(len(self.inference)/elapsed if elapsed else None)}",f"Average CPU: {val(statistics.fmean(self.cpu) if self.cpu else None,'%')}",f"Peak RAM: {val(max(self.rss) if self.rss else None,' MiB')}",f"Maximum CPU temperature: {val(max(self.temp) if self.temp else None,' C')}",f"Frame freshness avg: {val(statistics.fmean(self.freshness) if self.freshness else None,' ms')}","================================"))


def parse_args(argv:Sequence[str]|None=None):
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("--model",type=Path,default=DEFAULT_MODEL); parser.add_argument("--labels",type=Path,default=DEFAULT_LABELS); parser.add_argument("--confidence",type=float,default=.5); parser.add_argument("--max-detection-fps",type=float,default=5.); parser.add_argument("--ground-fps",type=float,default=1.); parser.add_argument("--benchmark-seconds",type=float,default=0.); parser.add_argument("--threads",type=int,default=2); parser.add_argument("--camera-width",type=int,default=640); parser.add_argument("--camera-height",type=int,default=480); parser.add_argument("--no-preview",action="store_true"); parser.add_argument("--debug-ground",action="store_true")
    args=parser.parse_args(argv)
    if not 0<=args.confidence<=1 or args.max_detection_fps<0 or args.ground_fps<=0 or args.benchmark_seconds<0 or args.threads<1: parser.error("invalid ground option")
    return args


def concise(result,camera): return f"GROUND: {result.category} | WALKING SPACE: LEFT {result.spaces[0]} | CENTER {result.spaces[1]} | RIGHT {result.spaces[2]} | GROUND TRANSITION: {result.transition} | CAMERA: {camera.state}"
def draw(frame,result,tracks,camera,cv2):
    h,w=frame.shape[:2]
    if result.ground_mask is not None:
        overlay=frame.copy(); overlay[result.ground_mask>0]=(70,170,70); cv2.addWeighted(overlay,.25,frame,.75,0,frame)
    for x in (w//3,2*w//3): cv2.line(frame,(x,h//2),(x,h),(255,255,0),1)
    if result.boundary_y: cv2.line(frame,(0,result.boundary_y),(w,result.boundary_y),(0,80,255),2)
    for track in tracks:
        l,t,r,b=track.box; cv2.rectangle(frame,(l,t),(r,b),(0,220,220),2)
    for i,line in enumerate((f"GROUND: {result.category}",f"SPACE L/C/R: {result.spaces[0]} / {result.spaces[1]} / {result.spaces[2]}",f"TRANSITION: {result.transition} | CAMERA: {camera.state}")):
        cv2.putText(frame,line,(7,20+i*20),cv2.FONT_HERSHEY_SIMPLEX,.45,(0,0,0),3); cv2.putText(frame,line,(7,20+i*20),cv2.FONT_HERSHEY_SIMPLEX,.45,(255,255,255),1)
    return frame


def main(argv:Sequence[str]|None=None)->int:
    args=parse_args(argv)
    if not args.model.is_file() or not args.labels.is_file(): print("MISSING MODEL ASSET: run python3 download_prototype_model.py",file=sys.stderr); return 2
    try:
        from hardware.camera import Camera,CameraError,CameraReader
        from hardware.ultrasonic import UltrasonicError,UltrasonicMonitor,UltrasonicSensor
        detector=LiteRTDetector(args.model,args.labels,args.threads)
    except Exception as exc: print(f"PHASE 6 STARTUP FAILED: {type(exc).__name__}: {exc}",file=sys.stderr); return 2
    sensor=ultrasonic=camera=reader=None; stats=GroundStats(); ultrasonic_error=None
    try:
        try: sensor=UltrasonicSensor().open(); ultrasonic=UltrasonicMonitor(sensor); ultrasonic.start()
        except UltrasonicError as exc: ultrasonic_error=str(exc); print(f"ULTRASONIC UNAVAILABLE: {exc}",flush=True)
        tracker=LightweightTracker(); smoother=MotionSmoother(); gate=NewestFrameGate(); monitor=SystemMonitor(); history=TransitionHistory(); analyzer=GroundAnalyzer(detector.cv2,detector.np); estimator=SparseCameraMotionEstimator(detector.cv2,detector.np); result=GroundResult(UNKNOWN,(UNKNOWN,UNKNOWN,UNKNOWN),UNKNOWN,GroundEvidence(0,0,0,0,0)); last_detect=last_ground=next_log=0.
        print("SENSE PHASE 6 GROUND PROTOTYPE | Gemini/cloud/audio/navigation: DISABLED",flush=True)
        camera=Camera(resolution=(args.camera_width,args.camera_height),pixel_format="RGB888").open(); reader=CameraReader(camera); reader.start()
        while True:
            now=time.monotonic()
            if args.benchmark_seconds and now-stats.started_at>=args.benchmark_seconds: break
            frame,age=reader.latest()
            if frame is None or not gate.accept(id(frame)): time.sleep(.002); continue
            if not args.max_detection_fps or now-last_detect>=1/args.max_detection_fps:
                last_detect=time.monotonic(); detections,inference=detector.detect(frame,args.confidence); stats.inference.append(inference); camera_motion=smoother.update(estimator.update(frame,detections)[0]); tracker.update(detections,time.monotonic())
            if 'camera_motion' not in locals(): continue
            if now-last_ground>=1/args.ground_fps:
                last_ground=now; started=time.perf_counter(); result=analyzer.analyze(frame,tracker.tracks_for_display(),camera_motion,history); stats.ground.append((time.perf_counter()-started)*1000); sample=monitor.sample(); stats.freshness.append((age+time.monotonic()-last_detect)*1000)
                for key,target in (("cpu",stats.cpu),("rss",stats.rss),("temp",stats.temp)):
                    if sample.get(key) is not None: target.append(float(sample[key]))
                snapshot=ultrasonic.snapshot() if ultrasonic else {"distance_cm":None,"age_s":None,"healthy":False,"error":ultrasonic_error}; forward,state=forward_distance(snapshot,.5)
                if now>=next_log: print(concise(result,camera_motion)+(f" | FORWARD OBSTACLE: {forward:.0f} cm ({state})" if forward is not None else " | FORWARD OBSTACLE: DISTANCE UNAVAILABLE"),flush=True); next_log=now+1
            if not args.no_preview:
                detector.cv2.imshow("Sense Phase 6 ground prototype",draw(frame.copy(),result,tracker.tracks_for_display(args.debug_ground),camera_motion,detector.cv2))
                if detector.cv2.waitKey(1)&0xFF in (ord('q'),27): break
            time.sleep(.002)
    except KeyboardInterrupt: print("Stopping on Ctrl-C.",flush=True)
    except CameraError as exc: print(f"CAMERA FAILED: {exc}",file=sys.stderr); return 3
    except Exception as exc: print(f"PHASE 6 FAILED: {type(exc).__name__}: {exc}",file=sys.stderr); return 3
    finally:
        elapsed=time.monotonic()-stats.started_at
        if reader: reader.stop()
        if camera: camera.close()
        if ultrasonic: ultrasonic.stop()
        if sensor: sensor.close()
        if not args.no_preview: detector.cv2.destroyAllWindows()
        print(stats.summary(elapsed),flush=True)
    return 0

if __name__=="__main__": raise SystemExit(main())
