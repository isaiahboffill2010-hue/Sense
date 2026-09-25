#!/usr/bin/env python3
"""Pi-side, hardware-explicit voice diagnostics for Sense."""
import argparse
import os
import subprocess
import sys
import tempfile

import config
from assistant import list_recording_devices


def command(args):
    print("+ " + " ".join(args), flush=True)
    return subprocess.run(args).returncode


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("devices", "mic", "stt", "gemini",
                                          "tts", "complete"))
    args = parser.parse_args(argv)
    if args.stage == "devices":
        print(list_recording_devices()); return 0
    if args.stage in ("mic", "stt"):
        handle = tempfile.NamedTemporaryFile(prefix="sense-mic-", suffix=".wav",
                                             delete=False)
        path = handle.name; handle.close()
        capture = ["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1",
                   "-d", "5"]
        if config.MIC_DEVICE: capture[1:1] = ["-D", config.MIC_DEVICE]
        capture.append(path)
        try:
            print("microphone detected"); print("recording...")
            if command(capture): return 1
            print("recording complete")
            if args.stage == "mic":
                play = ["aplay", "-q"]
                if config.AUDIO_DEVICE: play += ["-D", config.AUDIO_DEVICE]
                print("playback..."); return command(play + [path])
            import vision
            from assistant import VoiceAssistant
            vision.load_env_file(); gemini = vision.GeminiWorker().open()
            print("HEARD: " + VoiceAssistant(None, gemini_worker=gemini)._transcribe(path))
            return 0
        finally:
            try: os.unlink(path)
            except OSError: pass
    if args.stage == "gemini":
        import vision
        from assistant import VoiceAssistant
        vision.load_env_file(); gemini = vision.GeminiWorker().open()
        print("GEMINI: " + VoiceAssistant(None, gemini_worker=gemini)._answer(
            "Say: The assistant connection works.")); return 0
    if args.stage == "tts":
        from hardware.audio import SpeechPlayer
        import time
        player = SpeechPlayer().open()
        try:
            player.speak("Sense voice test.")
            while player.is_speaking(): time.sleep(0.05)
            return 0
        finally: player.close()
    return command([sys.executable, "main.py", "--test-assistant"])


if __name__ == "__main__": sys.exit(main())
