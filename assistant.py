"""Non-blocking voice assistant for Sense.

ALSA capture and wake detection stay local. Audio is sent to Gemini only
after the configured wake phrase has been detected. Every failure is caught
inside this worker so the safety threads remain independent.
"""
import audioop
import collections
import os
import shutil
import subprocess
import tempfile
import threading
import time
import wave

import config
import vision


class MicrophoneError(RuntimeError):
    pass


def list_recording_devices():
    """Return `arecord -l` output without assuming a card/device number."""
    if shutil.which("arecord") is None:
        raise MicrophoneError("arecord not found; install alsa-utils")
    result = subprocess.run(["arecord", "-l"], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=10)
    text = result.stdout.decode("utf-8", "replace").strip()
    if result.returncode:
        raise MicrophoneError(text or "arecord -l failed")
    return text


class MicrophoneStream:
    def __init__(self, device=None):
        self.device = device
        self.process = None
        self.chunk_bytes = int(config.ASSISTANT_SAMPLE_RATE *
                               config.ASSISTANT_CHUNK_MS / 1000) * 2

    def open(self):
        if shutil.which("arecord") is None:
            raise MicrophoneError("arecord not found; install alsa-utils")
        command = ["arecord", "-q", "-t", "raw", "-f", "S16_LE",
                   "-r", str(config.ASSISTANT_SAMPLE_RATE), "-c", "1"]
        if self.device:
            command[1:1] = ["-D", self.device]
        try:
            self.process = subprocess.Popen(command, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE)
        except Exception as exc:
            raise MicrophoneError("{}: {}".format(type(exc).__name__, exc))
        time.sleep(0.15)
        if self.process.poll() is not None:
            detail = self.process.stderr.read().decode("utf-8", "replace")
            raise MicrophoneError("microphone unavailable: {}".format(
                " ".join(detail.split()) or "arecord exited"))
        return self

    def read(self):
        if self.process is None or self.process.stdout is None:
            raise MicrophoneError("microphone is not open")
        data = self.process.stdout.read(self.chunk_bytes)
        if len(data) != self.chunk_bytes:
            detail = self.process.stderr.read().decode("utf-8", "replace")
            raise MicrophoneError("device disconnected or capture failed: {}".
                                  format(" ".join(detail.split())))
        return data

    def close(self):
        process, self.process = self.process, None
        if process is not None:
            try:
                process.terminate()
                process.wait(timeout=1)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass


def pcm_to_wav(pcm):
    handle = tempfile.NamedTemporaryFile(prefix="sense-question-", suffix=".wav",
                                         delete=False)
    path = handle.name
    handle.close()
    with wave.open(path, "wb") as wav:
        wav.setnchannels(1); wav.setsampwidth(2)
        wav.setframerate(config.ASSISTANT_SAMPLE_RATE); wav.writeframes(pcm)
    return path


class VoiceAssistant(threading.Thread):
    STATES = ("WAITING", "WAKE_DETECTED", "LISTENING", "TRANSCRIBING",
              "THINKING", "SPEAKING", "STOPPED")

    def __init__(self, speech, beeper=None, microphone_factory=MicrophoneStream,
                 decoder_factory=None, gemini_worker=None):
        super().__init__(name="voice-assistant", daemon=True)
        self.speech, self.beeper = speech, beeper
        self._microphone_factory = microphone_factory
        self._decoder_factory = decoder_factory or self._make_decoder
        self._gemini = gemini_worker
        self._stop_event = threading.Event()
        self._active_mic = None
        self._state = "STOPPED"
        self._lock = threading.Lock()
        self._history = collections.deque(
            maxlen=max(1, config.ASSISTANT_MAX_HISTORY_TURNS) * 2)
        self.error = None

    @property
    def state(self):
        with self._lock:
            return self._state

    def _set_state(self, state):
        with self._lock:
            self._state = state
        print("ASSISTANT STATE: {}".format(state), flush=True)

    @staticmethod
    def _make_decoder():
        try:
            from pocketsphinx import Decoder
        except ImportError as exc:
            raise RuntimeError("pocketsphinx is not installed") from exc
        return Decoder(keyphrase=config.ASSISTANT_WAKE_PHRASE,
                       kws_threshold=config.ASSISTANT_WAKE_THRESHOLD,
                       samprate=config.ASSISTANT_SAMPLE_RATE,
                       logfn=os.devnull)

    def open(self):
        self._decoder = self._decoder_factory()
        if self._gemini is None:
            vision.load_env_file()
            self._gemini = vision.GeminiWorker().open()
        # Probe capture now; failures disable only this feature.
        probe = self._microphone_factory(config.MIC_DEVICE).open()
        probe.close()
        print("VOICE: microphone ready ({})".format(
            config.MIC_DEVICE or "ALSA default"), flush=True)
        return self

    def run(self):
        while not self._stop_event.is_set():
            mic = None
            try:
                mic = self._microphone_factory(config.MIC_DEVICE).open()
                self._active_mic = mic
                self._wait_for_wake(mic)
            except Exception as exc:
                self.error = "{}: {}".format(type(exc).__name__, exc)
                print("VOICE ERROR: {}".format(self.error), flush=True)
                self._stop_event.wait(2.0)
            finally:
                if mic is not None:
                    mic.close()
                self._active_mic = None
        self._set_state("STOPPED")

    def _wait_for_wake(self, mic):
        decoder = self._decoder
        decoder.start_utt()
        self._set_state("WAITING")
        print("WAKE: waiting", flush=True)
        while not self._stop_event.is_set():
            data = mic.read()
            decoder.process_raw(data, False, False)
            if decoder.hyp() is not None:
                decoder.end_utt()
                print("WAKE: Hey Sense detected", flush=True)
                self._handle_request(mic)
                decoder.start_utt()
                self._set_state("WAITING")
                print("WAKE: waiting", flush=True)
        decoder.end_utt()

    def _handle_request(self, mic):
        self._set_state("WAKE_DETECTED")
        if self.beeper is not None:
            self.beeper.play_once(config.ASSISTANT_ACK_TONE)
            time.sleep(config.BEEP_DURATION_S + 0.08)
        self._set_state("LISTENING")
        print("LISTENING...", flush=True)
        pcm = self._record_utterance(mic)
        if not pcm:
            self._say_and_wait("I didn't catch that.")
            return
        path = pcm_to_wav(pcm)
        try:
            self._set_state("TRANSCRIBING")
            text = self._transcribe(path)
        finally:
            try: os.unlink(path)
            except OSError: pass
        if not text:
            print("STT ERROR: no usable speech", flush=True)
            self._say_and_wait("I didn't catch that.")
            return
        print("HEARD: {}".format(text), flush=True)
        self._set_state("THINKING")
        try:
            answer = self._answer(text)
        except Exception as exc:
            print("GEMINI ERROR: {}: {}".format(type(exc).__name__, exc),
                  flush=True)
            self._say_and_wait("I can't reach the assistant right now.")
            return
        print("GEMINI: {}".format(answer), flush=True)
        self._history.extend((("user", text), ("model", answer)))
        self._say_and_wait(answer)

    def _record_utterance(self, mic):
        chunks, speaking, silence = [], False, 0.0
        started = time.monotonic()
        chunk_s = config.ASSISTANT_CHUNK_MS / 1000.0
        while not self._stop_event.is_set():
            data = mic.read(); elapsed = time.monotonic() - started
            loud = audioop.rms(data, 2) >= config.ASSISTANT_ENERGY_THRESHOLD
            if loud:
                if not speaking:
                    speaking = True; print("SPEECH: detected", flush=True)
                chunks.append(data); silence = 0.0
            elif speaking:
                chunks.append(data); silence += chunk_s
                if silence >= config.ASSISTANT_SILENCE_TIMEOUT_S:
                    print("SPEECH: ended", flush=True); break
            if not speaking and elapsed >= config.ASSISTANT_SPEECH_START_TIMEOUT_S:
                break
            if elapsed >= config.ASSISTANT_MAX_RECORDING_S:
                print("SPEECH: maximum recording duration reached", flush=True)
                break
        print("RECORDING COMPLETE", flush=True)
        return b"".join(chunks) if speaking else b""

    def _transcribe(self, path):
        from google.genai import types
        with open(path, "rb") as source:
            audio = source.read()
        response = self._gemini._client.models.generate_content(
            model=config.ASSISTANT_GEMINI_MODEL,
            contents=[types.Part.from_bytes(data=audio, mime_type="audio/wav"),
                      "Transcribe this speech exactly. Return only the words spoken."],
        )
        return " ".join((getattr(response, "text", "") or "").split()).strip()

    def _answer(self, text):
        from google.genai import types
        context = "\n".join("{}: {}".format(role, value)
                            for role, value in self._history)
        prompt = (config.ASSISTANT_SYSTEM_PROMPT + "\n" + context +
                  "\nUser: " + text + "\nSense:")
        print("GEMINI REQUEST: {}".format(text), flush=True)
        response = self._gemini._client.models.generate_content(
            model=config.ASSISTANT_GEMINI_MODEL, contents=prompt,
            config=types.GenerateContentConfig(max_output_tokens=160,
                                               temperature=0.4),
        )
        answer = " ".join((getattr(response, "text", "") or "").split())
        if not answer:
            raise RuntimeError("empty response")
        return answer

    def _say_and_wait(self, text):
        self._set_state("SPEAKING")
        if self.speech is None or not self.speech.say(text):
            print("AUDIO ERROR: speech unavailable", flush=True); return
        deadline = time.monotonic() + config.SPEECH_MAX_HOLD_S + 3
        began = False
        while time.monotonic() < deadline and not self._stop_event.is_set():
            active = self.speech.speaking
            began = began or active
            if began and not active:
                print("SPEECH COMPLETE", flush=True); return
            time.sleep(0.05)

    def stop(self, timeout=3.0):
        self._stop_event.set()
        # arecord's stdout read is blocking. Closing its process is what lets
        # SIGTERM join this worker even when the microphone is silent.
        mic = self._active_mic
        if mic is not None:
            mic.close()
        if self.is_alive(): self.join(timeout=timeout)
