"""Moteurs vocaux : transcription (Whisper) et synthèse (Piper).

Le modèle Piper pèse 76 Mo et n'est **pas** versionné : il était suivi par git,
et `.git` en portait 71 Mo — payés par chaque clone, pour un binaire
reconstructible qui n'est pas du code. Il est désormais récupéré au premier
usage de la synthèse, puis vérifié par sha256.

Conséquence à ne pas perdre de vue : la synthèse peut être **indisponible**
(pas de réseau, amont déplacé, empreinte divergente). Épure est local-first et
la voix y est optionnelle — une voix absente doit produire un message clair et
rien d'autre. D'où :meth:`VoiceModelUnavailable`, un type dédié que l'endpoint
traduit en 503 au lieu d'un 500 opaque, et le fait que `PiperEngine` reste
derrière un `_LazyEngine` : rien n'est téléchargé au démarrage, donc une machine
hors ligne démarre normalement et tout le reste de l'app fonctionne.
"""

import hashlib
import io
import logging
import os
import tempfile
import threading
import urllib.request
import wave
from pathlib import Path

from core.paths import resolve_models_dir

logger = logging.getLogger(__name__)


class VoiceModelUnavailable(RuntimeError):
    """La voix est indisponible — paquet absent, modèle non récupérable.

    Type dédié — et non un `RuntimeError` nu — pour que les endpoints distinguent
    « la voix est indisponible, dis-le calmement » (503, message lisible, log en
    warning) d'une vraie panne (500, trace de pile). Sans cette distinction, une
    machine hors ligne renvoie un 500 qui se lit comme un bug d'Épure.

    Levé par les DEUX moteurs, et déclaré avant eux pour que ça se voie :
    `PiperEngine` pour la synthèse, `WhisperEngine` pour la transcription. La
    transcription ne le faisait pas — l'asymétrie a vécu jusqu'au 2026-08-13.
    """


class WhisperEngine:
    """Transcription vocale (faster-whisper), modèle chargé à la demande.

    Comme `PiperEngine`, construit au premier usage via le `_LazyEngine` de
    `core.runtime` — jamais au démarrage : `WhisperModel` télécharge son modèle
    depuis HuggingFace et son import tire `ctranslate2`.
    """

    def __init__(self, model_size: str = "small", language: str = "fr"):
        # L'import est DANS le try, exactement comme `PiperEngine._load` : sans
        # faster-whisper installé, la transcription est indisponible — un état
        # PRÉVU, pas un plantage. Il ne l'était pas ici, et l'asymétrie se voyait
        # à l'usage : une synthèse sans piper-tts répondait 503 avec un message
        # lisible, une transcription sans faster-whisper laissait remonter un
        # `ImportError` nu jusqu'au `except Exception` de l'endpoint — donc un
        # 500 « Erreur transcription » et une trace de pile complète dans les
        # logs pour une dépendance simplement absente.
        #
        # Ce n'est pas un cas théorique : `ctranslate2`, dont dépend
        # faster-whisper, ne publie aucune wheel `win_arm64` ni aucune sdist
        # (docs/remplacement-vectoriel.md, étape E). Sur Windows ARM64, ce paquet
        # est donc absent par construction, et c'est cette branche qui répond.
        #
        # Le chargement du modèle est dans le même try, pour la même raison que
        # chez Piper : hors ligne, un premier usage échoue à récupérer le modèle,
        # et « la voix n'est pas disponible » reste la bonne réponse — pas une
        # panne d'Épure.
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            # Message SANS préfixe « Transcription indisponible » : l'endpoint en
            # ajoute déjà un en journalisant (`Transcription vocale indisponible :
            # %s`), et le doubler donnait une ligne qui se répétait elle-même.
            # Même forme que les messages de `PiperEngine`, qui décrivent la
            # cause et laissent le contexte à l'appelant.
            raise VoiceModelUnavailable(
                "Le paquet 'faster-whisper' n'est pas installé."
            ) from exc

        logger.info("Chargement du modèle Whisper : %s", model_size)
        try:
            self._model = WhisperModel(model_size, device="cpu", compute_type="int8")
        except Exception as exc:
            raise VoiceModelUnavailable(
                f"Modèle de transcription illisible ({model_size}) : {exc}"
            ) from exc
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
    """Synthèse vocale Piper, modèle récupéré à la demande et vérifié.

    Le téléchargement a lieu dans ``__init__``, donc au premier accès via le
    `_LazyEngine` de `core.runtime` — c'est-à-dire à la première synthèse, pas
    au démarrage. Ne pas « simplifier » en instanciant le moteur à l'import :
    ce serait 76 Mo tirés au boot d'une installation neuve, et un démarrage qui
    dépend du réseau.
    """

    #: Dépôt amont des voix Piper. Le tag est **épinglé** (et pas ``main``) :
    #: une voix republiée en amont changerait de contenu sous nos pieds et
    #: ferait échouer la vérification sha256 ci-dessous sans que rien ne
    #: l'explique.
    #:
    #: URL **vérifiée par une requête réelle le 2026-08-09**, en ``HEAD``, avec
    #: urllib nu (sans User-Agent particulier : contrairement à Groq et Cerebras,
    #: HuggingFace ne renvoie pas de 403 à ``Python-urllib``) :
    #:
    #:   ``…/fr/fr_FR/upmc/medium/fr_FR-upmc-medium.onnx``      → 200, 76 733 615 o
    #:   ``…/fr/fr_FR/upmc/medium/fr_FR-upmc-medium.onnx.json`` → 200,       4 996 o
    #:
    #: Les deux tailles correspondent exactement aux fichiers qui fonctionnaient
    #: déjà sur le disque — c'est la seule vérification qui vaille, et elle a été
    #: faite avant d'écrire cette constante plutôt qu'après.
    _VOICES_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"

    #: sha256 des fichiers **qui fonctionnent**, mesurés sur le disque le
    #: 2026-08-09 — délibérément pas une empreinte publiée en amont. C'est
    #: auto-suffisant : ça détecte aussi bien une corruption réseau qu'un
    #: changement de contenu côté HuggingFace, sans dépendre d'un second canal
    #: qui pourrait mentir de la même façon que le premier.
    #:
    #: Une voix absente de ce tableau est téléchargée **sans** vérification,
    #: avec un avertissement : on ne prétend pas connaître ce qu'on n'a pas
    #: mesuré. Ajouter une voix ici veut dire l'avoir écoutée d'abord.
    _SHA256 = {
        "fr_FR-upmc-medium.onnx":
            "9abb3800c199148897a9ed64e100d224f3de83579f100044174ad19418f1786f",
        "fr_FR-upmc-medium.onnx.json":
            "e8636ec15dfd5d72db37a02cb5320a20f2b8d339f2a0e4337da64c58a33a5868",
    }

    #: Taille mesurée du .onnx, pour annoncer le poids à l'utilisateur AVANT de
    #: lancer 76 Mo sans prévenir (cf. :func:`etat_modele_vocal`).
    _TAILLE_ONNX = 76_733_615

    #: Progression du téléchargement en cours, lue par ``GET /voice/model``.
    #: État de **classe** et non d'instance, à dessein : pendant le
    #: téléchargement l'instance n'existe pas encore (on est dans ``__init__``,
    #: derrière `_LazyEngine`) — et c'est précisément ce moment-là que l'UI doit
    #: pouvoir suivre.
    _progres: dict = {"actif": False, "recu": 0, "total": 0}
    _progres_lock = threading.Lock()

    def __init__(self, voice: str = "fr_FR-upmc-medium", models_dir=None):
        # models_dir=None puis résolution DANS le corps : un défaut d'argument
        # est évalué à l'import, donc figé avant qu'EPURE_MODELS_DIR ait pu être
        # posée. Même piège que celui déjà payé par InstanceConfig et
        # QuotaTracker (cf. core.paths.resolve_data_dir).
        self._voice = voice
        self._models_dir = Path(models_dir) if models_dir else resolve_models_dir()
        self._models_dir.mkdir(parents=True, exist_ok=True)
        self._onnx = self._models_dir / f"{voice}.onnx"
        self._config = self._models_dir / f"{voice}.onnx.json"
        self._ensure_model()
        self._piper_voice = self._load()
        logger.info("Modèle Piper prêt : %s", voice)

    # ── Récupération du modèle ───────────────────────────────────────────────

    def _url_for(self, filename: str) -> str:
        # fr_FR-upmc-medium → lang=fr, lang_country=fr_FR, name=upmc, quality=medium
        parts = self._voice.split("-")
        if len(parts) != 3 or "_" not in parts[0]:
            raise VoiceModelUnavailable(
                f"Nom de voix inattendu : {self._voice!r} "
                "(attendu « langue_PAYS-nom-qualité », p. ex. fr_FR-upmc-medium)"
            )
        lang_country, name, quality = parts
        lang = lang_country.split("_")[0]
        return f"{self._VOICES_BASE}/{lang}/{lang_country}/{name}/{quality}/{filename}"

    def _ensure_model(self) -> None:
        """Récupère les fichiers manquants — le ``.onnx`` et son ``.onnx.json``.

        Les deux forment une **paire** : le JSON décrit l'échantillonnage et le
        phonémiseur du modèle. Un décalage de version entre les deux ne lève
        rien du tout, il produit une voix fausse. D'où la vérification par
        sha256 des deux, et pas seulement du gros fichier.
        """
        for cible, nom in (
            (self._onnx, f"{self._voice}.onnx"),
            (self._config, f"{self._voice}.onnx.json"),
        ):
            if not cible.is_file():
                self._telecharger(self._url_for(nom), cible, self._SHA256.get(nom))

    def _telecharger(self, url: str, cible: Path, sha_attendu) -> None:
        """Télécharge vers un ``.part``, vérifie, **puis** renomme.

        L'ordre n'est pas un détail. Écrire directement sur la cible laisserait,
        en cas de coupure, un fichier tronqué qui *existe* : le démarrage suivant
        le croirait valide (``_ensure_model`` ne teste que la présence), et
        planterait au chargement du modèle sans jamais retenter — une panne
        définitive causée par une coupure passagère. Le renommage final étant
        atomique, ``cible`` n'existe que complète et vérifiée.
        """
        temporaire = cible.with_name(cible.name + ".part")
        temporaire.unlink(missing_ok=True)   # reste d'une tentative interrompue
        logger.info("Téléchargement du modèle vocal : %s", url)
        digest = hashlib.sha256()
        try:
            with urllib.request.urlopen(url, timeout=60) as reponse:
                total = int(reponse.headers.get("Content-Length") or 0)
                self._debut_progres(total)
                recu = palier = 0
                with open(temporaire, "wb") as sortie:
                    while True:
                        bloc = reponse.read(1 << 20)
                        if not bloc:
                            break
                        sortie.write(bloc)
                        digest.update(bloc)
                        recu += len(bloc)
                        self._avancer_progres(recu)
                        # Un point tous les 10 % : le tray n'a que les logs pour
                        # montrer qu'un téléchargement de 76 Mo progresse.
                        if total and recu * 10 // total > palier:
                            palier = recu * 10 // total
                            logger.info(
                                "  modèle vocal : %d %% (%.1f/%.1f Mo)",
                                palier * 10, recu / 1e6, total / 1e6,
                            )
        except OSError as exc:
            # urllib.error.URLError et HTTPError dérivent d'OSError, comme les
            # erreurs disque : un seul filet, et le message dit lequel c'était.
            temporaire.unlink(missing_ok=True)
            raise VoiceModelUnavailable(
                f"Téléchargement du modèle vocal impossible ({url}) : {exc}"
            ) from exc
        finally:
            self._fin_progres()

        obtenu = digest.hexdigest()
        if sha_attendu is None:
            logger.warning(
                "Voix %s absente du tableau des empreintes : téléchargement NON "
                "vérifié (sha256 obtenu : %s)", self._voice, obtenu,
            )
        elif obtenu != sha_attendu:
            temporaire.unlink(missing_ok=True)
            raise VoiceModelUnavailable(
                f"Empreinte du modèle vocal incorrecte pour {cible.name} : attendu "
                f"{sha_attendu}, obtenu {obtenu}. Le fichier a été supprimé."
            )
        os.replace(temporaire, cible)
        logger.info(
            "Modèle vocal récupéré : %s (%.1f Mo)", cible.name, cible.stat().st_size / 1e6
        )

    # ── Progression (état de classe, cf. _progres) ───────────────────────────

    @classmethod
    def _debut_progres(cls, total: int) -> None:
        with cls._progres_lock:
            cls._progres = {"actif": True, "recu": 0, "total": total}

    @classmethod
    def _avancer_progres(cls, recu: int) -> None:
        with cls._progres_lock:
            cls._progres = {**cls._progres, "recu": recu}

    @classmethod
    def _fin_progres(cls) -> None:
        with cls._progres_lock:
            cls._progres = {"actif": False, "recu": 0, "total": 0}

    @classmethod
    def progres(cls) -> dict:
        with cls._progres_lock:
            return dict(cls._progres)

    # ── Synthèse ─────────────────────────────────────────────────────────────

    def _load(self):
        try:
            from piper.voice import PiperVoice
            return PiperVoice.load(str(self._onnx), config_path=str(self._config))
        except Exception as exc:
            # L'import est DANS le try : sans piper-tts installé, la voix est
            # indisponible — ce qui est un état prévu, pas un plantage.
            raise VoiceModelUnavailable(
                f"Modèle vocal illisible ({self._voice}) : {exc}"
            ) from exc

    def synthesize(self, text: str) -> bytes:
        """Texte → WAV en mémoire.

        ``synthesize_wav`` et pas ``synthesize`` : depuis piper-tts 1.3,
        ``PiperVoice.synthesize(text, wav)`` n'existe plus sous cette forme.
        ``synthesize`` est devenu un **générateur** d'``AudioChunk`` dont le 2e
        paramètre est une ``SynthesisConfig``. L'ancien appel ne levait donc
        rien : il fabriquait un générateur jamais consommé, n'écrivait pas une
        trame, et c'est ``wave.close()`` qui finissait par lever « # channels
        not specified » — une erreur qui ne dit pas un mot de la vraie cause.

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


def etat_modele_vocal(voice: str = "fr_FR-upmc-medium", models_dir=None) -> dict:
    """État du modèle de synthèse, sans le construire.

    Fonction libre et non méthode, à dessein : l'UI doit pouvoir demander l'état
    **avant** que `PiperEngine` existe — construire le moteur, c'est justement
    déclencher le téléchargement de 76 Mo qu'on veut annoncer. Une méthode
    d'instance ne pourrait répondre qu'une fois la question devenue inutile.
    """
    dossier = Path(models_dir) if models_dir else resolve_models_dir()
    onnx = dossier / f"{voice}.onnx"
    config = dossier / f"{voice}.onnx.json"
    progres = PiperEngine.progres()
    total = progres.get("total") or 0
    return {
        "voix": voice,
        "présent": onnx.is_file() and config.is_file(),
        "taille_attendue_mo": round(PiperEngine._TAILLE_ONNX / 1e6),
        "téléchargement_en_cours": progres["actif"],
        "progression": (progres["recu"] / total) if progres["actif"] and total else None,
    }
