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
        """Texte → WAV en mémoire.

        ``synthesize_wav`` et pas ``synthesize`` : depuis piper-tts 1.3,
        ``PiperVoice.synthesize(text, wav)`` n'existe plus sous cette forme.
        ``synthesize`` est devenu un **générateur** d'``AudioChunk`` dont le 2e
        paramètre est une ``SynthesisConfig``. L'ancien appel ne levait donc
        rien : il fabriquait un générateur jamais consommé, n'écrivait pas une
        trame, et c'est ``wave.close()`` qui finissait par lever « # channels
        not specified » — une erreur qui ne dit pas un mot de la vraie cause.
        Résultat mesuré sur piper-tts 1.4.2 : ``/voice/synthesize`` en 500 avec
        un modèle parfaitement chargé.

        Le format WAV est posé ici, et ``set_wav_format=False`` le confirme à
        piper : lui ne le pose qu'au PREMIER chunk audio, si bien qu'un texte
        n'en produisant aucun (blancs, ponctuation seule, emoji) laisserait
        l'en-tête incomplet et rejouerait exactement la même erreur pour une
        entrée bénigne. Un WAV vide est une réponse valide ; un 500 non.
        Les valeurs viennent de la même source que piper : 1 canal, 16 bits,
        échantillonnage déclaré par le modèle.
        """
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(self._piper_voice.config.sample_rate)
            self._piper_voice.synthesize_wav(text, wav, set_wav_format=False)
        return buf.getvalue()
