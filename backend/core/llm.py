import logging
import os
import time
from pathlib import Path
from typing import Generator, Optional

import ollama
import yaml
from dotenv import load_dotenv

# Loaded once at import time; reload_dotenv() refreshes after .env update
_ENV_FILE = Path(__file__).parent.parent / ".env"
load_dotenv(_ENV_FILE)

logger = logging.getLogger(__name__)


def _gemini_contents(messages: list[dict]) -> tuple[str, list[dict]]:
    """Split messages into (system_instruction, contents) for Gemini API."""
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
    def _is_gemini(model: str) -> bool:
        return model.startswith("gemini:")

    @staticmethod
    def _gemini_model_name(model: str) -> str:
        return model.split("gemini:", 1)[1]

    def stream(self, messages: list[dict], model: Optional[str] = None) -> Generator:
        m = model or self._model
        if self._is_gemini(m):
            yield from self._stream_gemini(messages, m)
        else:
            yield from self._stream_ollama(messages, m)

    def _stream_ollama(self, messages: list[dict], model: str) -> Generator:
        for chunk in ollama.chat(
            model=model,
            messages=messages,
            stream=True,
            options={
                "temperature": self._gen["temperature"],
                "top_p": self._gen["top_p"],
                "num_predict": self._gen["max_tokens"],
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

    def _stream_gemini(self, messages: list[dict], model: str) -> Generator:
        try:
            import google.generativeai as genai
        except ImportError:
            logger.error("google-generativeai non installé")
            yield "[Erreur: google-generativeai non installé]"
            return

        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            yield "[Erreur: GEMINI_API_KEY non configurée. Ajoutez la clé dans Settings.]"
            return

        try:
            genai.configure(api_key=api_key)
            model_name = self._gemini_model_name(model)
            sys_instr, contents = _gemini_contents(messages)

            kwargs: dict = {}
            if sys_instr:
                kwargs["system_instruction"] = sys_instr
            gen_model = genai.GenerativeModel(model_name, **kwargs)

            response = gen_model.generate_content(
                contents,
                stream=True,
                generation_config=genai.types.GenerationConfig(
                    temperature=self._gen["temperature"],
                    max_output_tokens=self._gen["max_tokens"],
                ),
            )
            stream_start = time.time()
            for chunk in response:
                if chunk.text:
                    yield chunk.text
            # usage_metadata is available after the stream is fully consumed
            try:
                meta = response.usage_metadata
                yield {
                    "__stats__": True,
                    "prompt_tokens": meta.prompt_token_count or 0,
                    "output_tokens": meta.candidates_token_count or 0,
                    "eval_duration_ns": int((time.time() - stream_start) * 1e9),
                    "prompt_duration_ns": 0,
                }
            except Exception:
                pass
        except Exception:
            logger.exception("Erreur streaming Gemini")
            yield "[Erreur Gemini — vérifiez la clé API et le nom du modèle]"

    def generate(self, messages: list[dict], model: Optional[str] = None) -> str:
        m = model or self._model
        if self._is_gemini(m):
            return self._generate_gemini(messages, m)
        return self._generate_ollama(messages, m)

    def _generate_ollama(self, messages: list[dict], model: str) -> str:
        response = ollama.chat(
            model=model,
            messages=messages,
            stream=False,
            options={
                "temperature": self._gen["temperature"],
                "top_p": self._gen["top_p"],
                "num_predict": self._gen["max_tokens"],
                "num_thread": 8,
            },
        )
        return response["message"]["content"]

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
            model_name = self._gemini_model_name(model)
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

    def reload_dotenv(self) -> None:
        """Reload GEMINI_API_KEY from .env after an update."""
        load_dotenv(_ENV_FILE, override=True)
