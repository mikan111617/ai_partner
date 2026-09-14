from __future__ import annotations

import math
import queue
import threading
import time
import wave
from pathlib import Path
from typing import Any, Callable

import numpy as np


class VNyanBridge:
    def __init__(self, cfg: dict[str, Any]):
        self.enabled = bool(cfg.get("enabled", False))
        self.url = str(cfg.get("websocket_url", "ws://127.0.0.1:8000/vnyan"))
        self.events = dict(cfg.get("events") or {})
        self.reset_after_speech = bool(cfg.get("reset_after_speech", True))
        self._queue: queue.Queue[str | None] = queue.Queue(maxsize=64)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="vnyan", daemon=True)
        if self.enabled:
            self._thread.start()
            print(f"[vnyan] expression bridge target={self.url}")

    def send(self, command: str, *, quiet: bool = False) -> None:
        if not self.enabled:
            return
        command = str(command or "").strip()
        if not command:
            return
        try:
            self._queue.put_nowait(command)
        except queue.Full:
            if not quiet:
                print("[vnyan] command queue full")

    def expression(self, emotion: str) -> None:
        event = str(self.events.get(str(emotion or "neutral"), "") or "").strip()
        if event:
            self.send(event)

    def neutral(self) -> None:
        self.expression("neutral")

    def _run(self) -> None:
        ws = None
        unavailable_reported = False
        while not self._stop.is_set():
            try:
                command = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if command is None:
                break
            try:
                if ws is None:
                    from websockets.sync.client import connect
                    ws = connect(self.url, open_timeout=2.0, close_timeout=0.5)
                    print("[vnyan] connected" if not unavailable_reported else "[vnyan] reconnected")
                    unavailable_reported = False
                ws.send(command)
                if not command.startswith("ai_mouth "):
                    print(f"[vnyan] -> {command}")
            except Exception as exc:
                try:
                    if ws is not None:
                        ws.close()
                except Exception:
                    pass
                ws = None
                if not unavailable_reported:
                    print(f"[vnyan] unavailable; will retry ({type(exc).__name__}: {exc})")
                    unavailable_reported = True
        try:
            if ws is not None:
                ws.close()
        except Exception:
            pass

    @staticmethod
    def _pcm(raw: bytes, width: int) -> np.ndarray:
        if width == 1:
            x = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
            return (x - 128.0) / 128.0
        if width == 2:
            return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        if width == 4:
            return np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
        return np.zeros(0, dtype=np.float32)

    def _mouth_values(self, path: Path, fps: float = 20.0) -> tuple[list[int], float]:
        with wave.open(str(path), "rb") as wf:
            rate = max(1, wf.getframerate())
            channels = max(1, wf.getnchannels())
            width = wf.getsampwidth()
            raw = wf.readframes(wf.getnframes())
        samples = self._pcm(raw, width)
        if channels > 1 and samples.size >= channels:
            usable = (samples.size // channels) * channels
            samples = samples[:usable].reshape(-1, channels).mean(axis=1)
        if not samples.size:
            return [0], 1.0 / fps
        hop = max(1, int(rate / fps))
        rms = []
        for start in range(0, samples.size, hop):
            chunk = samples[start:start+hop]
            if chunk.size:
                rms.append(float(math.sqrt(float(np.mean(chunk * chunk)) + 1e-12)))
        values_arr = np.asarray(rms, dtype=np.float32)
        floor = float(np.percentile(values_arr, 18)) if values_arr.size else 0.0
        peak = float(np.percentile(values_arr, 92)) if values_arr.size else 1.0
        span = max(1e-5, peak - floor)
        current = 0.0
        values: list[int] = []
        for level in rms:
            target = 0.0 if level <= max(0.004, floor*1.08) else ((max(0.0, min(1.0, (level-floor)/span)))**0.62)*100.0
            current += (target-current) * (0.72 if target >= current else 0.46)
            values.append(int(round(max(0.0, min(100.0, current)))))
        values.extend([0, 0])
        return values, 1.0/fps

    def play_with_lipsync(self, path: Path, playback: Callable[[Path], None]) -> None:
        if not self.enabled:
            playback(path)
            return
        try:
            values, interval = self._mouth_values(path)
        except Exception:
            playback(path)
            return
        stop = threading.Event()
        def worker() -> None:
            started = time.perf_counter()
            for i, value in enumerate(values):
                if stop.is_set():
                    break
                delay = started + i*interval - time.perf_counter()
                if delay > 0 and stop.wait(delay):
                    break
                self.send(f"ai_mouth {value}", quiet=True)
            self.send("ai_mouth 0", quiet=True)
        thread = threading.Thread(target=worker, name="vnyan-lipsync", daemon=True)
        thread.start()
        try:
            playback(path)
        finally:
            stop.set()
            thread.join(timeout=0.25)
            self.send("ai_mouth 0", quiet=True)

    def stop(self) -> None:
        if not self.enabled:
            return
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._thread.join(timeout=1.0)
