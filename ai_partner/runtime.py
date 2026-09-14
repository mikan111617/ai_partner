from __future__ import annotations

import queue
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .game_packs import GamePack, GamePackManager
from .llm import OllamaLLM
from .memory import MemoryStore
from .microphone import MicrophoneConversation, SpeechTurn
from .mind import MindEngine
from .vnyan import VNyanBridge
from .voice import VoiceOutput


@dataclass(slots=True)
class _Task:
    kind: str
    future: Future
    user_text: str = ""
    context_id: str = "normal:desktop"
    mode: str = "normal"


class PartnerRuntime:
    def __init__(self, cfg: dict[str, Any], data_dir: str | Path):
        self.cfg = cfg
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.mind = MindEngine(self.data_dir / "mind.json")
        self.memory = MemoryStore(self.data_dir / "memory.sqlite3")
        self.microphone = MicrophoneConversation(cfg["microphone"])
        self.llm = OllamaLLM(cfg["ollama"])
        self.vnyan = VNyanBridge(cfg.get("vnyan", {}))
        self.voice_active = False
        self.voice = VoiceOutput(cfg["voice"], self.vnyan, on_start=self._voice_start, on_end=self._voice_end)

        self._game_events: queue.Queue[tuple[GamePack, dict[str, Any]]] = queue.Queue(maxsize=32)
        self.games = GamePackManager(cfg.get("game_packs", {}), cfg.get("capture", {}), self._queue_game_event)

        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ai-partner")
        self.task: _Task | None = None
        self.pending_user: list[SpeechTurn] = []
        self.pending_game: tuple[GamePack, dict[str, Any]] | None = None
        self.drop_current_task = False

        now = time.time()
        self.started_at = now
        self.last_tick = now
        self.last_activity = now
        self.last_idle_check = 0.0
        self.last_game_reaction = 0.0
        self.last_save = 0.0
        self._stop = threading.Event()

    def _voice_start(self, _text: str) -> None:
        self.voice_active = True
        self.microphone.set_suspended(True)

    def _voice_end(self) -> None:
        self.voice_active = False
        self.microphone.set_suspended(False)
        self.mind.last_spoken = time.time()
        self.last_activity = time.time()

    def _queue_game_event(self, pack: GamePack, event: dict[str, Any]) -> None:
        try:
            self._game_events.put_nowait((pack, event))
        except queue.Full:
            try:
                self._game_events.get_nowait()
            except queue.Empty:
                pass
            try:
                self._game_events.put_nowait((pack, event))
            except queue.Full:
                pass

    def _current_mode_context(self) -> tuple[str, str]:
        selected = str(self.cfg.get("mode", "auto"))
        if selected == "normal":
            return "normal", "normal:desktop"
        if self.games.current is not None:
            return "game", f"game:{self.games.current.id}"
        if selected == "game":
            return "game", "game:generic"
        return "normal", "normal:desktop"

    def _submit_user(self, text: str) -> None:
        text = str(text or "").strip()
        if not text:
            return
        mode, context_id = self._current_mode_context()
        history = self.memory.conversation_context(context_id, int(self.cfg["memory"].get("recent_conversation_turns", 8)))
        events = self.memory.event_context(context_id, int(self.cfg["memory"].get("recent_events", 12)))
        if mode == "game" and self.games.current is not None:
            scenario = self.games.scenario_context()
            if scenario:
                events = (events + "\n\n" + scenario).strip()
        future = self.executor.submit(
            self.llm.respond,
            user_text=text,
            history=history,
            event_context=events,
            mind=dict(self.mind.state),
            mode=mode,
        )
        self.task = _Task("conversation", future, text, context_id, mode)
        self.drop_current_task = False
        print(f"[USER] [{context_id}] {text}")

    def _on_turn(self, turn: SpeechTurn) -> None:
        self.last_activity = time.time()
        if self.task is None:
            self._submit_user(turn.text)
            return

        self.pending_user.append(turn)
        if self.task.kind == "conversation":
            print("[conversation] continuing speech queued; current reply will be regenerated with all chunks")
            return

        # Scenario/idle comments are optional; direct speech always wins.
        if self.task.future.cancel():
            self.task = None
            merged = "。".join(x.text.strip("。 ") for x in self.pending_user if x.text.strip())
            self.pending_user.clear()
            self._submit_user(merged)
        else:
            self.drop_current_task = True
            print("[conversation] current background reaction will be discarded; user speech has priority")

    def _process_user_result(self, task: _Task, data: dict[str, Any]) -> None:
        event = {
            "summary": str(data.get("summary") or task.user_text),
            "event_type": "conversation",
            "actor_scope": "user",
            "importance": float(data.get("importance", 0.5) or 0.5),
            "signals": dict(data.get("signals") or {}),
        }
        delta = self.mind.appraise(event)
        if any(abs(v) >= 0.005 for v in delta.values()):
            shown = ", ".join(f"{k}={v:+.3f}" for k, v in delta.items() if abs(v) >= 0.005)
            print(f"[mind] {shown}")
        utterance = str(data.get("utterance") or "").strip()
        self.memory.add_conversation(task.context_id, task.user_text, utterance)
        if utterance:
            presentation = self.mind.presentation(str(data.get("local_emotion") or "neutral"), float(data.get("local_intensity", 0.35) or 0.35))
            self.voice.speak(utterance, presentation["emotion"], presentation["intensity"], presentation["delivery_energy"])

    def _process_game_result(self, data: dict[str, Any]) -> None:
        utterance = str(data.get("utterance") or "").strip()
        if not utterance:
            return
        presentation = self.mind.presentation(str(data.get("local_emotion") or "neutral"), float(data.get("local_intensity", 0.35) or 0.35))
        self.voice.speak(utterance, presentation["emotion"], presentation["intensity"], presentation["delivery_energy"])

    def _poll_task(self) -> None:
        task = self.task
        if task is None or not task.future.done():
            return
        self.task = None
        try:
            data = task.future.result()
        except Exception as exc:
            print(f"[llm] {type(exc).__name__}: {exc}")
            data = {}

        # If Whisper split one long utterance while the LLM was working, never
        # answer only the stale first fragment. Regenerate using the whole thought.
        if self.pending_user:
            chunks = [task.user_text] if task.kind == "conversation" else []
            chunks.extend(turn.text for turn in self.pending_user)
            self.pending_user.clear()
            merged = "。".join(str(x).strip("。 ") for x in chunks if str(x).strip())
            print(f"[conversation] joined continuing speech: {merged}")
            self._submit_user(merged)
            return

        if self.drop_current_task:
            self.drop_current_task = False
            return
        if task.kind == "conversation":
            self._process_user_result(task, data if isinstance(data, dict) else {})
        elif task.kind in {"game", "ambient"}:
            self._process_game_result(data if isinstance(data, dict) else {})

    def _drain_game_events(self) -> None:
        while True:
            try:
                pack, event = self._game_events.get_nowait()
            except queue.Empty:
                break
            context_id = f"game:{pack.id}"
            self.memory.add_event(context_id, str(event.get("event_type") or "game"), event)
            delta = self.mind.appraise(event)
            actor = str(event.get("actor_scope") or "unknown")
            if actor == "user" and any(abs(v) >= 0.005 for v in delta.values()):
                shown = ", ".join(f"{k}={v:+.3f}" for k, v in delta.items() if abs(v) >= 0.005)
                print(f"[mind/game] {shown}")
            self.pending_game = (pack, event)
            self.last_activity = time.time()

    def _maybe_game_reaction(self, now: float) -> None:
        if self.pending_game is None or self.task is not None or self.voice_active or self.pending_user:
            return
        if now - self.last_game_reaction < 4.5:
            return
        pack, event = self.pending_game
        self.pending_game = None
        self.last_game_reaction = now
        context = pack.corpus.context()
        future = self.executor.submit(self.llm.game_reaction, event=event, context=context, mind=dict(self.mind.state))
        self.task = _Task("game", future, context_id=f"game:{pack.id}", mode="game")

    def _maybe_ambient(self, now: float) -> None:
        icfg = self.cfg.get("initiative", {})
        if not bool(icfg.get("enabled", True)) or self.task is not None or self.voice_active or self.pending_user or self.pending_game:
            return
        if now - self.last_idle_check < float(icfg.get("idle_check_seconds", 3.0)):
            return
        self.last_idle_check = now
        if now - max(self.last_activity, self.mind.last_spoken) < float(icfg.get("min_speech_gap_seconds", 12.0)):
            return
        reason = self.mind.initiative_reason(icfg)
        if not reason:
            return
        # Deliberately no conversation history here. Silence must not resurrect an old topic.
        present = self.games.present_context()
        future = self.executor.submit(self.llm.ambient, reason=reason, present_context=present, mind=dict(self.mind.state))
        self.task = _Task("ambient", future, context_id=self._current_mode_context()[1], mode=self._current_mode_context()[0])
        self.last_activity = now

    def run(self) -> None:
        self.microphone.start()
        self.games.start()
        print("[runtime] started character=エリシア (ojousama). Ctrl+C to stop.")
        try:
            while not self._stop.is_set():
                now = time.time()
                self.mind.tick(now - self.last_tick)
                self.last_tick = now
                self._drain_game_events()

                turn = self.microphone.get_turn()
                if turn is not None:
                    self._on_turn(turn)

                self._poll_task()
                self._maybe_game_reaction(now)
                self._maybe_ambient(now)

                if now - self.last_save >= float(self.cfg.get("mind", {}).get("save_interval_seconds", 2.0)):
                    self.last_save = now
                    self.mind.save()
                time.sleep(0.04)
        except KeyboardInterrupt:
            print("\n[runtime] stopping")
        finally:
            self._stop.set()
            self.games.stop()
            self.microphone.stop()
            self.voice.stop()
            self.vnyan.stop()
            self.mind.save()
            self.memory.close()
            self.executor.shutdown(wait=False, cancel_futures=True)
