from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml


DEFAULTS: dict[str, Any] = {
    "mode": "auto",
    "ollama": {
        "url": "http://127.0.0.1:11434",
        "conversation_model": "qwen3.5:9b",
        "game_model": "qwen3.5:4b",
        "cognition_model": "qwen3.5:4b",
        "timeout_seconds": 180,
        "keep_alive": "10m",
        "normal_num_ctx": 8192,
        "normal_num_gpu": 22,
        "game_num_ctx": 4096,
        "game_num_gpu": 0,
    },
    "microphone": {
        "enabled": True,
        "device": "",
        "sample_rate": 16000,
        "model": "large-v3-turbo",
        "device_type": "cuda",
        "compute_type": "int8_float16",
        "rms_threshold": 0.015,
        "silence_seconds": 0.75,
        "min_speech_seconds": 0.35,
        "max_speech_seconds": 15.0,
        "beam_size": 3,
    },
    "voice": {
        "provider": "console",
        "irodori_dir": "",
        "irodori_reference_audio": "",
    },
    "vnyan": {
        "enabled": False,
        "websocket_url": "ws://127.0.0.1:8000/vnyan",
    },
    "memory": {"recent_conversation_turns": 8, "recent_events": 12},
    "initiative": {
        "enabled": True,
        "idle_check_seconds": 3.0,
        "min_speech_gap_seconds": 12.0,
        "boredom_threshold": 0.82,
        "emotion_threshold": 0.90,
    },
    "mind": {"save_interval_seconds": 2.0},
    "game_packs": {"enabled": True, "directory": "local_games", "active": []},
    "capture": {"monitor": 1, "width": 960, "height": 540},
    "modes": {"game_window_patterns": []},
}


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        raise TypeError(f"{path} top level must be a mapping")
    return value


def load_config(path: str | Path = "config.yaml") -> dict[str, Any]:
    base_path = Path(path)
    cfg = _merge(DEFAULTS, _load(base_path))
    local = base_path.with_name("config.user.yaml")
    if local.exists():
        cfg = _merge(cfg, _load(local))
        print(f"[config] local overrides={local}")
    if str(cfg.get("mode", "auto")) not in {"auto", "normal", "game"}:
        raise ValueError("mode must be auto, normal, or game")
    return cfg
