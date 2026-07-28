from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        cleaned = line.strip()
        if not cleaned or cleaned.startswith("#") or "=" not in cleaned:
            continue
        key, value = cleaned.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass
class AIProviderConfig:
    provider: str = "local"
    model: str = "local-heuristic"
    timeout: int = 60


class AIProvider:
    def __init__(self, app_root: Path, model: str | None = None, provider: str | None = None) -> None:
        load_dotenv(app_root / ".env")
        provider = (provider or os.getenv("AI_PROVIDER", "local")).strip().lower()
        selected_model = (model or os.getenv("AI_MODEL", "")).strip()
        self.config = AIProviderConfig(
            provider=provider,
            model=selected_model or self._default_model(provider),
            timeout=max(10, int(os.getenv("AI_TIMEOUT_SECONDS", "60") or "60")),
        )

    def available(self) -> bool:
        return bool(self._api_key())

    def analyze_json(self, *, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        if not self.available() or self.config.provider in {"local", "none", ""}:
            raise RuntimeError("AI provider is not configured.")
        content = self._call(system_prompt=system_prompt, user_payload=user_payload)
        return repair_json(content)

    def _default_model(self, provider: str) -> str:
        if provider == "openai":
            return "gpt-4.1-mini"
        if provider == "gemini":
            return "gemini-1.5-flash"
        if provider == "openrouter":
            return "openai/gpt-4.1-mini"
        if provider == "claude":
            return "claude-3-5-sonnet-latest"
        return "local-heuristic"

    def _api_key(self) -> str:
        provider = self.config.provider
        if provider == "openai":
            return os.getenv("OPENAI_API_KEY", "")
        if provider == "gemini":
            return os.getenv("GEMINI_API_KEY", "")
        if provider == "openrouter":
            return os.getenv("OPENROUTER_API_KEY", "")
        if provider == "claude":
            return os.getenv("ANTHROPIC_API_KEY", "")
        return ""

    def _call(self, *, system_prompt: str, user_payload: dict[str, Any]) -> str:
        provider = self.config.provider
        if provider == "gemini":
            return self._call_gemini(system_prompt, user_payload)
        if provider == "openrouter":
            return self._call_openai_compatible(
                "https://openrouter.ai/api/v1/chat/completions",
                self._api_key(),
                system_prompt,
                user_payload,
                extra_headers={"HTTP-Referer": "http://127.0.0.1", "X-Title": "Video Variant Studio"},
            )
        if provider == "openai":
            return self._call_openai_compatible(
                "https://api.openai.com/v1/chat/completions",
                self._api_key(),
                system_prompt,
                user_payload,
            )
        if provider == "claude":
            return self._call_claude(system_prompt, user_payload)
        raise RuntimeError(f"Unsupported AI provider: {provider}")

    def _request_json(self, url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"AI provider HTTP {exc.code}: {detail[:1200]}") from exc

    def _call_openai_compatible(
        self,
        url: str,
        api_key: str,
        system_prompt: str,
        user_payload: dict[str, Any],
        extra_headers: dict[str, str] | None = None,
    ) -> str:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        headers.update(extra_headers or {})
        payload = {
            "model": self.config.model,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
        }
        response = self._request_json(url, headers, payload)
        return response["choices"][0]["message"]["content"]

    def _call_gemini(self, system_prompt: str, user_payload: dict[str, Any]) -> str:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.config.model}:generateContent?key={self._api_key()}"
        )
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": json.dumps(user_payload, ensure_ascii=False)}]}],
            "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
        }
        response = self._request_json(url, {"Content-Type": "application/json"}, payload)
        parts = response["candidates"][0]["content"]["parts"]
        return "".join(part.get("text", "") for part in parts)

    def _call_claude(self, system_prompt: str, user_payload: dict[str, Any]) -> str:
        headers = {
            "x-api-key": self._api_key(),
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.config.model,
            "max_tokens": 4096,
            "temperature": 0.2,
            "system": system_prompt,
            "messages": [{"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)}],
        }
        response = self._request_json("https://api.anthropic.com/v1/messages", headers, payload)
        return "".join(part.get("text", "") for part in response.get("content", []) if part.get("type") == "text")


def repair_json(content: str) -> dict[str, Any]:
    cleaned = (content or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.S)
        if match:
            return json.loads(match.group(0))
        raise
