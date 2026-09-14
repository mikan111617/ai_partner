from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

from .vnyan import VNyanBridge


PREFIX = "@@IRODORI_COMPANION@@"


def _refs(value: Any) -> list[Path]:
    if value in (None, "", []):
        return []
    items = [value] if isinstance(value, (str, Path)) else list(value)
    return [Path(str(x)).expanduser().resolve() for x in items if str(x).strip()]


class IrodoriSession:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.proc: subprocess.Popen[str] | None = None
        self.available = False
        self.restart_used = False

    @property
    def repo_dir(self) -> Path:
        raw = str(self.cfg.get("irodori_dir", "") or "").strip()
        if not raw:
            raise RuntimeError("voice.irodori_dir is empty. Set it in config.user.yaml")
        return Path(raw).expanduser().resolve()

    def _launch(self, worker: Path) -> tuple[list[str], dict[str, str]]:
        repo = self.repo_dir
        explicit = str(self.cfg.get("irodori_python", "") or "").strip()
        env_name = str(self.cfg.get("irodori_uv_environment", ".venv-companion") or ".venv-companion")
        candidates = []
        if explicit:
            candidates.append(Path(explicit).expanduser().resolve())
        candidates.extend([repo / env_name / "Scripts" / "python.exe", repo / ".venv" / "Scripts" / "python.exe"])
        for candidate in candidates:
            if candidate.exists():
                env = os.environ.copy(); env["PYTHONIOENCODING"] = "utf-8"; env["PYTHONUTF8"] = "1"
                return [str(candidate), str(worker)], env
        uv = str(self.cfg.get("irodori_uv", "") or "").strip() or shutil.which("uv")
        if not uv:
            raise RuntimeError("Irodori Python/uv not found. Prepare Irodori-TTS first.")
        env = os.environ.copy(); env.pop("VIRTUAL_ENV", None); env["PYTHONIOENCODING"] = "utf-8"; env["PYTHONUTF8"] = "1"
        env["UV_PROJECT_ENVIRONMENT"] = str((repo / env_name).resolve())
        env["UV_LINK_MODE"] = "copy"
        return [str(uv), "run", "--no-sync", "python", str(worker)], env

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.proc is None or self.proc.stdin is None or self.proc.stdout is None:
            raise RuntimeError("Irodori worker is not running")
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=True)+"\n"); self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if line == "":
                raise RuntimeError(f"Irodori worker exited: {self.proc.poll()}")
            text = line.rstrip("\r\n")
            if text.startswith(PREFIX):
                return json.loads(text[len(PREFIX):])
            if text:
                print(f"[Irodori] {text}")

    def start(self) -> bool:
        if self.available and self.proc is not None and self.proc.poll() is None:
            return True
        try:
            repo = self.repo_dir
            if not (repo / "infer.py").exists():
                raise RuntimeError(f"Irodori infer.py not found under {repo}")
            worker = Path(__file__).with_name("irodori_worker.py").resolve()
            cmd, env = self._launch(worker)
            print("[Irodori] starting local worker; model will be loaded once...")
            self.proc = subprocess.Popen(cmd, cwd=str(repo), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1)
            cache = Path(str(self.cfg.get("irodori_ref_cache_dir", ".irodori_companion_ref_cache")))
            response = self._request({
                "op":"init", "irodori_dir":str(repo),
                "hf_checkpoint":str(self.cfg.get("irodori_checkpoint","Aratako/Irodori-TTS-v4-Small")),
                "codec_repo":str(self.cfg.get("irodori_codec_repo","Aratako/Semantic-DACVAE-Japanese-32dim")),
                "device":str(self.cfg.get("irodori_device","")),
                "model_precision":str(self.cfg.get("irodori_model_precision","bf16")),
                "codec_precision":str(self.cfg.get("irodori_codec_precision","bf16")),
                "ref_latent_cache":bool(self.cfg.get("irodori_ref_latent_cache",True)),
                "ref_cache_dir":str(cache.resolve()),
            })
            if not response.get("ok"):
                raise RuntimeError(str(response.get("error") or "Irodori init failed"))
            self.available = True
            print(f"[Irodori] ready device={response.get('device')} cuda={'yes' if response.get('cuda_available') else 'no'} load={float(response.get('load_seconds',0)):.2f}s")
            return True
        except Exception as exc:
            print(f"[Irodori] startup failed: {exc}")
            self.stop()
            return False

    def synthesize(self, text: str, caption: str, output: Path) -> tuple[bool, str]:
        if not self.available and not self.start():
            return False, "Irodori unavailable"
        payload = {
            "op":"synthesize", "text":text, "caption":caption,
            "refs":[str(x) for x in _refs(self.cfg.get("irodori_reference_audio",""))],
            "output_wav":str(output.resolve()),
            "num_steps":int(self.cfg.get("irodori_num_steps",6)),
            "t_schedule_mode":str(self.cfg.get("irodori_t_schedule_mode","sway")),
            "sway_coeff":float(self.cfg.get("irodori_sway_coeff",-1.0)),
            "codec_device":str(self.cfg.get("irodori_codec_device","")),
        }
        try:
            result = self._request(payload)
            return bool(result.get("ok")), str(result.get("error") or "")
        except Exception as exc:
            if bool(self.cfg.get("irodori_restart_on_crash",True)) and not self.restart_used:
                self.restart_used = True; self.stop()
                if self.start():
                    try:
                        result = self._request(payload)
                        return bool(result.get("ok")), str(result.get("error") or "")
                    except Exception as retry:
                        return False, str(retry)
            return False, str(exc)

    def stop(self) -> None:
        proc = self.proc; self.proc = None; self.available = False
        if proc is None: return
        try:
            if proc.poll() is None:
                proc.terminate(); proc.wait(timeout=2)
        except Exception:
            try: proc.kill()
            except Exception: pass


class VoiceOutput:
    CAPTIONS = {
        "neutral":"自然な会話調で、落ち着いて", "happy":"嬉しそうに、明るく自然に",
        "surprise":"思わず驚いて、反射的に", "angry":"不満と苛立ちを込めつつ、自然な会話として",
        "worry":"相手を心配して、少し切迫感を込めて", "sad":"悲しさをにじませて、声を抑えめに",
        "thinking":"考え込みながら、少し間を意識して", "excited":"興奮して、テンポよく楽しげに",
    }

    def __init__(self, cfg: dict[str, Any], vnyan: VNyanBridge, on_start: Callable[[str],None]|None=None, on_end: Callable[[],None]|None=None):
        self.cfg = cfg; self.provider = str(cfg.get("provider","console")).lower(); self.vnyan = vnyan
        self.on_start = on_start; self.on_end = on_end
        self.q: queue.Queue[tuple[str,str,float,float]|None] = queue.Queue(maxsize=8)
        self.irodori = IrodoriSession(cfg) if self.provider == "irodori" else None
        self.thread = threading.Thread(target=self._worker, name="voice-output", daemon=True); self.thread.start()

    def speak(self, text: str, emotion: str="neutral", intensity: float=0.4, energy: float=0.5) -> None:
        text = str(text or "").strip()
        if not text: return
        try: self.q.put_nowait((text, emotion, float(intensity), float(energy)))
        except queue.Full: pass

    def _caption(self, emotion: str, intensity: float, energy: float) -> str:
        base = self.CAPTIONS.get(emotion, self.CAPTIONS["neutral"])
        if intensity >= 0.8: base += "。今回の感情は強め"
        elif intensity <= 0.3: base += "。今回の感情は控えめ"
        if energy >= 0.75: base += "。会話の勢いはやや高め"
        return base

    def _worker(self) -> None:
        if self.irodori is not None and bool(self.cfg.get("irodori_preload",True)):
            self.irodori.start()
        while True:
            item = self.q.get()
            if item is None: break
            text, emotion, intensity, energy = item
            try:
                if self.on_start: self.on_start(text)
                self.vnyan.expression(emotion)
                print(f"[SAY] {text}")
                if self.provider == "irodori":
                    self._irodori(text, self._caption(emotion,intensity,energy))
                elif self.provider != "console":
                    print(f"[voice] unknown provider={self.provider}; console only")
            except Exception as exc:
                print(f"[voice] {type(exc).__name__}: {exc}")
            finally:
                if self.vnyan.reset_after_speech: self.vnyan.neutral()
                if self.on_end: self.on_end()
        if self.irodori is not None: self.irodori.stop()

    def _irodori(self, text: str, caption: str) -> None:
        assert self.irodori is not None
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = Path(f.name)
        path.unlink(missing_ok=True)
        try:
            ok, error = self.irodori.synthesize(text, caption, path)
            if not ok: raise RuntimeError(error)
            self.vnyan.play_with_lipsync(path, self._play_wav)
        finally:
            path.unlink(missing_ok=True)

    @staticmethod
    def _play_wav(path: Path) -> None:
        if sys.platform.startswith("win"):
            import winsound
            winsound.PlaySound(str(path), winsound.SND_FILENAME)
        else:
            print(f"[voice] generated WAV: {path}")

    def stop(self) -> None:
        try: self.q.put(None, timeout=0.5)
        except queue.Full: pass
        self.thread.join(timeout=5)
        if self.irodori is not None: self.irodori.stop()
