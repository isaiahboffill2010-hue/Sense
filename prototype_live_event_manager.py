#!/usr/bin/env python3
"""Single-pipeline live Phase 7.5 person awareness to Phase 8 Event Manager."""
from prototype_person_awareness import main
import sys
if __name__=="__main__":raise SystemExit(main(["--event-manager",*sys.argv[1:]]))
