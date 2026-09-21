"""Module Image — génération via ComfyUI, appelé par réseau local.

Module actif, avec ``Component.tsx`` (``frontend/src/modules/generated/image``)
— txt2img, img2img et LoRA optionnel.

ComfyUI tourne sur une machine SECONDE (pas celle du backend Épure), sur le
wifi partagé — la traversée réseau, pas seulement l'appel, peut échouer.
``COMFYUI_HOST`` suit le patron de ``LMSTUDIO_HOST``
(``core/models.py::check_lmstudio``, ``core/llm.py:99-101``) **et pas** celui
d'``OLLAMA_HOST`` : ce dernier réécrit ``0.0.0.0`` en ``localhost`` parce que
c'est une adresse d'ÉCOUTE inutilisable côté client (CLAUDE.md §8) — mais ici
un hôte distant (l'IP LAN de la machine ComfyUI) est le cas normal, pas une
erreur de config à corriger. Reprendre cette réécriture serait donc activement
faux : elle empêcherait justement l'usage prévu par ce module.

**Étape 0 — ce qui a été vérifié avant d'écrire ce fichier, pas supposé :**

1. Rien dans ce dépôt ne fait parler un LLM par le réseau local aujourd'hui.
   ``backend/.env.example:77-81`` documente LM Studio comme "lancé sur ce
   poste" (même machine que le backend) ; ``LMSTUDIO_HOST`` accepterait par
   construction une URL LAN (parsing scheme+port, aucune réécriture forcée
   vers localhost), mais rien ne l'utilise ni ne le documente ainsi. ComfyUI
   par réseau local est donc le PREMIER chemin de ce genre dans Épure — ce qui
   renforce, et n'affaiblit pas, l'avertissement ci-dessous sur ``--listen``.
2. ``check_comfyui()`` reproduit à l'identique le contrat de
   ``core.models.check_lmstudio()`` : ``urllib.request`` direct (pas de
   dépendance httpx pour une simple sonde), timeout=2, retourne un booléen.

**Pourquoi une attente par sondage HTTP et pas un WebSocket** (demandé dans la
spécification d'origine) : ``wsproto`` (déclaré pour uvicorn, cf. pieges-connus
§20) est une implémentation sans-I/O pour le côté SERVEUR d'Épure, pas un
client WebSocket sortant ; ``websockets`` a été délibérément retiré du dépôt
après deux incidents (pieges-connus §20/§23 — accélérateur C non signé,
risque Smart App Control par version) ; ``httpx`` n'a pas de client WebSocket.
Ajouter une dépendance binaire neuve pour ce module irait à l'encontre d'un
invariant payé deux fois. Interroger ``GET /history/<id>`` à intervalle fixe
jusqu'à complétion ou échéance dure ne coûte aucune dépendance de plus.

**L'échéance dure vit DANS la boucle de sondage, pas autour de l'appel
asynchrone qui l'exécute** — pieges-connus §19, texte exact : « un
``future.result(timeout=…)`` borne l'ATTENTE, pas le TRAVAIL ». Un
``asyncio.wait_for`` posé autour de ``run_in_executor`` laisserait le thread
de sondage tourner indéfiniment même après l'abandon côté requête HTTP ; la
échéance est donc un ``time.monotonic()`` vérifié à chaque itération de
:func:`generer_image`, complétée par un timeout court sur CHAQUE requête HTTP
individuelle (connexion et lecture), pas par un ``wait_for`` en plus.

**La variante img2img — IDs vérifiés contre un export réel (2026-09-21).**
Le premier export du module (txt2img seul) ne couvrait pas cette branche ;
les IDs ``40``/``41`` construits à l'origine ne correspondaient à rien de
réel et se sont révélés FAUX à la lecture d'un second export, celui-ci pris
sur un graphe img2img validé manuellement par l'utilisateur. Ce second
export réutilise EXACTEMENT le même graphe de base que
``workflow_txt2img.json`` (nœuds ``6``/``8``/``9``/``27``/``30``/``31``/``33``
identiques, y compris ``27`` — ``EmptySD3LatentImage`` — présent mais
DÉBRANCHÉ, ``31.inputs.latent_image`` ne pointant plus dessus) et y ajoute
trois nœuds :

- ``38`` — ``LoadImage``, ``inputs: {"image": "<nom de fichier>"}`` SEUL —
  pas de clé ``upload`` dans ce format API (celle-ci n'existe que côté
  widget de l'éditeur ComfyUI, pas dans le JSON envoyé à ``/prompt``). Le
  nom de fichier référencé est celui déjà présent dans le dossier
  ``input/`` de ComfyUI — d'où l'obligation d'appeler ``POST
  /upload/image`` AVANT de poster ce workflow (déjà l'ordre suivi par
  :func:`generer_image`, cf. plus bas).
- ``40`` — ``ImageScale`` (absent de la première hypothèse construite,
  jamais halluciné faute de mieux à l'époque) : ``pixels`` de
  ``VAEEncode`` ne vient PAS directement de ``LoadImage`` mais de cette
  mise à l'échelle intermédiaire. Résolution CODÉE EN DUR dans l'export
  (1024×1024) — décision prise ici : reprendre dynamiquement
  ``largeur``/``hauteur`` de ``27`` (``EmptySD3LatentImage`` du graphe de
  base, 1008×1008 en production) plutôt que dupliquer un second magique en
  dur, pour que l'image de sortie ait la même résolution en txt2img et en
  img2img sans second réglage à maintenir en synchronisation.
- ``39`` — ``VAEEncode``, ``pixels`` ← ``40`` (pas ``38``), ``vae`` ← ``30``
  (checkpoint) — inchangé par rapport à l'hypothèse initiale.

``denoise`` valait ``0.8`` dans cet export — valeur de SESSION observée sur
le poste de l'utilisateur pendant ses essais manuels, pas un défaut
délibéré (signalé comme tel). Le défaut de ce module reste ``0.75``, déjà
documenté comme placeholder arbitraire faute de mesure — rien dans cet
export ne le justifie mieux, donc pas de changement sans raison.

**LoRA optionnel (``use_lora``) — signature ``LoraLoader`` confirmée contre
le VRAI ComfyUI distant, pas supposée (Étape 0, 2026-09-21) :**
``GET /object_info/LoraLoader`` sur ``COMFYUI_HOST`` donne
``inputs.required = {model: MODEL, clip: CLIP, lora_name: enum, strength_model:
FLOAT, strength_clip: FLOAT}``, ``output = [MODEL, CLIP]`` — DEUX champs de
force séparés (modèle/CLIP), pas un seul ``strength`` comme dans d'autres
loaders ComfyUI ; l'énumération ``lora_name`` ne listait à cette date que
``super-realism.safetensors``, ce qui confirme au passage que le fichier est
bien en place côté ComfyUI. Décision de scope déjà actée : ce module
n'expose qu'un seul ``lora_strength`` côté API et le reproduit sur les DEUX
champs du node plutôt que d'exposer un réglage séparé — pas de sélecteur de
fichier, pas de multi-LoRA, ``lora_name`` fixe en dur
(:data:`_LORA_NAME`).

Le node ``LoraLoader`` (:data:`_NODE_LORA`) s'insère entre le checkpoint
(``30``) et TOUS ses consommateurs actuels de MODEL/CLIP — ``KSampler.model``
et les deux ``CLIPTextEncode.clip`` (positif ``6``, négatif ``33``) — dans
les deux branches (txt2img et img2img), puisque les deux partagent le même
graphe de base chargé par :func:`_charger_workflow`. La branche img2img ne
touche ni MODEL ni CLIP (seulement ``latent_image``/``denoise`` du
``KSampler``, et ``vae`` du checkpoint pour ``VAEEncode``) : l'insertion du
LoRA est donc indépendante de la construction du payload img2img, pas
dupliquée entre les deux. ``use_lora=False`` (défaut) laisse le graphe
strictement inchangé — aucun node ``LoraLoader`` ajouté.

``_LORA_STRENGTH_DEFAUT`` est un PLACEHOLDER non mesuré, même statut que
``denoise=0.75`` ci-dessus — compatibilité Dev/Schnell de ce LoRA précis non
garantie côté source (le checkpoint de ce module est Schnell,
``flux1-schnell-fp8.safetensors``), à surveiller au premier test réel plutôt
que supposée bonne.

**Calibration de prompt (``calibrer_prompt``) — observé en usage réel
(2026-09-21) : des utilisateurs écrivent des prompts avec la syntaxe d'un
autre outil** (ex. ``--ar 1:2 --chaos 20 --style raw``, façon Midjourney).
Flux ne lève aucune erreur : ``CLIPTextEncode`` encode ce texte tel quel, la
partie qui n'est pas un descripteur d'image est simplement inerte. Même
patron que ``core.websearch.reformuler_requete`` (CLAUDE.md §3.7 : tâche de
fond, ``modele_local_defaut()`` **toujours**, jamais de cloud, repli
silencieux sur l'entrée brute) — une fonction NOUVELLE et pas une extension
de ``reformuler_requete`` : objectif différent (prompt de diffusion
structuré, pas mots-clés de recherche web).

Ce que le prompt système impose, et pourquoi — vérifié dans
``workflow_txt2img.json`` (nœud ``31``, ``KSampler``), pas supposé :
``cfg`` y est fixé à ``1`` et ``steps`` à ``4`` — Flux Schnell les ignore par
construction. Un prompt calibré qui mentionnerait un réglage de CFG ou un
negative prompt (nœud ``33``, toujours vide dans le gabarit) induirait donc
l'utilisateur en erreur ; la calibration ne doit jamais en ajouter. Traduit
systématiquement vers l'anglais, quelle que soit la langue d'entrée.

**Découplée de ``/generate`` à dessein** : ``/calibrate-prompt`` ne déclenche
aucun appel ComfyUI, ne retourne que le texte calibré. Le résultat est
affiché et éditable côté frontend, jamais appliqué silencieusement — décision
actée, pas un choix d'implémentation à revisiter ici.
"""

import asyncio
import json
import logging
import os
import random
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from core.instance import modele_local_defaut
from core.paths import resolve_data_dir
from core.runtime import llm

logger = logging.getLogger(__name__)

router = APIRouter()

#: Suit le patron `LMSTUDIO_HOST` (`core/llm.py:99-101`) : scheme + port par
#: défaut ajoutés si absents, JAMAIS de réécriture `0.0.0.0` → `localhost`
#: (cf. docstring de tête — ici un hôte distant est le cas normal).
_COMFYUI_HOST = os.environ.get("COMFYUI_HOST", "").strip() or "http://localhost:8188"
if not _COMFYUI_HOST.startswith("http"):
    _COMFYUI_HOST = f"http://{_COMFYUI_HOST}:8188"
_COMFYUI_HOST = _COMFYUI_HOST.rstrip("/")

#: Échéance dure de génération. Mesuré manuellement par l'utilisateur :
#: de quelques secondes à ~1 minute pour 4 steps Flux Schnell — 180 s couvre
#: une machine distante chargée sans attendre indéfiniment si l'Acer est
#: injoignable ou éteint en cours de route (après un check_comfyui() positif).
_TIMEOUT_S = float(os.environ.get("COMFYUI_TIMEOUT_S", "180"))
_POLL_INTERVAL_S = 1.0
_HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

_WORKFLOW_FILE = Path(__file__).parent / "workflow_txt2img.json"

#: IDs du pipeline txt2img fourni — LUS dans le fichier, cf. docstring §Étape 0.
_NODE_POSITIVE = "6"
_NODE_KSAMPLER = "31"
_NODE_LATENT_EMPTY = "27"
_NODE_CHECKPOINT = "30"
_NODE_SAVE = "9"

#: IDs de la branche img2img — vérifiés contre un export réel (cf. docstring
#: de tête, §2026-09-21) : LoadImage → ImageScale → VAEEncode, pas
#: LoadImage → VAEEncode direct comme la première hypothèse le supposait.
_NODE_LOAD_IMAGE = "38"
_NODE_IMAGE_SCALE = "40"
_NODE_VAE_ENCODE = "39"

#: Node LoraLoader, inséré à la demande (`use_lora=True`) — ID hors de la
#: plage déjà occupée par le graphe de base et la branche img2img (cf.
#: docstring de tête, §LoRA).
_NODE_LORA = "34"
_NODE_NEGATIVE = "33"

#: Fixe en dur — décision de scope actée, pas de sélecteur multi-LoRA (cf.
#: docstring de tête).
_LORA_NAME = "super-realism.safetensors"

#: Placeholder non mesuré, même statut que le `0.75` de `denoise` plus bas —
#: appliqué identiquement à `strength_model` ET `strength_clip` (cf.
#: docstring de tête, §LoRA : deux champs séparés côté ComfyUI, un seul
#: exposé côté API de ce module).
_LORA_STRENGTH_DEFAUT = 0.6

#: Dossier des images générées, sous `resolve_data_dir()` comme
#: `code_backups/` (CLAUDE.md §3.5 : jamais de chemin figé, mais un
#: sous-dossier fixe d'un `resolve_*()` existant est le patron déjà en place —
#: pas besoin d'un nouveau résolveur pour un module qui n'a qu'une seule
#: arborescence de sortie). Isolé des tests gratuitement via `EPURE_DATA_DIR`
#: (`_test_env.py`), sans y toucher.
def _images_dir() -> Path:
    d = resolve_data_dir() / "generated_images"
    d.mkdir(parents=True, exist_ok=True)
    return d


class ComfyUIError(Exception):
    """Échec de génération — message destiné à remonter tel quel au frontend."""


def check_comfyui() -> bool:
    """True si ComfyUI répond sur `_COMFYUI_HOST`.

    Reproduit à l'identique `core.models.check_lmstudio()` — même minimalisme
    (urllib direct, timeout=2, booléen), même raison : c'est une sonde de
    joignabilité pure, pas un chemin qui a besoin d'httpx.
    """
    try:
        req = urllib.request.Request(f"{_COMFYUI_HOST}/system_stats")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def _charger_workflow() -> dict:
    with open(_WORKFLOW_FILE, encoding="utf-8") as f:
        return json.load(f)


def _uploader_image(image_source: bytes) -> str:
    """POST /upload/image — appelé AVANT `/prompt` : LoadImage(38) référence
    un fichier déjà présent dans le dossier `input/` de ComfyUI, cf. docstring
    de tête (export réel du 2026-09-21).

    Retourne la référence à passer à `LoadImage.inputs.image`
    (``"sous-dossier/nom"`` si ComfyUI range l'upload dans un sous-dossier,
    ``"nom"`` sinon — convention ComfyUI, pas une hypothèse de ce module).
    """
    try:
        resp = httpx.post(
            f"{_COMFYUI_HOST}/upload/image",
            files={"image": (f"{uuid.uuid4().hex}.png", image_source, "image/png")},
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        raise ComfyUIError(f"Envoi de l'image source à ComfyUI échoué : {exc}") from exc
    nom = data.get("name")
    if not nom:
        raise ComfyUIError(f"Réponse d'upload ComfyUI inattendue : {data!r}")
    sous_dossier = data.get("subfolder") or ""
    return f"{sous_dossier}/{nom}" if sous_dossier else nom


def _appliquer_lora(workflow: dict, strength: float) -> None:
    """Insère `LoraLoader` entre le checkpoint (30) et tous ses consommateurs
    actuels de MODEL/CLIP — commun aux deux branches (txt2img/img2img), qui
    partagent le même graphe de base (cf. docstring de tête, §LoRA).

    Signature confirmée contre le vrai ComfyUI distant (`GET
    /object_info/LoraLoader`), pas supposée : deux champs de force séparés,
    reproduits ici depuis le seul `strength` exposé côté API de ce module.
    """
    workflow[_NODE_LORA] = {
        "inputs": {
            "model": [_NODE_CHECKPOINT, 0],
            "clip": [_NODE_CHECKPOINT, 1],
            "lora_name": _LORA_NAME,
            "strength_model": strength,
            "strength_clip": strength,
        },
        "class_type": "LoraLoader",
        "_meta": {"title": "Load LoRA (super-realism)"},
    }
    workflow[_NODE_KSAMPLER]["inputs"]["model"] = [_NODE_LORA, 0]
    workflow[_NODE_POSITIVE]["inputs"]["clip"] = [_NODE_LORA, 1]
    workflow[_NODE_NEGATIVE]["inputs"]["clip"] = [_NODE_LORA, 1]


#: Seuils de validation de `calibrer_prompt` — même esprit que
#: `websearch._REFORMULATION_MAX_LEN`, mais un prompt de diffusion structuré
#: (sujet, style, éclairage, composition) est légitimement plus long que 2-3
#: mots-clés de recherche. PLACEHOLDER non mesuré, même statut que
#: `_LORA_STRENGTH_DEFAUT` ci-dessus.
_CALIBRATION_MAX_LEN = 400


def calibrer_prompt(prompt_brut: str) -> str:
    """Calibre un prompt utilisateur pour Flux.1 Schnell via le modèle LOCAL.

    Même patron que `core.websearch.reformuler_requete` (tâche de fond au
    sens de CLAUDE.md §3.7 : jamais de cloud, `modele_local_defaut()`
    toujours, repli silencieux sur `prompt_brut` en cas d'échec) — nouvelle
    fonction, pas une extension de `reformuler_requete` elle-même : objectif
    différent (prompt de diffusion structuré, pas mots-clés de recherche
    web). Cf. docstring de tête, §Calibration, pour le contexte complet
    (contraintes Flux vérifiées, pas supposées).

    Ne lève jamais : un échec (modèle local absent, timeout, sortie invalide)
    replie silencieusement sur `prompt_brut` tel quel — jamais d'exception
    remontée au frontend, jamais de génération bloquée par une calibration
    ratée.
    """
    if not prompt_brut or not prompt_brut.strip():
        return prompt_brut
    prompt = (
        "Tu calibres un prompt pour un modèle de génération d'image par "
        "diffusion (Flux.1 Schnell). Réécris le prompt suivant en ANGLAIS, "
        "quelle que soit sa langue d'origine, structuré comme une liste de "
        "descripteurs séparés par des virgules (sujet, style, éclairage, "
        "composition) — pas une phrase narrative.\n"
        "Retire toute syntaxe empruntée à un autre outil de génération "
        "d'image : paramètres commençant par -- (ex. --ar, --chaos, "
        "--style), poids de mots entre parenthèses (ex. (mot:1.3)), ou "
        "toute autre option de ligne de commande.\n"
        "N'ajoute JAMAIS de negative prompt ni de mention d'un réglage CFG "
        "— ce modèle n'en tient pas compte (cfg fixe, ignoré).\n"
        "Réponds UNIQUEMENT le prompt calibré, sur une seule ligne, sans "
        "explication ni préambule ni guillemets.\n\n"
        f"Prompt original : {prompt_brut}"
    )
    try:
        sortie = llm.generate([{"role": "user", "content": prompt}], model=modele_local_defaut())
        candidate = sortie.strip().strip('"').strip("'")
        if (
            not candidate
            or len(candidate) > _CALIBRATION_MAX_LEN
            or "\n" in candidate
            or "```" in candidate
            or "`" in candidate
        ):
            return prompt_brut
        return candidate
    except Exception:
        logger.debug("Calibration de prompt image échouée, repli sur le prompt brut", exc_info=True)
        return prompt_brut


def generer_image(
    prompt: str,
    seed: Optional[int] = None,
    image_source: Optional[bytes] = None,
    denoise: Optional[float] = None,
    use_lora: bool = False,
    lora_strength: Optional[float] = None,
) -> bytes:
    """Génère une image via ComfyUI et retourne ses octets PNG.

    Bloquant de bout en bout (POST + sondage HTTP) — à appeler UNIQUEMENT via
    `loop.run_in_executor`, jamais depuis une coroutine directement (cf.
    docstring de tête sur l'échéance dure).

    `image_source` non None → branche img2img (`LoadImage` → `ImageScale` →
    `VAEEncode`, IDs vérifiés contre un export réel, cf. docstring de tête).
    `denoise` n'a d'effet que dans ce cas ; ignoré en txt2img où le gabarit
    fixe `denoise=1` sur latent vide.

    `use_lora=True` insère `LoraLoader` (cf. :func:`_appliquer_lora`),
    indépendamment de la branche txt2img/img2img choisie. `use_lora=False`
    (défaut) laisse le graphe strictement identique à avant cette option —
    aucune régression sur le comportement déjà vérifié.
    """
    workflow = _charger_workflow()
    workflow[_NODE_POSITIVE]["inputs"]["text"] = prompt
    workflow[_NODE_KSAMPLER]["inputs"]["seed"] = (
        seed if seed is not None else random.randint(0, 2**32 - 1)
    )

    if use_lora:
        _appliquer_lora(
            workflow,
            lora_strength if lora_strength is not None else _LORA_STRENGTH_DEFAUT,
        )

    if image_source is not None:
        ref = _uploader_image(image_source)
        # Résolution de sortie : reprise de l'EmptySD3LatentImage du graphe de
        # base (27, 1008×1008 en production) plutôt qu'un second 1024×1024 en
        # dur comme dans l'export — évite deux réglages de résolution à tenir
        # synchronisés pour un seul pipeline (cf. docstring de tête).
        largeur = workflow[_NODE_LATENT_EMPTY]["inputs"]["width"]
        hauteur = workflow[_NODE_LATENT_EMPTY]["inputs"]["height"]
        workflow[_NODE_LOAD_IMAGE] = {
            # Pas de clé "upload" : absente du format API réel (cf. docstring
            # de tête) — c'est un artefact de l'éditeur ComfyUI, pas du JSON
            # envoyé à /prompt.
            "inputs": {"image": ref},
            "class_type": "LoadImage",
            "_meta": {"title": "Load Image (img2img)"},
        }
        workflow[_NODE_IMAGE_SCALE] = {
            "inputs": {
                "upscale_method": "nearest-exact",
                "width": largeur,
                "height": hauteur,
                "crop": "disabled",
                "image": [_NODE_LOAD_IMAGE, 0],
            },
            "class_type": "ImageScale",
            "_meta": {"title": "Upscale Image (img2img)"},
        }
        workflow[_NODE_VAE_ENCODE] = {
            "inputs": {
                "pixels": [_NODE_IMAGE_SCALE, 0],
                "vae": [_NODE_CHECKPOINT, 2],
            },
            "class_type": "VAEEncode",
            "_meta": {"title": "VAE Encode (img2img)"},
        }
        workflow[_NODE_KSAMPLER]["inputs"]["latent_image"] = [_NODE_VAE_ENCODE, 0]
        workflow[_NODE_KSAMPLER]["inputs"]["denoise"] = (
            # 0,75 reste un PLACEHOLDER arbitraire documenté — l'export réel
            # du 2026-09-21 portait 0.8, mais signalé par l'utilisateur comme
            # une valeur de session (résidu d'essais manuels), pas un défaut
            # voulu ; rien ici ne justifie de changer le placeholder.
            denoise if denoise is not None else 0.75
        )

    client_id = uuid.uuid4().hex
    try:
        resp = httpx.post(
            f"{_COMFYUI_HOST}/prompt",
            json={"prompt": workflow, "client_id": client_id},
            timeout=_HTTP_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise ComfyUIError(f"ComfyUI injoignable — génération jamais lancée : {exc}") from exc
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        # ComfyUI répond 400 avec le détail du refus (node_errors) dans le
        # corps — `raise_for_status()` le rejette avant qu'on l'ait lu, donc
        # on le relit explicitement plutôt que de perdre le motif.
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise ComfyUIError(f"ComfyUI a refusé le workflow : {detail}") from exc
    data = resp.json()

    if data.get("node_errors"):
        raise ComfyUIError(f"ComfyUI a refusé le workflow : {data['node_errors']}")
    prompt_id = data.get("prompt_id")
    if not prompt_id:
        raise ComfyUIError(f"Réponse ComfyUI sans prompt_id : {data!r}")

    echeance = time.monotonic() + _TIMEOUT_S
    while time.monotonic() < echeance:
        try:
            resp = httpx.get(
                f"{_COMFYUI_HOST}/history/{prompt_id}", timeout=_HTTP_TIMEOUT
            )
            resp.raise_for_status()
            historique = resp.json()
        except httpx.HTTPError as exc:
            # Le wifi partagé Yoga-Acer n'est "pas garanti à 100 %" (cf.
            # docstring de tête) — un sondage raté est le cas attendu, pas
            # une raison d'abandonner une génération que ComfyUI mène encore.
            # Seul l'échec du POST /prompt initial, ci-dessus, veut dire
            # "jamais lancée".
            logger.warning("Sondage ComfyUI momentanément en échec (%s) — nouvel essai avant l'échéance", exc)
            time.sleep(_POLL_INTERVAL_S)
            continue

        entree = historique.get(prompt_id)
        if entree:
            images = entree.get("outputs", {}).get(_NODE_SAVE, {}).get("images")
            if images:
                return _recuperer_image(images[0])
            # Terminé (succès ou erreur) mais rien au nœud SaveImage — une
            # erreur d'exécution ComfyUI (ex. OOM sur les 8 Go de VRAM de
            # l'Acer) ne doit pas se faire passer pour un problème réseau en
            # tournant jusqu'à l'échéance. Forme du champ `status` NON
            # vérifiée contre un ComfyUI réel (aucun joignable à l'écriture) —
            # cf. ComfyUIIntegrationTest, qui l'imprime au premier passage réel.
            statut = entree.get("status") or {}
            if statut.get("completed") or statut.get("status_str") == "error":
                raise ComfyUIError(
                    f"ComfyUI a terminé sans image au nœud {_NODE_SAVE} "
                    f"({statut.get('status_str', 'statut inconnu')}) : "
                    f"{statut.get('messages')}"
                )

        time.sleep(_POLL_INTERVAL_S)

    raise ComfyUIError(
        f"ComfyUI n'a pas terminé après {_TIMEOUT_S:.0f} s — "
        "machine distante injoignable ou éteinte en cours de génération ?"
    )


def _recuperer_image(image_info: dict) -> bytes:
    params = {
        "filename": image_info.get("filename", ""),
        "subfolder": image_info.get("subfolder", ""),
        "type": image_info.get("type", "output"),
    }
    try:
        resp = httpx.get(f"{_COMFYUI_HOST}/view", params=params, timeout=_HTTP_TIMEOUT)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise ComfyUIError(f"Récupération de l'image générée échouée : {exc}") from exc
    return resp.content


@router.get("/status")
async def image_status():
    """Joignabilité de ComfyUI — même contrat que les sondes de `/models`.

    `check_comfyui()` est un `urlopen` synchrone (timeout=2) : `run_in_executor`
    comme ses voisines de `main.py` (`check_lmstudio` notamment), sinon 2 s de
    boucle asyncio gelée à chaque appel — précisément dans le cas courant
    (Acer éteint) où ce endpoint sert à le détecter vite.
    """
    loop = asyncio.get_running_loop()
    reachable = await loop.run_in_executor(None, check_comfyui)
    return {"comfyui_reachable": reachable, "host": _COMFYUI_HOST}


@router.post("/calibrate-prompt")
async def image_calibrate_prompt(prompt: str = Form(...)):
    """Calibre un prompt SANS lancer de génération — cf. docstring de tête,
    §Calibration : découplé de `/generate` à dessein, aucun appel ComfyUI ici.

    `run_in_executor` : `calibrer_prompt` bloque sur `llm.generate()` (appel
    HTTP synchrone vers le modèle local), même raison que `/status` ci-dessus.
    """
    loop = asyncio.get_running_loop()
    calibrated = await loop.run_in_executor(None, calibrer_prompt, prompt)
    return {"prompt": calibrated}


@router.post("/generate")
async def image_generate(
    prompt: str = Form(...),
    seed: Optional[int] = Form(None),
    denoise: Optional[float] = Form(None),
    use_lora: bool = Form(False),
    lora_strength: Optional[float] = Form(None),
    image: Optional[UploadFile] = File(None),
):
    """Génère une image (txt2img, ou img2img si `image` est fourni).

    Multipart (`Form` + `File`), pas un corps JSON : c'est le seul moyen
    d'accepter un upload binaire et des champs texte sur le même endpoint
    FastAPI.
    """
    image_bytes_source = await image.read() if image is not None else None
    loop = asyncio.get_running_loop()
    try:
        image_bytes = await loop.run_in_executor(
            None, generer_image, prompt, seed, image_bytes_source, denoise,
            use_lora, lora_strength,
        )
    except ComfyUIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    nom = f"{uuid.uuid4().hex}.png"
    chemin = _images_dir() / nom
    chemin.write_bytes(image_bytes)
    return {"path": str(chemin), "filename": nom}


@router.get("/file/{nom}")
async def image_file(nom: str):
    """Sert le PNG généré par `/generate`, référencé par son `filename`.

    Confinement par `cible.parent == racine`, pas `is_relative_to` — même
    choix que `EncreEngine._page_path`/`HistoryEngine._conv_path` (CLAUDE.md
    §6) et pour la même raison écrite là-bas : `is_relative_to` admettrait
    encore un sous-dossier (`sub/x` → `<images>/sub/x.png`), confiné mais pas
    ce qu'on veut valider pour un nom qui doit être un segment nu ; et un
    antislash est un séparateur de chemin sous Windows (plateforme primaire),
    pas seulement `/` — un `{nom}` construit avec `..\\..\\x` doit donc être
    rejeté par la comparaison de PARENT, pas supposé inoffensif parce que
    Starlette n'a laissé passer aucun `/`.
    """
    if not nom or nom in (".", ".."):
        raise HTTPException(status_code=404, detail="Fichier introuvable")
    racine = _images_dir()
    cible = (racine / nom).resolve()
    if cible.parent != racine or not cible.is_file():
        raise HTTPException(status_code=404, detail="Fichier introuvable")
    return FileResponse(cible, media_type="image/png")
