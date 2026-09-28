# Phase 9 Speech Manager

Consumes only `EMIT` Event Manager actions. PERSON_AHEAD maps to “Person ahead.”, POSSIBLE_APPROACH to “Someone appears to be approaching.”, and directional crossings to short directional phrases. It uses the existing `GeminiSpeechPlayer`/eSpeak fallback path when wired, and dry-run makes no API or audio call. One pending request is retained; higher priority replaces lower, stale requests expire after five seconds, and duplicate event/phrase keys are suppressed.
