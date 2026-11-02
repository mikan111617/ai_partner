from __future__ import annotations

import ctypes
import hashlib
import json
import os
import random
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    lines: dict[str, Any] = field(init=False)
    reaction_style: str = field(init=False)

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
        self.lines = dict(self.manifest.get("lines") or {})
        self.reaction_style = str(self.manifest.get("reaction_style") or "").strip()

    @property
    def method(self) -> str:
        return str(self.tracking_cfg.get("method") or "state_file").lower()

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


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return ""


class GamePackManager:
    """Local game integration without loading or indexing game script files.

    Supported public integration modes:
      - state_file: an owned/developed/authorized game writes explicit events.
      - screen_ocr: detect configured gameplay text from what is currently visible.
      - http: the game posts events to http://127.0.0.1:<port>/event (browser/Electron games).
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
        self._lock = threading.Lock()
        self._http_server: ThreadingHTTPServer | None = None
        self._http_last: dict[str, float] = {}
        self._last_line: dict[tuple[str, str], int] = {}

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
        self._start_http()

    def stop(self) -> None:
        self._stop.set()
        if self._http_server is not None:
            try:
                self._http_server.shutdown()
                self._http_server.server_close()
            except Exception:
                pass
            self._http_server = None
        if self._thread:
            self._thread.join(timeout=2)

    # ---------- http: the game sends events directly ----------
    def _http_packs(self) -> list[GamePack]:
        return [pack for pack in self.packs if pack.method == "http"]

    def _start_http(self) -> None:
        packs = self._http_packs()
        if not packs or self._http_server is not None:
            return
        port = int(packs[0].tracking_cfg.get("port", 8765))
        manager = self

        class Handler(BaseHTTPRequestHandler):
            def _headers(self, code: int, body: bytes = b"") -> None:
                self.send_response(code)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_OPTIONS(self) -> None:  # noqa: N802
                self._headers(204)

            def do_GET(self) -> None:  # noqa: N802
                self._headers(200, b"ok")

            def do_POST(self) -> None:  # noqa: N802
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(min(length, 65536)) if length > 0 else b""
                    data = json.loads(raw.decode("utf-8-sig") or "{}")
                    count = manager.receive_http(data)
                    self._headers(200, f"ok {count}".encode())
                except Exception as exc:
                    self._headers(400, f"bad request: {type(exc).__name__}".encode())

            def log_message(self, *_args: Any) -> None:
                return

        try:
            self._http_server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        except OSError as exc:
            print(f"[game-pack] could not open event port {port}: {exc}")
            return
        threading.Thread(target=self._http_server.serve_forever, name="game-events-http", daemon=True).start()
        print(f"[game-pack] waiting for game events at http://127.0.0.1:{port}/event")

    def receive_http(self, data: Any) -> int:
        """Accept {"game": id, "event": {...}} or {"game": id, "events": [...]} or a bare event."""
        if not isinstance(data, dict):
            return 0
        packs = self._http_packs()
        if not packs:
            return 0
        wanted = str(data.get("game") or "").lower()
        pack = next((p for p in packs if p.id == wanted), packs[0])
        items: list[Any]
        if isinstance(data.get("events"), list):
            items = list(data["events"])
        elif isinstance(data.get("event"), dict):
            items = [data["event"]]
        elif "event_type" in data:
            items = [data]
        else:
            items = []
        count = 0
        with self._lock:
            self._http_last[pack.id] = time.time()
            if self.current is not pack:
                self.current = pack
                print(f"[game-pack] active={pack.name} (events from the game)")
            for item in items:
                if not isinstance(item, dict):
                    continue
                event = dict(item)
                # The same kind of event (e.g. "hit") happens many times; never treat it as a duplicate.
                event.setdefault("event_id", uuid.uuid4().hex)
                self._emit_state_event(pack, event, default_actor="user")
                count += 1
        return count

    def _http_active(self) -> GamePack | None:
        now = time.time()
        for pack in self._http_packs():
            hold = float(pack.tracking_cfg.get("hold_seconds", 600))
            if now - self._http_last.get(pack.id, 0.0) < hold:
                return pack
        return None

    # ---------- prepared lines (used without an LLM, or when it is too slow) ----------
    def pick_line(self, pack: GamePack | None, event: dict[str, Any]) -> dict[str, Any]:
        if pack is None:
            return {}
        kind = str(event.get("event_type") or "")
        spec = pack.lines.get(kind) or pack.lines.get("default")
        if not spec:
            return {}
        if isinstance(spec, list):
            spec = {"texts": spec}
        texts = [str(x) for x in (spec.get("texts") or []) if str(x).strip()]
        if not texts:
            return {}
        key = (pack.id, kind)
        choices = list(range(len(texts)))
        if len(choices) > 1 and key in self._last_line:
            choices.remove(self._last_line[key])
        index = random.choice(choices)
        self._last_line[key] = index
        values = _SafeDict()
        for source in (event, event.get("details") or {}):
            if isinstance(source, dict):
                values.update({str(k): v for k, v in source.items() if isinstance(v, (str, int, float))})
        try:
            text = texts[index].format_map(values)
        except (ValueError, IndexError):
            text = texts[index]
        return {
            "utterance": text,
            "local_emotion": str(spec.get("emotion") or "neutral"),
            "local_intensity": float(spec.get("intensity", 0.45)),
        }

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

            if selected is None:
                selected = self._http_active()

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
                elif method == "http":
                    pass
                else:
                    print(
                        f"[game-pack:{selected.id}] unsupported tracking.method={method}"
                    )
                    self._stop.wait(1.0)

            self._stop.wait(0.20)
