from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml


@dataclass(slots=True)
class GamePack:
    root: Path
    manifest: dict[str, Any]
    id: str = field(init=False)
    name: str = field(init=False)
    tracking_cfg: dict[str, Any] = field(init=False)
    match_cfg: dict[str, Any] = field(init=False)
    gameplay_rules: list[dict[str, Any]] = field(init=False)

    def __post_init__(self) -> None:
        raw = str(self.manifest.get("id") or self.root.name).lower()
        self.id = re.sub(r"[^a-z0-9_-]+", "_", raw).strip("_") or "game"
        self.name = str(self.manifest.get("name") or self.id)
        self.tracking_cfg = dict(self.manifest.get("tracking") or {})
        self.match_cfg = dict(self.manifest.get("match") or {})
        self.gameplay_rules = [
            dict(item)
            for item in (self.manifest.get("gameplay_rules") or [])
            if isinstance(item, dict)
        ]

    def resolve(self, raw: str) -> Path:
        path = Path(raw)
        return path if path.is_absolute() else self.root / path

    def matches(self, title: str, process: str, process_path: str) -> bool:
        proc = str(process or "").lower()
        path = str(process_path or "").replace("/", "\\").lower()
        names = {str(x).lower() for x in (self.match_cfg.get("process_names") or [])}
        contains = [
            str(x).replace("/", "\\").lower()
            for x in (self.match_cfg.get("process_path_contains") or [])
        ]

        if proc in names and (not contains or any(value in path for value in contains)):
            return True
        if contains and any(value in path for value in contains):
            return True

        searchable = f"{title}\n{process}"
        for raw_pattern in self.match_cfg.get("window_patterns") or []:
            try:
                if re.search(str(raw_pattern), searchable, re.I):
                    return True
            except re.error:
                continue
        return False


@dataclass(slots=True)
class Foreground:
    title: str = ""
    process: str = ""
    process_path: str = ""


def foreground_window() -> Foreground:
    if os.name != "nt":
        return Foreground()

    title = ""
    process = ""
    process_path = ""
    try:
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = user32.GetForegroundWindow()
        if hwnd:
            length = user32.GetWindowTextLengthW(hwnd)
            title_buffer = ctypes.create_unicode_buffer(max(1, length + 1))
            user32.GetWindowTextW(hwnd, title_buffer, len(title_buffer))
            title = title_buffer.value

            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                handle = kernel32.OpenProcess(0x1000, False, pid.value)
                if handle:
                    try:
                        size = wintypes.DWORD(32768)
                        process_buffer = ctypes.create_unicode_buffer(size.value)
                        if kernel32.QueryFullProcessImageNameW(
                            handle,
                            0,
                            process_buffer,
                            ctypes.byref(size),
                        ):
                            process_path = process_buffer.value
                            process = Path(process_path).name
                    finally:
                        kernel32.CloseHandle(handle)
    except Exception:
        pass

    return Foreground(title.strip(), process.strip(), process_path.strip())


class GamePackManager:
    """Local game integration without loading or indexing game script files.

    Supported public integration modes:
      - state_file: an owned/developed/authorized game writes explicit events.
      - screen_ocr: detect configured gameplay text from what is currently visible.
    """

    def __init__(
        self,
        cfg: dict[str, Any],
        capture_cfg: dict[str, Any],
        on_event: Callable[[GamePack, dict[str, Any]], None],
    ):
        self.cfg = cfg
        self.capture_cfg = capture_cfg
        self.on_event = on_event

        raw_root = str(cfg.get("directory", "local_games") or "local_games")
        root = Path(raw_root)
        self.root = root if root.is_absolute() else Path.cwd() / root

        self.packs: list[GamePack] = []
        self.current: GamePack | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_digest: dict[str, str] = {}
        self._last_ocr: dict[str, float] = {}
        self._rule_times: dict[tuple[str, int], float] = {}
        self._seen_state_events: set[str] = set()
        self._present_summary: dict[str, str] = {}

        if bool(cfg.get("enabled", True)):
            self._load()

    def _load(self) -> None:
        if not self.root.exists():
            print(f"[game-pack] local directory not found (optional): {self.root}")
            return

        active = {str(x).lower() for x in (self.cfg.get("active") or [])}
        for manifest_path in sorted(self.root.glob("*/game.yaml")):
            try:
                data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
            except Exception as exc:
                print(f"[game-pack] invalid {manifest_path}: {exc}")
                continue

            if not isinstance(data, dict) or not bool(data.get("enabled", True)):
                continue

            pack = GamePack(manifest_path.parent, data)
            if active and pack.id not in active:
                continue
            self.packs.append(pack)

        if self.packs:
            print(
                "[game-pack] loaded: "
                + ", ".join(f"{pack.name} ({pack.id})" for pack in self.packs)
            )

    def start(self) -> None:
        if self.packs and self._thread is None:
            self._thread = threading.Thread(
                target=self._run,
                name="game-packs",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def present_context(self) -> str:
        if self.current is None:
            return "デスクトップ"
        summary = self._present_summary.get(self.current.id, "").strip()
        if summary:
            return f"{self.current.name}をプレイ中。直近の出来事: {summary}"
        return f"{self.current.name}をプレイ中"

    def _ocr(self) -> str:
        import cv2
        import mss
        import numpy as np
        import winocr

        monitor_index = max(1, int(self.capture_cfg.get("monitor", 1)))
        with mss.mss() as sct:
            monitor = sct.monitors[min(monitor_index, len(sct.monitors) - 1)]
            shot = np.asarray(sct.grab(monitor))
            frame = cv2.cvtColor(shot, cv2.COLOR_BGRA2BGR)

        width = frame.shape[1]
        if width < 1600:
            scale = min(2.0, 1600.0 / max(1, width))
            frame = cv2.resize(
                frame,
                None,
                fx=scale,
                fy=scale,
                interpolation=cv2.INTER_CUBIC,
            )

        result = winocr.recognize_cv2_sync(frame, "ja")
        if isinstance(result, dict):
            return str(result.get("text") or "").strip()
        return str(getattr(result, "text", "") or "").strip()

    def _apply_rules(self, pack: GamePack, text: str) -> None:
        now = time.time()
        for index, rule in enumerate(pack.gameplay_rules):
            pattern = str(rule.get("pattern") or "")
            if not pattern:
                continue
            try:
                matched = re.search(pattern, text, re.I) is not None
            except re.error:
                continue
            if not matched:
                continue

            cooldown = max(0.5, float(rule.get("cooldown_seconds", 8.0)))
            key = (pack.id, index)
            if now - self._rule_times.get(key, 0.0) < cooldown:
                continue
            self._rule_times[key] = now

            summary = str(
                rule.get("summary")
                or f"画面上で設定済みのゲームイベントを検出 ({rule.get('event_type', 'gameplay')})"
            )
            event = {
                "event_type": str(rule.get("event_type") or "gameplay"),
                "actor_scope": str(rule.get("actor_scope") or "user"),
                "summary": summary,
                "importance": float(rule.get("importance", 0.6)),
                "signals": dict(rule.get("signals") or {}),
            }
            self._present_summary[pack.id] = summary
            self.on_event(pack, event)

    def _emit_state_event(
        self,
        pack: GamePack,
        raw_event: dict[str, Any],
        *,
        default_actor: str,
    ) -> None:
        event = dict(raw_event)
        event.setdefault("actor_scope", default_actor)
        event.setdefault("event_type", "gameplay")
        event.setdefault("importance", 0.6)
        event.setdefault("signals", {})
        event.setdefault("summary", event.get("event_type", "gameplay"))

        event_id = str(
            event.get("event_id")
            or hashlib.sha1(
                json.dumps(event, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
        )
        if event_id in self._seen_state_events:
            return
        self._seen_state_events.add(event_id)

        summary = str(event.get("summary") or event.get("event_type") or "gameplay")
        self._present_summary[pack.id] = summary
        self.on_event(pack, event)

    def _handle_state(self, pack: GamePack) -> None:
        raw = str(pack.tracking_cfg.get("state_file") or "runtime_state.json")
        path = pack.resolve(raw)
        if not path.exists():
            return

        try:
            data_bytes = path.read_bytes()
            digest = hashlib.sha1(data_bytes).hexdigest()
        except OSError:
            return

        if self._state_digest.get(pack.id) == digest:
            return
        self._state_digest[pack.id] = digest

        try:
            state = json.loads(data_bytes.decode("utf-8-sig"))
        except Exception as exc:
            print(f"[game-pack:{pack.id}] invalid state file: {exc}")
            return
        if not isinstance(state, dict):
            return

        if isinstance(state.get("current_event"), dict):
            self._emit_state_event(
                pack,
                state["current_event"],
                default_actor="unknown",
            )

        if isinstance(state.get("player_event"), dict):
            self._emit_state_event(
                pack,
                state["player_event"],
                default_actor="user",
            )

        if isinstance(state.get("events"), list):
            for item in state["events"]:
                if isinstance(item, dict):
                    self._emit_state_event(
                        pack,
                        item,
                        default_actor=str(item.get("actor_scope") or "unknown"),
                    )

    def _handle_ocr(self, pack: GamePack) -> None:
        interval = max(0.6, float(pack.tracking_cfg.get("interval_seconds", 0.9)))
        now = time.time()
        if now - self._last_ocr.get(pack.id, 0.0) < interval:
            return
        self._last_ocr[pack.id] = now

        try:
            text = self._ocr()
        except Exception as exc:
            print(
                f"[game-pack:{pack.id}] OCR unavailable: "
                f"{type(exc).__name__}: {exc}"
            )
            time.sleep(2)
            return

        if text:
            self._apply_rules(pack, text)

    def _run(self) -> None:
        while not self._stop.is_set():
            foreground = foreground_window()
            selected = next(
                (
                    pack
                    for pack in self.packs
                    if pack.matches(
                        foreground.title,
                        foreground.process,
                        foreground.process_path,
                    )
                ),
                None,
            )

            if selected is not self.current:
                self.current = selected
                print(f"[game-pack] active={selected.name if selected else 'none'}")

            if selected is not None:
                method = str(
                    selected.tracking_cfg.get("method") or "state_file"
                ).lower()
                if method == "state_file":
                    self._handle_state(selected)
                elif method == "screen_ocr":
                    self._handle_ocr(selected)
                else:
                    print(
                        f"[game-pack:{selected.id}] unsupported tracking.method={method}"
                    )
                    self._stop.wait(1.0)

            self._stop.wait(0.20)
