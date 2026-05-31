import logging
import os
import time
from pathlib import Path
from typing import Generator, Optional

import ollama
import yaml
from dotenv import load_dotenv

_ENV_FILE = Path(__file__).parent.parent / ".env"
load_dotenv(_ENV_FILE)

logger = logging.getLogger(__name__)

# OpenAI-compatible providers: name → (base_url, env_key | None)
# env_key=None means no API key required (local server)
_OPENAI_COMPAT: dict[str, tuple[str, str | None]] = {
    "groq":     ("https://api.groq.com/openai/v1",      "GROQ_API_KEY"),
    "cerebras": ("https://api.cerebras.ai/v1",          "CEREBRAS_API_KEY"),
    "deepseek": ("https://api.deepseek.com",            "DEEPSEEK_API_KEY"),
    "nvidia":   ("https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY"),
    "flm":      ("http://localhost:11435/v1",           None),
}


def _gemini_contents(messages: list[dict]) -> tuple[str, list[dict]]:
    system_parts: list[str] = []
    contents: list[dict] = []
    for msg in messages:
        role = msg.get("role", "user")
        text = msg.get("content", "")
        if role == "system":
            system_parts.append(text)
        elif role == "assistant":
            contents.append({"role": "model", "parts": [{"text": text}]})
        else:
            contents.append({"role": "user", "parts": [{"text": text}]})
    if not contents:
        contents = [{"role": "user", "parts": [{"text": "(vide)"}]}]
    return "\n\n".join(system_parts), contents


class LLMEngine:
    def __init__(self, config_path: str = "config.yaml"):
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        self._model = cfg["model"]["name"]
        self._gen = cfg["generation"]

    @staticmethod
    def _parse_model(model: str) -> tuple[str, str]:
        """'provider:model_id' → (provider, model_id).  Falls back to ('ollama', model)."""
        if ":" in model:
            prefix, rest = model.split(":", 1)
            if prefix in _OPENAI_COMPAT or prefix == "gemini":
                return prefix, rest
        return "ollama", model

    def _openai_client(self, provider: str):
        base_url, key_name = _OPENAI_COMPAT[provider]
        if key_name is None:
            api_key = "not-needed"  # local server, no auth required
        else:
            api_key = os.environ.get(key_name, "").strip()
            if not api_key:
                raise ValueError(f"{key_name} non configurée — ajoutez-la dans Settings")
        try:
            from openai import OpenAI
            return OpenAI(base_url=base_url, api_key=api_key)
        except ImportError:
            raise RuntimeError("Package 'openai' non installé — pip install openai")

    # ── Public API ───────────────────────────────────────────────────────────

    def stream(self, messages: list[dict], model: Optional[str] = None, max_tokens: Optional[int] = None) -> Generator:
        m = model or self._model
        provider, model_id = self._parse_model(m)
        mt = max_tokens or self._gen["max_tokens"]
        if provider == "gemini":
            yield from self._stream_gemini(messages, m, mt)
        elif provider in _OPENAI_COMPAT:
            client = self._openai_client(provider)  # raises if key missing
            yield from self._stream_openai(messages, model_id, client, provider, mt)
        else:
            yield from self._stream_ollama(messages, m, mt)

    def generate(self, messages: list[dict], model: Optional[str] = None) -> str:
        m = model or self._model
        provider, model_id = self._parse_model(m)
        if provider == "gemini":
            return self._generate_gemini(messages, m)
        elif provider in _OPENAI_COMPAT:
            client = self._openai_client(provider)
            return self._generate_openai(messages, model_id, client, provider)
        return self._generate_ollama(messages, m)

    def reload_dotenv(self) -> None:
        load_dotenv(_ENV_FILE, override=True)

    # ── Ollama ───────────────────────────────────────────────────────────────

    def _stream_ollama(self, messages: list[dict], model: str, max_tokens: Optional[int] = None) -> Generator:
        for chunk in ollama.chat(
            model=model, messages=messages, stream=True,
            options={
                "temperature": self._gen["temperature"],
                "top_p": self._gen["top_p"],
                "num_predict": max_tokens or self._gen["max_tokens"],
                "num_thread": 8,
            },
        ):
            content = chunk["message"]["content"]
            if content:
                yield content
            try:
                if chunk["done"]:
                    yield {
                        "__stats__": True,
                        "prompt_tokens": chunk["prompt_eval_count"] or 0,
                        "output_tokens": chunk["eval_count"] or 0,
                        "eval_duration_ns": chunk["eval_duration"] or 0,
                        "prompt_duration_ns": chunk["prompt_eval_duration"] or 0,
                    }
            except Exception:
                pass

    def _generate_ollama(self, messages: list[dict], model: str) -> str:
        response = ollama.chat(
            model=model, messages=messages, stream=False,
            options={
                "temperature": self._gen["temperature"],
                "top_p": self._gen["top_p"],
                "num_predict": self._gen["max_tokens"],
                "num_thread": 8,
            },
        )
        return response["message"]["content"]

    # ── Gemini ───────────────────────────────────────────────────────────────

    def _stream_gemini(self, messages: list[dict], model: str, max_tokens: Optional[int] = None) -> Generator:
        try:
            import google.generativeai as genai
        except ImportError:
            raise RuntimeError("Package 'google-generativeai' non installé")

        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise ValueError("GEMINI_API_KEY non configurée — ajoutez-la dans Settings")

        genai.configure(api_key=api_key)
        model_name = model.split("gemini:", 1)[1]
        sys_instr, contents = _gemini_contents(messages)

        kwargs: dict = {}
        if sys_instr:
            kwargs["system_instruction"] = sys_instr
        gen_model = genai.GenerativeModel(model_name, **kwargs)
        response = gen_model.generate_content(
            contents, stream=True,
            generation_config=genai.types.GenerationConfig(
                temperature=self._gen["temperature"],
                max_output_tokens=max_tokens or self._gen["max_tokens"],
            ),
        )

        stream_start = time.time()
        for chunk in response:
            if chunk.text:
                yield chunk.text
        stream_end = time.time()  # capture après la boucle complète

        try:
            meta = response.usage_metadata
            yield {
                "__stats__": True,
                "prompt_tokens": meta.prompt_token_count or 0,
                "output_tokens": meta.candidates_token_count or 0,
                "eval_duration_ns": int((stream_end - stream_start) * 1e9),
                "prompt_duration_ns": 0,
            }
        except Exception:
            pass

    def _generate_gemini(self, messages: list[dict], model: str) -> str:
        try:
            import google.generativeai as genai
        except ImportError:
            return "[Erreur: google-generativeai non installé]"
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            return "[Erreur: GEMINI_API_KEY non configurée]"
        try:
            genai.configure(api_key=api_key)
            model_name = model.split("gemini:", 1)[1]
            sys_instr, contents = _gemini_contents(messages)
            kwargs: dict = {}
            if sys_instr:
                kwargs["system_instruction"] = sys_instr
            gen_model = genai.GenerativeModel(model_name, **kwargs)
            response = gen_model.generate_content(
                contents,
                generation_config=genai.types.GenerationConfig(
                    temperature=self._gen["temperature"],
                    max_output_tokens=self._gen["max_tokens"],
                ),
            )
            return response.text
        except Exception:
            logger.exception("Erreur generate Gemini")
            return "[Erreur Gemini]"

    # ── OpenAI-compatible providers ──────────────────────────────────────────

    def _stream_openai(self, messages: list[dict], model_id: str, client, provider: str = "", max_tokens: Optional[int] = None) -> Generator:
        oai = [{"role": m["role"], "content": m["content"]} for m in messages]
        stream_start = time.time()
        prompt_tokens = 0
        output_tokens = 0
        mt = max_tokens or self._gen["max_tokens"]

        try:
            stream = client.chat.completions.create(
                model=model_id, messages=oai, stream=True,
                stream_options={"include_usage": True},
                temperature=self._gen["temperature"],
                max_tokens=mt,
            )
        except Exception:
            # Provider doesn't support stream_options — retry without
            stream = client.chat.completions.create(
                model=model_id, messages=oai, stream=True,
                temperature=self._gen["temperature"],
                max_tokens=mt,
            )

        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
            if getattr(chunk, "usage", None):
                prompt_tokens = getattr(chunk.usage, "prompt_tokens", 0) or 0
                output_tokens = getattr(chunk.usage, "completion_tokens", 0) or 0

        yield {
            "__stats__": True,
            "prompt_tokens": prompt_tokens,
            "output_tokens": output_tokens,
            "eval_duration_ns": int((time.time() - stream_start) * 1e9),
            "prompt_duration_ns": 0,
        }

    def _generate_openai(self, messages: list[dict], model_id: str, client, provider: str = "") -> str:
        oai = [{"role": m["role"], "content": m["content"]} for m in messages]
        try:
            response = client.chat.completions.create(
                model=model_id, messages=oai, stream=False,
                temperature=self._gen["temperature"],
                max_tokens=self._gen["max_tokens"],
            )
            return response.choices[0].message.content or ""
        except Exception:
            logger.exception("Erreur generate %s", provider)
            return f"[Erreur {provider}]"
