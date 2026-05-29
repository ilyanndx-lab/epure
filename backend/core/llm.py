import yaml
import ollama
from typing import Generator

class LLMEngine:
    def __init__(self, config_path: str = "config.yaml"):
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        self._model = cfg["model"]["name"]
        self._gen = cfg["generation"]

    def stream(self, messages: list[dict]) -> Generator[str, None, None]:
        for chunk in ollama.chat(
            model=self._model,
            messages=messages,
            stream=True,
            options={
                "temperature": self._gen["temperature"],
                "top_p": self._gen["top_p"],
                "num_predict": self._gen["max_tokens"],
            }
        ):
            content = chunk["message"]["content"]
            if content:
                yield content

    def generate(self, messages: list[dict]) -> str:
        response = ollama.chat(
            model=self._model,
            messages=messages,
            stream=False,
            options={
                "temperature": self._gen["temperature"],
                "top_p": self._gen["top_p"],
                "num_predict": self._gen["max_tokens"],
            }
        )
        return response["message"]["content"]