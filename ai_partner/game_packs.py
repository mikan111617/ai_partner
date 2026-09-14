from __future__ import annotations

import csv
import ctypes
import hashlib
import json
import os
import re
import threading
import time
import unicodedata
from collections import Counter, deque
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

import yaml


def _clean(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.replace("<br>", "\n").replace("<BR>", "\n").replace("{LF}", "\n")
    text = re.sub(r"<ruby/[^>]+>(.*?)</ruby>", r"\1", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def _norm(value: Any) -> str:
    return re.sub(r"[^0-9A-Za-zぁ-んァ-ヶ一-龯々ー]+", "", _clean(value)).lower()


def _ngrams(text: str, n: int = 2) -> list[str]:
    if len(text) <= n:
        return [text] if text else []
    return [text[i:i+n] for i in range(len(text)-n+1)]


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-16", "cp932", "shift_jis"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="ignore")


@dataclass(slots=True)
class ScenarioEntry:
    key: str
    source: str
    line_no: int
    speaker: str
    text: str
    normalized: str


@dataclass(slots=True)
class ScenarioMatch:
    entry: ScenarioEntry
    confidence: float
    observed: str


class ScenarioCorpus:
    def __init__(self, pack: "GamePack"):
        self.pack = pack
        self.entries: list[ScenarioEntry] = []
        self.by_key: dict[str, ScenarioEntry] = {}
        self._index: dict[str, list[int]] = {}
        self._last_index: int | None = None
        self.recent: deque[ScenarioEntry] = deque(maxlen=8)
        self._recent_keys: set[str] = set()
        self._load()
        for idx, entry in enumerate(self.entries):
            for gram in set(_ngrams(entry.normalized)):
                self._index.setdefault(gram, []).append(idx)

    @property
    def ready(self) -> bool:
        return bool(self.entries)

    def _add(self, source: str, line_no: int, text: Any, speaker: Any = "", key: Any = "") -> None:
        cleaned = _clean(text)
        normalized = _norm(cleaned)
        minimum = max(2, int(self.pack.scenario_cfg.get("min_text_chars", 4)))
        if len(normalized) < minimum:
            return
        aliases = self.pack.scenario_cfg.get("speaker_aliases") or {}
        who = str(speaker or "").strip().strip("[]【】()（）")
        if isinstance(aliases, dict):
            who = str(aliases.get(who, aliases.get(who.lower(), who)) or "")
        item_key = str(key or f"{source}:{line_no}").strip()
        item = ScenarioEntry(item_key, source, int(line_no), who, cleaned, normalized)
        self.entries.append(item)
        self.by_key[item_key] = item

    def _load(self) -> None:
        cfg = self.pack.scenario_cfg
        if not bool(cfg.get("enabled", False)):
            return
        root = self.pack.resolve(str(cfg.get("directory", "scenario") or "scenario"))
        if not root.exists():
            print(f"[game-pack:{self.pack.id}] scenario folder not found: {root}")
            return
        patterns = cfg.get("files") or ["**/*"]
        if isinstance(patterns, str):
            patterns = [patterns]
        paths: set[Path] = set()
        for pattern in patterns:
            paths.update(p for p in root.glob(str(pattern)) if p.is_file())
        for path in sorted(paths):
            if path.suffix.lower() not in {".txt", ".ks", ".csv", ".tsv", ".json", ".jsonl"}:
                continue
            source = str(path.relative_to(root)).replace("\\", "/")
            try:
                if path.suffix.lower() in {".csv", ".tsv"}:
                    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
                    rows = csv.reader(_read_text(path).splitlines(), delimiter=delimiter)
                    text_col = int(cfg.get("csv_text_column", -1))
                    speaker_col = cfg.get("csv_speaker_column")
                    id_col = cfg.get("csv_id_column")
                    for line_no, row in enumerate(rows, 1):
                        if not row:
                            continue
                        def cell(col: Any) -> str:
                            if col is None: return ""
                            i = int(col); i = i if i >= 0 else len(row)+i
                            return str(row[i]).strip() if 0 <= i < len(row) else ""
                        self._add(source, line_no, cell(text_col), cell(speaker_col), cell(id_col))
                elif path.suffix.lower() in {".json", ".jsonl"}:
                    raw = _read_text(path)
                    if path.suffix.lower() == ".jsonl":
                        records = [json.loads(line) for line in raw.splitlines() if line.strip()]
                    else:
                        obj = json.loads(raw)
                        records = obj if isinstance(obj, list) else obj.get("lines", obj.get("entries", [obj]))
                    for line_no, row in enumerate(records, 1):
                        if isinstance(row, dict):
                            self._add(source, line_no, row.get(str(cfg.get("json_text_field", "text")), ""), row.get(str(cfg.get("json_speaker_field", "speaker")), ""), row.get(str(cfg.get("json_id_field", "id")), ""))
                else:
                    ignore = tuple(str(x) for x in (cfg.get("ignore_prefixes") or [";", "@", "#"]))
                    parse_speaker = bool(cfg.get("parse_speaker_prefix", False))
                    for line_no, raw in enumerate(_read_text(path).splitlines(), 1):
                        line = raw.strip()
                        if not line or line.startswith(ignore):
                            continue
                        speaker, text = "", line
                        if parse_speaker:
                            for sep in ("\t", "：", ":", "|"):
                                if sep in line:
                                    left, right = line.split(sep, 1)
                                    if left.strip() and right.strip() and len(left.strip()) <= 48:
                                        speaker, text = left.strip(), right.strip()
                                        break
                        self._add(source, line_no, text, speaker)
            except Exception as exc:
                print(f"[game-pack:{self.pack.id}] skip {path.name}: {type(exc).__name__}: {exc}")
        if self.entries:
            print(f"[game-pack:{self.pack.id}] loaded {len(self.entries):,} local scenario lines")

    def _remember(self, item: ScenarioEntry) -> bool:
        if item.key in self._recent_keys:
            return False
        if len(self.recent) == self.recent.maxlen and self.recent:
            self._recent_keys.discard(self.recent[0].key)
        self.recent.append(item)
        self._recent_keys.add(item.key)
        return True

    def mark_key(self, key: str) -> tuple[ScenarioEntry | None, bool]:
        item = self.by_key.get(str(key or "").strip())
        return (item, self._remember(item)) if item else (None, False)

    def best_match(self, observed: str, threshold: float) -> ScenarioMatch | None:
        query = _norm(observed)
        if len(query) < 4 or not self.entries:
            return None
        counts: Counter[int] = Counter()
        for gram in set(_ngrams(query)):
            counts.update(self._index.get(gram, ()))
        candidates = [idx for idx, _ in counts.most_common(140)]
        if self._last_index is not None:
            source = self.entries[self._last_index].source
            for idx in range(max(0, self._last_index-8), min(len(self.entries), self._last_index+40)):
                if self.entries[idx].source == source and idx not in candidates:
                    candidates.append(idx)
        best_idx, best = -1, 0.0
        qgrams = set(_ngrams(query))
        for idx in candidates:
            target = self.entries[idx].normalized
            ratio = SequenceMatcher(None, query, target, autojunk=False).ratio()
            tgrams = set(_ngrams(target))
            overlap = len(qgrams & tgrams) / max(1, min(len(qgrams), len(tgrams)))
            length = min(len(query), len(target)) / max(len(query), len(target))
            score = 0.62*ratio + 0.28*overlap + 0.10*length
            if len(query) >= 6 and (query in target or target in query): score = max(score, 0.90)
            if self._last_index is not None and self.entries[idx].source == self.entries[self._last_index].source and -3 <= idx-self._last_index <= 28: score += 0.05
            if score > best: best_idx, best = idx, min(1.0, score)
        if best_idx < 0 or best < threshold:
            return None
        return ScenarioMatch(self.entries[best_idx], best, observed)

    def remember_match(self, match: ScenarioMatch) -> bool:
        try: self._last_index = self.entries.index(match.entry)
        except ValueError: pass
        return self._remember(match.entry)

    def context(self) -> str:
        if not self.recent:
            return ""
        lines = [f"【{self.pack.name}：到達済みシナリオ】", "以下は実際に到達したと確認できた内容だけです。未到達の内容は使わないでください。"]
        for item in self.recent:
            lines.append(f"- {item.speaker or 'ナレーション/話者不明'}: {item.text[:240]}")
        lines.append("先の展開を予告・暗示・ネタバレしないでください。")
        return "\n".join(lines)


@dataclass(slots=True)
class GamePack:
    root: Path
    manifest: dict[str, Any]
    id: str = field(init=False)
    name: str = field(init=False)
    scenario_cfg: dict[str, Any] = field(init=False)
    tracking_cfg: dict[str, Any] = field(init=False)
    match_cfg: dict[str, Any] = field(init=False)
    gameplay_rules: list[dict[str, Any]] = field(init=False)
    corpus: ScenarioCorpus = field(init=False)

    def __post_init__(self) -> None:
        raw = str(self.manifest.get("id") or self.root.name).lower()
        self.id = re.sub(r"[^a-z0-9_-]+", "_", raw).strip("_") or "game"
        self.name = str(self.manifest.get("name") or self.id)
        self.scenario_cfg = dict(self.manifest.get("scenario") or {})
        self.tracking_cfg = dict(self.manifest.get("tracking") or {})
        self.match_cfg = dict(self.manifest.get("match") or {})
        self.gameplay_rules = [dict(x) for x in (self.manifest.get("gameplay_rules") or []) if isinstance(x, dict)]
        self.corpus = ScenarioCorpus(self)

    def resolve(self, raw: str) -> Path:
        path = Path(raw)
        return path if path.is_absolute() else self.root / path

    def matches(self, title: str, process: str, process_path: str) -> bool:
        proc = str(process or "").lower()
        path = str(process_path or "").replace("/", "\\").lower()
        names = {str(x).lower() for x in (self.match_cfg.get("process_names") or [])}
        contains = [str(x).replace("/", "\\").lower() for x in (self.match_cfg.get("process_path_contains") or [])]
        if proc in names and (not contains or any(x in path for x in contains)):
            return True
        if contains and any(x in path for x in contains):
            return True
        searchable = f"{title}\n{process}"
        for raw in self.match_cfg.get("window_patterns") or []:
            try:
                if re.search(str(raw), searchable, re.I): return True
            except re.error: pass
        return False


@dataclass(slots=True)
class Foreground:
    title: str = ""
    process: str = ""
    process_path: str = ""


def foreground_window() -> Foreground:
    if os.name != "nt":
        return Foreground()
    title = process = process_path = ""
    try:
        from ctypes import wintypes
        user32 = ctypes.windll.user32; kernel32 = ctypes.windll.kernel32
        hwnd = user32.GetForegroundWindow()
        if hwnd:
            length = user32.GetWindowTextLengthW(hwnd); buf = ctypes.create_unicode_buffer(max(1,length+1)); user32.GetWindowTextW(hwnd,buf,len(buf)); title=buf.value
            pid = wintypes.DWORD(0); user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                handle = kernel32.OpenProcess(0x1000, False, pid.value)
                if handle:
                    try:
                        size=wintypes.DWORD(32768); pbuf=ctypes.create_unicode_buffer(size.value)
                        if kernel32.QueryFullProcessImageNameW(handle,0,pbuf,ctypes.byref(size)):
                            process_path=pbuf.value; process=Path(process_path).name
                    finally: kernel32.CloseHandle(handle)
    except Exception: pass
    return Foreground(title.strip(), process.strip(), process_path.strip())


class GamePackManager:
    def __init__(self, cfg: dict[str, Any], capture_cfg: dict[str, Any], on_event: Callable[[GamePack, dict[str, Any]], None]):
        self.cfg = cfg; self.capture_cfg = capture_cfg; self.on_event = on_event
        raw_root = str(cfg.get("directory", "local_games") or "local_games"); root=Path(raw_root); self.root=root if root.is_absolute() else Path.cwd()/root
        self.packs: list[GamePack] = []; self.current: GamePack | None = None
        self._stop=threading.Event(); self._thread: threading.Thread|None=None; self._state_digest: dict[str,str]={}; self._last_ocr: dict[str,float]={}; self._rule_times: dict[tuple[str,int],float]={}; self._seen_state_events:set[str]=set()
        if bool(cfg.get("enabled",True)): self._load()

    def _load(self) -> None:
        if not self.root.exists():
            print(f"[game-pack] local directory not found (optional): {self.root}"); return
        active = {str(x).lower() for x in (self.cfg.get("active") or [])}
        for manifest_path in sorted(self.root.glob("*/game.yaml")):
            try: data=yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
            except Exception as exc: print(f"[game-pack] invalid {manifest_path}: {exc}"); continue
            if not isinstance(data,dict) or not bool(data.get("enabled",True)): continue
            pack=GamePack(manifest_path.parent,data)
            if active and pack.id not in active: continue
            self.packs.append(pack)
        if self.packs: print("[game-pack] loaded: "+", ".join(f"{p.name} ({p.id})" for p in self.packs))

    def start(self) -> None:
        if self.packs and self._thread is None:
            self._thread=threading.Thread(target=self._run,name="game-packs",daemon=True); self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread: self._thread.join(timeout=2)

    def context_id(self) -> str:
        return f"game:{self.current.id}" if self.current else "normal:desktop"

    def scenario_context(self) -> str:
        return self.current.corpus.context() if self.current else ""

    def present_context(self) -> str:
        if not self.current: return "デスクトップ"
        recent=self.current.corpus.context(); return recent or f"{self.current.name}をプレイ中"

    def _ocr(self) -> str:
        import cv2, mss, numpy as np, winocr
        monitor_index=max(1,int(self.capture_cfg.get("monitor",1)))
        with mss.mss() as sct:
            monitor=sct.monitors[min(monitor_index,len(sct.monitors)-1)]; shot=np.asarray(sct.grab(monitor)); frame=cv2.cvtColor(shot,cv2.COLOR_BGRA2BGR)
        width=frame.shape[1]
        if width<1600:
            scale=min(2.0,1600.0/max(1,width)); frame=cv2.resize(frame,None,fx=scale,fy=scale,interpolation=cv2.INTER_CUBIC)
        result=winocr.recognize_cv2_sync(frame,"ja")
        return str(result.get("text") if isinstance(result,dict) else getattr(result,"text","") or "").strip()

    @staticmethod
    def _chunks(text: str) -> list[str]:
        lines=[re.sub(r"\s+","",x) for x in text.splitlines()]; lines=[x for x in lines if len(x)>=4]; out=[]
        def add(x:str):
            if len(x)>=4 and x not in out: out.append(x)
        for x in lines: add(x)
        for size in (2,3):
            for i in range(max(0,len(lines)-size+1)): add("".join(lines[i:i+size]))
        add("".join(lines)); return out

    def _apply_rules(self, pack: GamePack, text: str) -> None:
        now=time.time()
        for index, rule in enumerate(pack.gameplay_rules):
            pattern=str(rule.get("pattern") or "")
            if not pattern: continue
            try: matched=re.search(pattern,text,re.I) is not None
            except re.error: continue
            if not matched: continue
            cooldown=max(0.5,float(rule.get("cooldown_seconds",8.0))); key=(pack.id,index)
            if now-self._rule_times.get(key,0.0)<cooldown: continue
            self._rule_times[key]=now
            event={"event_type":str(rule.get("event_type") or "gameplay"),"actor_scope":str(rule.get("actor_scope") or "user"),"summary":str(rule.get("summary") or f"画面上で {pattern} に該当する出来事を確認"),"importance":float(rule.get("importance",0.6)),"signals":dict(rule.get("signals") or {}),"observed_text":text[:500]}
            self.on_event(pack,event)

    def _handle_state(self, pack: GamePack) -> None:
        state_cfg=dict(pack.tracking_cfg); raw=str(state_cfg.get("state_file") or "runtime_state.json"); path=pack.resolve(raw)
        if not path.exists(): return
        try: data_bytes=path.read_bytes(); digest=hashlib.sha1(data_bytes).hexdigest()
        except OSError: return
        if self._state_digest.get(pack.id)==digest: return
        self._state_digest[pack.id]=digest
        try: state=json.loads(data_bytes.decode("utf-8-sig"))
        except Exception as exc: print(f"[game-pack:{pack.id}] invalid state file: {exc}"); return
        if not isinstance(state,dict): return
        scenario_key=str(state.get("scenario_key") or state.get("scene_id") or "")
        if scenario_key:
            item,is_new=pack.corpus.mark_key(scenario_key)
            if item and is_new:
                self.on_event(pack,{"event_type":"dialogue","actor_scope":"story_character","summary":f"{item.speaker or 'ナレーション'}: {item.text}","importance":0.45,"signals":{},"scenario_key":item.key})
        if state.get("text"):
            event_id=str(state.get("event_id") or hashlib.sha1(json.dumps(state,ensure_ascii=False,sort_keys=True).encode()).hexdigest())
            if event_id not in self._seen_state_events:
                self._seen_state_events.add(event_id)
                self.on_event(pack,{"event_type":"dialogue","actor_scope":"story_character","summary":f"{state.get('speaker') or 'ナレーション'}: {state.get('text')}","importance":float(state.get("importance",0.45)),"signals":{},"event_id":event_id})
        events=[]
        if isinstance(state.get("player_event"),dict): events.append(state["player_event"])
        if isinstance(state.get("events"),list): events.extend(x for x in state["events"] if isinstance(x,dict))
        for raw_event in events:
            event=dict(raw_event); event.setdefault("actor_scope","user"); event.setdefault("event_type","gameplay"); event.setdefault("importance",0.6); event.setdefault("signals",{}); event.setdefault("summary",event.get("event_type","gameplay"))
            event_id=str(event.get("event_id") or hashlib.sha1(json.dumps(event,ensure_ascii=False,sort_keys=True).encode()).hexdigest())
            if event_id in self._seen_state_events: continue
            self._seen_state_events.add(event_id); self.on_event(pack,event)

    def _handle_ocr(self, pack: GamePack) -> None:
        cfg=pack.tracking_cfg; interval=max(0.6,float(cfg.get("interval_seconds",0.9))); now=time.time()
        if now-self._last_ocr.get(pack.id,0.0)<interval: return
        self._last_ocr[pack.id]=now
        try: text=self._ocr()
        except Exception as exc:
            print(f"[game-pack:{pack.id}] OCR unavailable: {type(exc).__name__}: {exc}"); time.sleep(2); return
        if not text: return
        self._apply_rules(pack,text)
        if not pack.corpus.ready: return
        threshold=max(0.50,min(0.98,float(cfg.get("match_threshold",0.64)))); matches=[]
        for chunk in self._chunks(text):
            match=pack.corpus.best_match(chunk,threshold)
            if match: matches.append(match)
        if not matches: return
        best=max(matches,key=lambda x:x.confidence)
        if not pack.corpus.remember_match(best): return
        item=best.entry; print(f"[game-pack:{pack.id}] script {best.confidence:.2f} {item.speaker}: {item.text[:80]}")
        self.on_event(pack,{"event_type":"dialogue","actor_scope":"story_character","summary":f"{item.speaker or 'ナレーション'}: {item.text}","importance":min(0.72,0.40+best.confidence*0.25),"signals":{},"match_confidence":best.confidence,"scenario_key":item.key})

    def _run(self) -> None:
        while not self._stop.is_set():
            fg=foreground_window(); selected=next((p for p in self.packs if p.matches(fg.title,fg.process,fg.process_path)),None)
            if selected is not self.current:
                self.current=selected
                print(f"[game-pack] active={selected.name if selected else 'none'}")
            if selected:
                method=str(selected.tracking_cfg.get("method") or "state_file").lower()
                if method=="state_file": self._handle_state(selected)
                elif method=="screen_ocr": self._handle_ocr(selected)
            self._stop.wait(0.20)
