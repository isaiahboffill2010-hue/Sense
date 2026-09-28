# Phase 8 Event Manager

`Event` is a structured future-speech payload: source, type, entity/id, zone, state, priority, reasons, reliability, TTL-derived active state, and metadata. The manager never performs perception. It supports person adapters now, vehicle/ultrasonic/walking-space source namespaces through the same schema.

Priority is LOW/MEDIUM/HIGH/CRITICAL. Only independent ultrasonic close states should ever use stronger priority; visual approach remains HIGH. Deduplication keys include source, entity type/id, and event type. Default cooldowns are 8–20 seconds by event type, escalation/change bypasses cooldown, active records expire after type TTL plus one-second loss grace, and the output budget is two events per processing cycle; lower events drop rather than queue stale information.

```bash
cd ~/Sense
git pull
python3 -m unittest -v test_prototype_event_manager.py
python3 prototype_event_manager.py --simulate-events --debug-events
```

Live person integration is represented by `person_events()` consuming real Phase 7.5 observations; camera/detector ownership remains in the person prototype to avoid duplicate perception loops. No speech, cloud, audio, navigation, or production integration exists.
