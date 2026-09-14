from __future__ import annotations

import math
import queue
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass
from typing import Any

import numpy as np


@dataclass(slots=True)
class SpeechTurn:
    text: str
    ts: float
    endpoint_seconds: float = 0.0
    stt_seconds: float = 0.0


class MicrophoneConversation:
    def __init__(self, cfg: dict[str, Any]):
        self.enabled = bool(cfg.get("enabled", True))
        self.target_rate = int(cfg.get("sample_rate", 16000))
        self.model_name = str(cfg.get("model", "large-v3-turbo"))
        self.whisper_device = str(cfg.get("device_type", "cuda"))
        self.compute_type = str(cfg.get("compute_type", "int8_float16"))
        self.device_name = str(cfg.get("device", "") or "").strip()
        self.rms_threshold = float(cfg.get("rms_threshold", 0.015))
        self.silence_seconds = float(cfg.get("silence_seconds", 0.75))
        self.min_speech_seconds = float(cfg.get("min_speech_seconds", 0.35))
        self.max_speech_seconds = float(cfg.get("max_speech_seconds", 15.0))
        self.beam_size = int(cfg.get("beam_size", 3))
        self.available = False
        self._stop = threading.Event()
        self._suspended = threading.Event()
        self._thread: threading.Thread | None = None
        self._turns: queue.Queue[SpeechTurn] = queue.Queue(maxsize=8)

    def start(self) -> None:
        if self.enabled and self._thread is None:
            self._thread = threading.Thread(target=self._run, name="microphone-stt", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def set_suspended(self, value: bool) -> None:
        self._suspended.set() if value else self._suspended.clear()

    def get_turn(self) -> SpeechTurn | None:
        try:
            return self._turns.get_nowait()
        except queue.Empty:
            return None

    def _push(self, turn: SpeechTurn) -> None:
        try:
            self._turns.put_nowait(turn)
        except queue.Full:
            try:
                self._turns.get_nowait()
            except queue.Empty:
                pass
            try:
                self._turns.put_nowait(turn)
            except queue.Full:
                pass

    @staticmethod
    def _resample(audio: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
        audio = np.asarray(audio, dtype=np.float32).reshape(-1)
        if source_rate == target_rate or not audio.size:
            return audio
        from scipy.signal import resample_poly
        divisor = math.gcd(int(source_rate), int(target_rate))
        return np.asarray(resample_poly(audio, int(target_rate)//divisor, int(source_rate)//divisor), dtype=np.float32)

    def _select_device(self, sd):
        if not self.device_name:
            return None
        needle = self.device_name.lower()
        for index, info in enumerate(sd.query_devices()):
            if int(info.get("max_input_channels", 0) or 0) > 0 and needle in str(info.get("name", "")).lower():
                return index
        raise RuntimeError(f"Microphone containing '{self.device_name}' was not found")

    def _run(self) -> None:
        try:
            import sounddevice as sd
            from faster_whisper import WhisperModel

            print(f"[mic] loading faster-whisper model={self.model_name} device={self.whisper_device} compute={self.compute_type}")
            model = WhisperModel(self.model_name, device=self.whisper_device, compute_type=self.compute_type)
            selected = self._select_device(sd)
            info = sd.query_devices(selected, kind="input") if selected is not None else sd.query_devices(kind="input")
            rate = int(round(float(info.get("default_samplerate", self.target_rate))))
            channels = max(1, min(2, int(info.get("max_input_channels", 1) or 1)))
            frames = max(1, int(rate * 0.10))
            self.available = True
            print(f"[mic] ready microphone={info.get('name', 'default')} sample_rate={rate}")

            preroll: deque[np.ndarray] = deque(maxlen=3)
            parts: list[np.ndarray] = []
            speaking = False
            silent_for = 0.0
            speech_for = 0.0

            with sd.InputStream(device=selected, samplerate=rate, channels=channels, dtype="float32", blocksize=frames) as stream:
                while not self._stop.is_set():
                    data, _ = stream.read(frames)
                    data = np.asarray(data, dtype=np.float32)
                    mono = data[:, 0] if data.ndim == 2 else data.reshape(-1)
                    if self._suspended.is_set():
                        preroll.clear(); parts.clear(); speaking = False; silent_for = 0.0; speech_for = 0.0
                        continue
                    rms = float(np.sqrt(np.mean(np.square(mono), dtype=np.float64))) if mono.size else 0.0
                    voiced = rms >= self.rms_threshold
                    if not speaking:
                        preroll.append(mono.copy())
                        if voiced:
                            speaking = True; parts = list(preroll); preroll.clear(); speech_for = 0.1; silent_for = 0.0
                        continue
                    parts.append(mono.copy()); speech_for += 0.1
                    silent_for = 0.0 if voiced else silent_for + 0.1
                    if silent_for < self.silence_seconds and speech_for < self.max_speech_seconds:
                        continue
                    detected = time.time()
                    utterance_ts = detected - silent_for
                    audio = np.concatenate(parts) if parts else np.empty(0, dtype=np.float32)
                    voiced_seconds = max(0.0, speech_for - silent_for)
                    parts.clear(); speaking = False; endpoint = silent_for; silent_for = 0.0; speech_for = 0.0
                    if voiced_seconds < self.min_speech_seconds or not audio.size:
                        continue
                    audio = self._resample(audio, rate, self.target_rate)
                    started = time.perf_counter()
                    segments, _ = model.transcribe(audio, language="ja", beam_size=self.beam_size, vad_filter=True, condition_on_previous_text=False, temperature=0.0)
                    text = "".join(str(seg.text) for seg in segments).strip()
                    if text:
                        print(f"[mic] {text}")
                        self._push(SpeechTurn(text, utterance_ts, endpoint, time.perf_counter()-started))
        except Exception as exc:
            self.available = False
            print(f"[mic] disabled: {type(exc).__name__}: {exc}")
            traceback.print_exc()
