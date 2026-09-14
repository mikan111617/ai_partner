from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

from .persona import persona


LONG_STATE = ("trust", "affection", "respect", "antipathy")
SHORT_EMOTIONS = ("joy", "anger", "sadness", "fear", "disgust", "embarrassment", "excitement")


def clamp(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _bounded_add(current: float, amount: float, gain: float = 1.0) -> float:
    current = clamp(current)
    if amount == 0:
        return current
    strength = 1.0 - math.exp(-abs(float(amount)) * max(0.0, gain))
    if amount > 0:
        return clamp(current + (1.0 - current) * strength)
    return clamp(current - current * strength)


class MindEngine:
    """Persistent relationship state plus short emotions.

    Baseline arousal is deliberately NOT the intensity of the current utterance.
    The current utterance's emotion/intensity comes from the current event/LLM turn.
    Arousal only affects delivery energy.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.profile = persona()
        self.state: dict[str, float] = {
            "trust": 0.30,
            "affection": 0.25,
            "respect": 0.25,
            "antipathy": 0.08,
            "joy": 0.04,
            "anger": 0.02,
            "sadness": 0.02,
            "fear": 0.02,
            "disgust": 0.02,
            "embarrassment": 0.02,
            "excitement": 0.05,
            "curiosity": 0.20,
            "boredom": 0.10,
            "arousal": 0.05,
            "concern": 0.02,
        }
        self.targets = {name: self.state[name] for name in SHORT_EMOTIONS}
        self.last_meaningful_event = time.time()
        self.last_spoken = 0.0
        self._load()
        self._refresh_derived()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            values = raw.get("state", raw)
            if isinstance(values, dict):
                for key in self.state:
                    if key in values:
                        self.state[key] = clamp(values[key])
            self.last_spoken = float(raw.get("last_spoken", 0.0) or 0.0)
            self.targets = {name: self.state[name] for name in SHORT_EMOTIONS}
        except Exception:
            pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"schema_version": 1, "state": self.state, "last_spoken": self.last_spoken}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _refresh_derived(self) -> None:
        peak = max((self.state[name] for name in SHORT_EMOTIONS), default=0.0)
        mean = sum(self.state[name] for name in SHORT_EMOTIONS) / len(SHORT_EMOTIONS)
        self.state["arousal"] = clamp(0.78 * peak + 0.22 * mean)

    def tick(self, dt: float) -> None:
        dt = max(0.0, float(dt))
        if not dt:
            return
        fall = {"joy": 8.0, "anger": 16.0, "sadness": 22.0, "fear": 9.0, "disgust": 18.0, "embarrassment": 10.0, "excitement": 8.0}
        for name in SHORT_EMOTIONS:
            self.targets[name] *= math.exp(-dt / fall[name])
            tau = 1.0 if self.targets[name] > self.state[name] else fall[name]
            alpha = 1.0 - math.exp(-dt / tau)
            self.state[name] = clamp(self.state[name] + (self.targets[name] - self.state[name]) * alpha)
        self.state["curiosity"] *= math.exp(-dt / 40.0)
        self.state["concern"] *= math.exp(-dt / 20.0)
        if time.time() - self.last_meaningful_event > 12.0:
            self.state["boredom"] = _bounded_add(self.state["boredom"], dt * float(self.profile.get("quiet_boredom_rate", 0.002)))
        else:
            self.state["boredom"] *= math.exp(-dt / 180.0)
        self._refresh_derived()

    @staticmethod
    def _signals(event: dict[str, Any]) -> dict[str, float]:
        return {str(k): clamp(v) for k, v in (event.get("signals") or {}).items() if isinstance(v, (int, float))}

    @staticmethod
    def _weighted(weights: dict[str, float], signals: dict[str, float]) -> float:
        return sum(float(weight) * signals.get(name, 0.0) for name, weight in weights.items())

    def appraise(self, event: dict[str, Any]) -> dict[str, float]:
        signals = self._signals(event)
        importance = clamp(event.get("importance", 0.5))
        strength = 0.45 + 0.55 * importance
        relational = str(event.get("actor_scope") or "unknown") == "user"
        delta: dict[str, float] = {}

        if relational:
            for name in LONG_STATE:
                raw = self._weighted(self.profile["sentiment_weights"].get(name, {}), signals) * strength
                before = self.state[name]
                self.state[name] = _bounded_add(before, raw, gain=0.34)
                delta[name] = self.state[name] - before

        for name in SHORT_EMOTIONS:
            raw = max(0.0, self._weighted(self.profile["emotion_weights"].get(name, {}), signals) * strength)
            if raw:
                before = self.targets[name]
                self.targets[name] = _bounded_add(before, raw, gain=1.0)
                delta[name] = self.targets[name] - before

        curiosity = max(0.0, self._weighted(self.profile.get("curiosity_weights", {}), signals) * strength)
        if curiosity:
            before = self.state["curiosity"]
            self.state["curiosity"] = _bounded_add(before, curiosity, gain=0.8)
            delta["curiosity"] = self.state["curiosity"] - before

        boredom = max(0.0, self._weighted(self.profile.get("boredom_weights", {}), signals) * strength)
        if boredom:
            self.state["boredom"] = _bounded_add(self.state["boredom"], boredom, gain=0.8)
        relief = max(signals.get("novelty", 0.0), signals.get("adventure", 0.0), signals.get("boldness", 0.0), signals.get("victory", 0.0))
        if relief:
            self.state["boredom"] = clamp(self.state["boredom"] - 0.30 * relief)

        if signals.get("player_defeated", 0.0) or signals.get("heavy_damage", 0.0):
            self.state["concern"] = _bounded_add(self.state["concern"], 0.22 * max(signals.get("player_defeated", 0.0), signals.get("heavy_damage", 0.0)), gain=1.0)

        if importance > 0.18:
            self.last_meaningful_event = time.time()
        self._refresh_derived()
        return delta

    def mood_valence(self) -> float:
        positive = 0.35 * self.state["joy"] + 0.28 * self.state["excitement"] + 0.18 * self.state["affection"] + 0.12 * self.state["trust"]
        negative = 0.36 * self.state["anger"] + 0.34 * self.state["sadness"] + 0.30 * self.state["disgust"] + 0.18 * self.state["antipathy"]
        return max(-1.0, min(1.0, (positive - negative) * 1.6))

    def presentation(self, local_emotion: str, local_intensity: float) -> dict[str, Any]:
        """Resolve current face/voice without turning baseline excitement into rage.

        local_intensity must come from the current trigger. Mood can soften/harshen it
        a little; arousal only becomes delivery_energy.
        """
        emotion = str(local_emotion or "neutral").lower()
        if emotion not in {"neutral", "happy", "surprise", "angry", "worry", "sad", "thinking", "excited"}:
            emotion = "neutral"
        intensity = clamp(local_intensity)
        valence = self.mood_valence()
        if emotion in {"angry", "sad", "worry"} and valence > 0:
            intensity *= 1.0 - 0.38 * valence
            intensity = max(intensity, clamp(local_intensity) * 0.50)
        elif emotion in {"angry", "sad", "worry"} and valence < 0:
            intensity = min(1.0, intensity * (1.0 + 0.15 * abs(valence)))
        elif emotion in {"happy", "excited"} and valence < 0:
            intensity *= 1.0 - 0.25 * abs(valence)

        return {
            "emotion": emotion,
            "intensity": clamp(intensity),
            "delivery_energy": clamp(0.25 + 0.75 * self.state["arousal"]),
            "mood_valence": valence,
        }

    def initiative_reason(self, cfg: dict[str, Any]) -> str:
        if not bool(cfg.get("enabled", True)):
            return ""
        if self.state["boredom"] >= float(cfg.get("boredom_threshold", 0.82)):
            return "少し退屈している"
        threshold = float(cfg.get("emotion_threshold", 0.90))
        strongest = max(SHORT_EMOTIONS, key=lambda name: self.state[name])
        if self.state[strongest] >= threshold:
            return f"{strongest}の感情が強く残っている"
        return ""
