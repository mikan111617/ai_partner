from __future__ import annotations

import json
import re
from typing import Any

import requests

from .persona import persona_prompt


ALLOWED_SIGNALS = (
    "surprise", "danger", "kindness", "cruelty", "courage", "cowardice", "novelty", "repetition", "tension", "mystery",
    "boldness", "consistency", "inconsistency", "special_treatment", "sincerity", "active_behavior", "bland_safe_choice",
    "adventure", "learning", "victory", "player_defeated", "heavy_damage"
)


def _extract_json(text: str) -> dict[str, Any]:
    value = str(text or "").strip()
    value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.I)
    value = re.sub(r"\s*```$", "", value)
    start, end = value.find("{"), value.rfind("}")
    if start < 0 or end < start:
        raise ValueError(f"No JSON object in model response: {value[:240]}")
    return json.loads(value[start:end+1])


class OllamaLLM:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.base_url = str(cfg.get("url", "http://127.0.0.1:11434")).rstrip("/")
        self.timeout = float(cfg.get("timeout_seconds", 180))
        self.keep_alive = str(cfg.get("keep_alive", "10m"))

    def _chat(self, messages: list[dict[str, str]], *, model: str, temperature: float, num_ctx: int, num_gpu: int) -> dict[str, Any]:
        response = requests.post(
            f"{self.base_url}/api/chat",
            json={
                "model": model,
                "messages": messages,
                "stream": False,
                "format": "json",
                "think": False,
                "keep_alive": self.keep_alive,
                "options": {"temperature": temperature, "num_ctx": int(num_ctx), "num_gpu": int(num_gpu)},
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        content = str((response.json().get("message") or {}).get("content", ""))
        return _extract_json(content)

    @staticmethod
    def _mind_text(mind: dict[str, float]) -> str:
        keys = ("trust", "affection", "respect", "antipathy", "joy", "anger", "sadness", "fear", "disgust", "embarrassment", "excitement", "boredom", "arousal", "concern")
        return json.dumps({k: round(float(mind.get(k, 0.0)), 3) for k in keys}, ensure_ascii=False)

    def respond(self, *, user_text: str, history: str, event_context: str, mind: dict[str, float], mode: str) -> dict[str, Any]:
        system = f"""/no_think
{persona_prompt()}

【現在の心理状態】
{self._mind_text(mind)}

重要: arousal / excitement は基礎的な活性度・勢いです。怒り、悲しみ、敵意の強さではありません。
今回の local_emotion と local_intensity は、最新の発話と現在の出来事だけから独立して判断してください。
高い arousal を理由に、軽い不満を激怒へ、軽い悲しみを絶望へ増幅してはいけません。
軽いからかい・冗談・小さな失敗には、必要なら軽い反発や呆れ程度を優先してください。

会話相手の固有名・固定呼称は存在しません。名前を作ったり、以前の所有者の名前を呼んだりしないでください。
最新の発話へ直接返してください。過去の会話は文脈の解釈にだけ使い、別の過去話題を勝手に再開しないでください。

Allowed signals: {', '.join(ALLOWED_SIGNALS)}
Return JSON only:
{{
  "utterance": "自然な日本語1〜2文",
  "local_emotion": "neutral|happy|surprise|angry|worry|sad|thinking|excited",
  "local_intensity": 0.0,
  "summary": "今回ユーザーが伝えた内容の短い要約",
  "importance": 0.0,
  "signals": {{"supported_signal": 0.0}}
}}
"""
        messages = [{"role": "system", "content": system}]
        if history:
            messages.append({"role": "system", "content": history})
        if event_context:
            messages.append({"role": "system", "content": event_context})
        messages.append({"role": "user", "content": user_text})
        model = str(self.cfg.get("game_model") if mode == "game" else self.cfg.get("conversation_model"))
        num_ctx = int(self.cfg.get("game_num_ctx", 4096) if mode == "game" else self.cfg.get("normal_num_ctx", 8192))
        num_gpu = int(self.cfg.get("game_num_gpu", 0) if mode == "game" else self.cfg.get("normal_num_gpu", 22))
        data = self._chat(messages, model=model, temperature=0.45, num_ctx=num_ctx, num_gpu=num_gpu)
        data.setdefault("utterance", "")
        data.setdefault("local_emotion", "neutral")
        data.setdefault("local_intensity", 0.35)
        data.setdefault("signals", {})
        data.setdefault("importance", 0.5)
        return data

    def game_reaction(self, *, event: dict[str, Any], context: str, mind: dict[str, float]) -> dict[str, Any]:
        prompt = f"""/no_think
{persona_prompt()}
現在の心理状態: {self._mind_text(mind)}

ゲーム中に今起きた出来事:
{json.dumps(event, ensure_ascii=False)}
現在のゲーム状況:
{context}

まだ起きていない展開を推測・予告しないでください。今の瞬間への短い反応だけを返してください。
arousal は発話の勢いにだけ影響し、感情強度を自動増幅しません。
Return JSON only:
{{"utterance":"短い一言または空文字", "local_emotion":"neutral|happy|surprise|angry|worry|sad|thinking|excited", "local_intensity":0.0}}
"""
        model = str(self.cfg.get("game_model") or self.cfg.get("conversation_model"))
        data = self._chat([{"role":"user","content":prompt}], model=model, temperature=0.55, num_ctx=int(self.cfg.get("game_num_ctx",4096)), num_gpu=int(self.cfg.get("game_num_gpu",0)))
        data.setdefault("utterance", "")
        data.setdefault("local_emotion", "neutral")
        data.setdefault("local_intensity", 0.35)
        return data

    def ambient(self, *, reason: str, present_context: str, mind: dict[str, float]) -> dict[str, Any]:
        prompt = f"""/no_think
{persona_prompt()}
現在の心理状態: {self._mind_text(mind)}
自発発話の理由: {reason}
今この瞬間の状況: {present_context or '特に情報なし'}

これは会話相手が今話していない時の自発発話です。過去の会話履歴は与えられていません。
過去の質問・昔の話題を想像して再開してはいけません。今について話す理由が弱ければ utterance は空文字にしてください。
Return JSON only:
{{"utterance":"短い一言または空文字", "local_emotion":"neutral|happy|surprise|angry|worry|sad|thinking|excited", "local_intensity":0.0}}
"""
        data = self._chat([{"role":"user","content":prompt}], model=str(self.cfg.get("game_model") or self.cfg.get("conversation_model")), temperature=0.55, num_ctx=2048, num_gpu=int(self.cfg.get("game_num_gpu",0)))
        data.setdefault("utterance", "")
        data.setdefault("local_emotion", "neutral")
        data.setdefault("local_intensity", 0.3)
        return data
