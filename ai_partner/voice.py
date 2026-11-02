from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

import requests

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
                env = os.environ.copy()
                env["PYTHONIOENCODING"] = "utf-8"
                env["PYTHONUTF8"] = "1"
                return [str(candidate), str(worker)], env
        uv = str(self.cfg.get("irodori_uv", "") or "").strip() or shutil.which("uv")
        if not uv:
            raise RuntimeError("Irodori Python/uv not found. Prepare Irodori-TTS first.")
        env = os.environ.copy()
        env.pop("VIRTUAL_ENV", None)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["UV_PROJECT_ENVIRONMENT"] = str((repo / env_name).resolve())
        env["UV_LINK_MODE"] = "copy"
        return [str(uv), "run", "--no-sync", "python", str(worker)], env

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.proc is None or self.proc.stdin is None or self.proc.stdout is None:
            raise RuntimeError("Irodori worker is not running")
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=True) + "\n")
        self.proc.stdin.flush()
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
            self.proc = subprocess.Popen(
                cmd,
                cwd=str(repo),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            cache = Path(str(self.cfg.get("irodori_ref_cache_dir", ".irodori_companion_ref_cache")))
            response = self._request(
                {
                    "op": "init",
                    "irodori_dir": str(repo),
                    "hf_checkpoint": str(self.cfg.get("irodori_checkpoint", "Aratako/Irodori-TTS-v4-Small")),
                    "codec_repo": str(self.cfg.get("irodori_codec_repo", "Aratako/Semantic-DACVAE-Japanese-32dim")),
                    "device": str(self.cfg.get("irodori_device", "")),
                    "model_precision": str(self.cfg.get("irodori_model_precision", "bf16")),
                    "codec_precision": str(self.cfg.get("irodori_codec_precision", "bf16")),
                    "ref_latent_cache": bool(self.cfg.get("irodori_ref_latent_cache", True)),
                    "ref_cache_dir": str(cache.resolve()),
                }
            )
            if not response.get("ok"):
                raise RuntimeError(str(response.get("error") or "Irodori init failed"))
            self.available = True
            print(
                f"[Irodori] ready device={response.get('device')} "
                f"cuda={'yes' if response.get('cuda_available') else 'no'} "
                f"load={float(response.get('load_seconds', 0)):.2f}s"
            )
            return True
        except Exception as exc:
            print(f"[Irodori] startup failed: {exc}")
            self.stop()
            return False

    def synthesize(self, text: str, caption: str, output: Path) -> tuple[bool, str]:
        if not self.available and not self.start():
            return False, "Irodori unavailable"
        payload = {
            "op": "synthesize",
            "text": text,
            "caption": caption,
            "refs": [str(x) for x in _refs(self.cfg.get("irodori_reference_audio", ""))],
            "output_wav": str(output.resolve()),
            "num_steps": int(self.cfg.get("irodori_num_steps", 6)),
            "t_schedule_mode": str(self.cfg.get("irodori_t_schedule_mode", "sway")),
            "sway_coeff": float(self.cfg.get("irodori_sway_coeff", -1.0)),
            "codec_device": str(self.cfg.get("irodori_codec_device", "")),
        }
        try:
            result = self._request(payload)
            return bool(result.get("ok")), str(result.get("error") or "")
        except Exception as exc:
            if bool(self.cfg.get("irodori_restart_on_crash", True)) and not self.restart_used:
                self.restart_used = True
                self.stop()
                if self.start():
                    try:
                        result = self._request(payload)
                        return bool(result.get("ok")), str(result.get("error") or "")
                    except Exception as retry:
                        return False, str(retry)
            return False, str(exc)

    def stop(self) -> None:
        proc = self.proc
        self.proc = None
        self.available = False
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


class VoicevoxSession:
    """VOICEVOX engine (http://127.0.0.1:50021). Runs on CPU, so it suits laptops.

    The VOICEVOX app starts the engine while it is open. Each emotion maps to a
    style of one speaker (by default 四国めたん, whose styles fit an ojousama).
    """

    DEFAULT_STYLES = {
        "neutral": "ノーマル",
        "happy": "あまあま",
        "excited": "あまあま",
        "surprise": "ツンツン",
        "angry": "ツンツン",
        "worry": "ノーマル",
        "sad": "ノーマル",
        "thinking": "ノーマル",
    }

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.url = str(cfg.get("voicevox_url", "http://127.0.0.1:50021") or "").rstrip("/")
        self.speaker_name = str(cfg.get("voicevox_speaker", "四国めたん") or "").strip()
        self.styles = dict(self.DEFAULT_STYLES)
        self.styles.update({str(k): str(v) for k, v in (cfg.get("voicevox_styles") or {}).items()})
        self.style_ids: dict[str, int] = {}
        for key, value in (cfg.get("voicevox_style_ids") or {}).items():
            try:
                self.style_ids[str(key)] = int(value)
            except (TypeError, ValueError):
                pass
        self.timeout = float(cfg.get("voicevox_timeout_seconds", 20))
        self.available = False

    def start(self, wait_seconds: float = 0.0) -> bool:
        deadline = time.time() + max(0.0, wait_seconds)
        while True:
            try:
                speakers = requests.get(f"{self.url}/speakers", timeout=3).json()
                self._resolve_styles(speakers)
                self.available = True
                shown = ", ".join(f"{k}={v}" for k, v in sorted(self.style_ids.items()))
                print(f"[VOICEVOX] ready speaker={self.speaker_name} styles: {shown}")
                return True
            except Exception as exc:
                if time.time() >= deadline:
                    print(f"[VOICEVOX] engine not reachable at {self.url}: {type(exc).__name__}. Open the VOICEVOX app.")
                    self.available = False
                    return False
                time.sleep(2.0)

    def _resolve_styles(self, speakers: list[dict[str, Any]]) -> None:
        chosen = None
        for speaker in speakers or []:
            if str(speaker.get("name", "")) == self.speaker_name:
                chosen = speaker
                break
        if chosen is None and speakers:
            chosen = speakers[0]
            print(f"[VOICEVOX] speaker '{self.speaker_name}' not found; using '{chosen.get('name')}'")
            self.speaker_name = str(chosen.get("name", ""))
        if chosen is None:
            raise RuntimeError("VOICEVOX returned no speakers")
        by_name = {str(st.get("name", "")): int(st.get("id")) for st in chosen.get("styles") or []}
        first = next(iter(by_name.values()))
        for emotion, style_name in self.styles.items():
            if emotion in self.style_ids:
                continue
            self.style_ids[emotion] = by_name.get(style_name, by_name.get("ノーマル", first))
        self.style_ids.setdefault("neutral", by_name.get("ノーマル", first))

    def synthesize(self, text: str, emotion: str, intensity: float, energy: float, output: Path) -> tuple[bool, str]:
        if not self.available and not self.start():
            return False, "VOICEVOX unavailable"
        style = self.style_ids.get(emotion, self.style_ids.get("neutral", 0))
        try:
            query = requests.post(
                f"{self.url}/audio_query",
                params={"text": text, "speaker": style},
                timeout=self.timeout,
            )
            query.raise_for_status()
            q = query.json()
            base_speed = float(self.cfg.get("voicevox_speed", 1.08))
            speed = base_speed + (max(0.0, min(1.0, energy)) - 0.5) * 0.24
            pitch = float(self.cfg.get("voicevox_pitch", 0.0))
            intonation = 1.0 + max(0.0, min(1.0, intensity)) * 0.45
            if emotion == "surprise":
                pitch += 0.04
                speed += 0.05
            elif emotion in {"sad", "worry"}:
                pitch -= 0.02
                speed -= 0.08
            elif emotion == "thinking":
                speed -= 0.06
            q["speedScale"] = max(0.7, min(1.5, speed))
            q["pitchScale"] = max(-0.15, min(0.15, pitch))
            q["intonationScale"] = max(0.6, min(1.8, intonation))
            q["volumeScale"] = float(self.cfg.get("voicevox_volume", 1.0))
            q["prePhonemeLength"] = 0.05
            q["postPhonemeLength"] = 0.08
            audio = requests.post(
                f"{self.url}/synthesis",
                params={"speaker": style},
                json=q,
                timeout=self.timeout,
            )
            audio.raise_for_status()
            output.write_bytes(audio.content)
            return True, ""
        except Exception as exc:
            self.available = False
            return False, f"{type(exc).__name__}: {exc}"

    def stop(self) -> None:
        self.available = False


class VoiceOutput:
    CAPTIONS = {
        "neutral": "自然な会話調で、落ち着いて",
        "happy": "嬉しそうに、明るく自然に",
        "surprise": "思わず驚いて、反射的に",
        "angry": "不満と苛立ちを込めつつ、自然な会話として",
        "worry": "相手を心配して、少し切迫感を込めて",
        "sad": "悲しさをにじませて、声を抑えめに",
        "thinking": "考え込みながら、少し間を意識して",
        "excited": "興奮して、テンポよく楽しげに",
    }

    def __init__(
        self,
        cfg: dict[str, Any],
        vnyan: VNyanBridge,
        on_start: Callable[[str], None] | None = None,
        on_end: Callable[[], None] | None = None,
    ):
        self.provider = str(cfg.get("provider", "irodori")).lower()
        if self.provider not in {"irodori", "voicevox"}:
            raise RuntimeError("voice.provider must be irodori or voicevox")

        self.cfg = cfg
        self.vnyan = vnyan
        self.on_start = on_start
        self.on_end = on_end
        self.q: queue.Queue[tuple[str, str, float, float] | None] = queue.Queue(maxsize=8)
        self.irodori: IrodoriSession | None = None
        self.voicevox: VoicevoxSession | None = None
        if self.provider == "voicevox":
            self.voicevox = VoicevoxSession(cfg)
            # The app may still be starting; keep waiting a little, then carry on and retry per line.
            self.voicevox.start(wait_seconds=float(cfg.get("voicevox_wait_seconds", 30)))
        else:
            self.irodori = IrodoriSession(cfg)
            if bool(self.cfg.get("irodori_preload", True)) and not self.irodori.start():
                raise RuntimeError(
                    "Irodori-TTS could not start. Check voice.irodori_dir and the Irodori-TTS setup."
                )

        self.thread = threading.Thread(target=self._worker, name="voice-output", daemon=True)
        self.thread.start()

    def speak(self, text: str, emotion: str = "neutral", intensity: float = 0.4, energy: float = 0.5) -> None:
        text = str(text or "").strip()
        if not text:
            return
        try:
            self.q.put_nowait((text, emotion, float(intensity), float(energy)))
        except queue.Full:
            pass

    def _caption(self, emotion: str, intensity: float, energy: float) -> str:
        base = self.CAPTIONS.get(emotion, self.CAPTIONS["neutral"])
        if intensity >= 0.8:
            base += "。今回の感情は強め"
        elif intensity <= 0.3:
            base += "。今回の感情は控えめ"
        if energy >= 0.75:
            base += "。会話の勢いはやや高め"
        return base

    def _worker(self) -> None:
        while True:
            item = self.q.get()
            if item is None:
                break
            text, emotion, intensity, energy = item
            try:
                if self.on_start:
                    self.on_start(text)
                self.vnyan.expression(emotion)
                print(f"[SAY] {text}")
                if self.voicevox is not None:
                    self._voicevox(text, emotion, intensity, energy)
                else:
                    self._irodori(text, self._caption(emotion, intensity, energy))
            except Exception as exc:
                print(f"[voice] {type(exc).__name__}: {exc}")
            finally:
                if self.vnyan.reset_after_speech:
                    self.vnyan.neutral()
                if self.on_end:
                    self.on_end()
        self._stop_engines()

    def _stop_engines(self) -> None:
        if self.irodori is not None:
            self.irodori.stop()
        if self.voicevox is not None:
            self.voicevox.stop()

    def _voicevox(self, text: str, emotion: str, intensity: float, energy: float) -> None:
        assert self.voicevox is not None
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = Path(f.name)
        try:
            ok, error = self.voicevox.synthesize(text, emotion, intensity, energy, path)
            if not ok:
                raise RuntimeError(error)
            self.vnyan.play_with_lipsync(path, self._play_wav)
        finally:
            path.unlink(missing_ok=True)

    def _irodori(self, text: str, caption: str) -> None:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = Path(f.name)
        path.unlink(missing_ok=True)
        try:
            assert self.irodori is not None
            ok, error = self.irodori.synthesize(text, caption, path)
            if not ok:
                raise RuntimeError(error)
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
        try:
            self.q.put(None, timeout=0.5)
        except queue.Full:
            pass
        self.thread.join(timeout=5)
        self._stop_engines()
