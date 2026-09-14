from __future__ import annotations

import gc
import hashlib
import inspect
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any

PREFIX = "@@IRODORI_COMPANION@@"


def emit(payload: dict[str, Any]) -> None:
    print(PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


def supported(callable_obj, kwargs: dict[str, Any]) -> dict[str, Any]:
    try:
        sig = inspect.signature(callable_obj)
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
            return dict(kwargs)
        return {k:v for k,v in kwargs.items() if k in sig.parameters}
    except Exception:
        return dict(kwargs)


class Runtime:
    def __init__(self):
        self.runtime = None; self.repo_dir: Path|None = None; self.checkpoint = ""; self.codec_repo = "Aratako/Semantic-DACVAE-Japanese-32dim"
        self.device = "cuda"; self.ref_cache_enabled = True; self.ref_cache_dir: Path|None = None; self.memory: dict[str,str] = {}; self.supports_ref_latent = False

    def initialize(self, req: dict[str,Any]) -> dict[str,Any]:
        started = time.perf_counter(); self.repo_dir = Path(str(req["irodori_dir"])).expanduser().resolve()
        if str(self.repo_dir) not in sys.path: sys.path.insert(0,str(self.repo_dir))
        from irodori_tts import inference_runtime as ir
        hf = str(req.get("hf_checkpoint") or "Aratako/Irodori-TTS-v4-Small"); self.codec_repo = str(req.get("codec_repo") or self.codec_repo)
        downloader = getattr(ir,"download_hf_checkpoint",None)
        if downloader is not None: self.checkpoint = str(downloader(hf))
        else:
            from huggingface_hub import hf_hub_download
            self.checkpoint = str(hf_hub_download(repo_id=hf, filename="model.safetensors"))
        self.device = str(req.get("device") or "").strip() or str(getattr(ir,"default_runtime_device",lambda:"cuda")())
        RuntimeKey = ir.RuntimeKey
        key = RuntimeKey(**supported(RuntimeKey, {"checkpoint":self.checkpoint,"model_device":self.device,"codec_repo":self.codec_repo,"model_precision":str(req.get("model_precision") or "bf16"),"codec_device":self.device,"codec_precision":str(req.get("codec_precision") or "bf16"),"codec_deterministic_encode":True,"codec_deterministic_decode":True}))
        self.runtime = ir.InferenceRuntime.from_key(key)
        self.supports_ref_latent = "ref_latent" in getattr(ir.SamplingRequest,"__dataclass_fields__",{})
        self.ref_cache_enabled = bool(req.get("ref_latent_cache",True)); self.ref_cache_dir = Path(str(req.get("ref_cache_dir") or ".irodori_ref_cache")).resolve(); self.ref_cache_dir.mkdir(parents=True,exist_ok=True)
        try:
            import torch
            cuda = bool(torch.cuda.is_available())
        except Exception: cuda = False
        return {"ok":True,"op":"ready","load_seconds":round(time.perf_counter()-started,3),"device":self.device,"cuda_available":cuda}

    def _latent(self, path_text: str) -> str:
        if not self.ref_cache_enabled or not self.supports_ref_latent or self.runtime is None or self.ref_cache_dir is None: return ""
        path = Path(path_text).expanduser().resolve(); stat = path.stat(); key = hashlib.sha1(f"{path}|{stat.st_size}|{stat.st_mtime_ns}|{self.checkpoint}".encode()).hexdigest()[:24]
        if key in self.memory and Path(self.memory[key]).exists(): return self.memory[key]
        dest = self.ref_cache_dir / f"{key}.pt"
        if dest.exists(): self.memory[key]=str(dest); return str(dest)
        try:
            from irodori_tts import inference_runtime as ir
            import torch
            loader = getattr(ir,"_load_audio",None)
            if loader is None: return ""
            wav,sr = loader(str(path)); latent = self.runtime.codec.encode_waveform(wav.unsqueeze(0),sample_rate=int(sr),normalize_db=-16.0,ensure_max=True).cpu()
            if latent.ndim>=3 and latent.shape[0]==1: latent=latent.squeeze(0)
            torch.save(latent,dest); self.memory[key]=str(dest); return str(dest)
        except Exception: return ""

    def synthesize(self, req: dict[str,Any]) -> dict[str,Any]:
        if self.runtime is None: raise RuntimeError("Irodori is not initialized")
        from irodori_tts import inference_runtime as ir
        started=time.perf_counter(); text=str(req.get("text") or "").strip(); caption=str(req.get("caption") or "").strip(); refs=[str(Path(x).expanduser().resolve()) for x in (req.get("refs") or []) if str(x).strip()]
        if not text: raise ValueError("text is empty")
        kwargs={"text":text,"caption":caption or None,"num_steps":max(1,int(req.get("num_steps",6))),"t_schedule_mode":str(req.get("t_schedule_mode") or "sway"),"sway_coeff":float(req.get("sway_coeff",-1.0))}
        latents=[self._latent(x) for x in refs]
        if refs and self.supports_ref_latent and all(latents):
            kwargs["ref_latent" if len(latents)==1 else "ref_latents"] = latents[0] if len(latents)==1 else latents
        elif refs: kwargs["ref_wav" if len(refs)==1 else "ref_wavs"] = refs[0] if len(refs)==1 else refs
        else: kwargs["no_ref"] = True
        request=ir.SamplingRequest(**supported(ir.SamplingRequest,kwargs)); result=self.runtime.synthesize(request,log_fn=None)
        out=Path(str(req["output_wav"])).resolve(); out.parent.mkdir(parents=True,exist_ok=True); ir.save_wav(out,result.audio,result.sample_rate)
        return {"ok":True,"op":"synthesize","output_wav":str(out),"seconds":round(time.perf_counter()-started,3)}

    def close(self):
        self.runtime=None; gc.collect()
        try:
            import torch
            if torch.cuda.is_available(): torch.cuda.empty_cache()
        except Exception: pass


def main() -> int:
    runtime=Runtime()
    for raw in sys.stdin:
        raw=raw.strip()
        if not raw: continue
        try:
            req=json.loads(raw); op=str(req.get("op") or "")
            if op=="init": emit(runtime.initialize(req))
            elif op=="synthesize": emit(runtime.synthesize(req))
            elif op=="shutdown": emit({"ok":True,"op":"shutdown"}); break
            else: emit({"ok":False,"error":f"unknown op: {op}"})
        except KeyboardInterrupt: break
        except Exception as exc:
            traceback.print_exc(file=sys.stdout); sys.stdout.flush(); emit({"ok":False,"error":f"{type(exc).__name__}: {exc}"})
    runtime.close(); return 0


if __name__ == "__main__": raise SystemExit(main())
