from typing import Generator, Optional

import ollama
import yaml


class LLMEngine:
    def __init__(self, config_path: str = "config.yaml"):
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        self._model = cfg["model"]["name"]
        self._gen = cfg["generation"]

    def stream(
        self, messages: list[dict], model: Optional[str] = None
    ) -> Generator[str, None, None]:
        for chunk in ollama.chat(
            model=model or self._model,
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

    def generate(self, messages: list[dict], model: Optional[str] = None) -> str:
        response = ollama.chat(
            model=model or self._model,
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
