import io
import logging
import os
import tempfile
import urllib.request
import wave
from pathlib import Path

logger = logging.getLogger(__name__)


class WhisperEngine:
    def __init__(self, model_size: str = "small", language: str = "fr"):
        from faster_whisper import WhisperModel
        logger.info("Chargement du modèle Whisper : %s", model_size)
        self._model = WhisperModel(model_size, device="cpu", compute_type="int8")
        self._language = language
        logger.info("Modèle Whisper prêt")

    def transcribe(self, audio_bytes: bytes) -> str:
        with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name
        try:
            segments, _info = self._model.transcribe(
                tmp_path,
                language=self._language,
                beam_size=5,
            )
            return " ".join(seg.text.strip() for seg in segments).strip()
        except Exception:
            logger.exception("Erreur transcription Whisper")
            raise
        finally:
            os.unlink(tmp_path)


class PiperEngine:
    _VOICES_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"

    def __init__(self, voice: str = "fr_FR-upmc-medium", models_dir: str = "piper_models"):
        self._voice = voice
        self._models_dir = Path(models_dir)
        self._models_dir.mkdir(exist_ok=True)
        self._onnx = self._models_dir / f"{voice}.onnx"
        self._config = self._models_dir / f"{voice}.onnx.json"
        self._ensure_model()
        self._piper_voice = self._load()
        logger.info("Modèle Piper prêt : %s", voice)

    def _url_for(self, filename: str) -> str:
        # fr_FR-upmc-medium → lang=fr, lang_country=fr_FR, name=upmc, quality=medium
        parts = self._voice.split("-")
        lang_country = parts[0]
        lang = lang_country.split("_")[0]
        name = parts[1]
        quality = parts[2]
        return f"{self._VOICES_BASE}/{lang}/{lang_country}/{name}/{quality}/{filename}"

    def _ensure_model(self) -> None:
        for path, filename in [
            (self._onnx, f"{self._voice}.onnx"),
            (self._config, f"{self._voice}.onnx.json"),
        ]:
            if not path.exists():
                url = self._url_for(filename)
                logger.info("Téléchargement modèle Piper : %s", url)
                try:
                    urllib.request.urlretrieve(url, path)
                except Exception:
                    logger.exception("Échec téléchargement %s", url)
                    raise

    def _load(self):
        from piper.voice import PiperVoice
        return PiperVoice.load(str(self._onnx), config_path=str(self._config))

    def synthesize(self, text: str) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            self._piper_voice.synthesize(text, wav)
        return buf.getvalue()
