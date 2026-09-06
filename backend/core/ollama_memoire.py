"""Modèles Ollama RÉSIDENTS EN MÉMOIRE : lister, charger, éjecter.

À ne pas confondre avec `core/models.py:get_ollama_installed`, qui répond à une
autre question. **`/api/tags` dit ce qui est sur le DISQUE, `/api/ps` ce qui est
en MÉMOIRE**, et les deux ensembles sont indépendants : sept modèles installés
et zéro chargé est l'état normal d'une machine au repos. Une interface qui
dériverait l'un de l'autre afficherait exactement l'inverse de l'information
cherchée.

Trois formes mesurées sur un vrai Ollama (ce poste, 2026-09-05) plutôt que lues
dans la doc, et chacune a changé une décision de ce module :

* **`size_vram: 0` ne veut pas dire « pas chargé ».** `moondream` résident,
  servi par le CPU, annonce `size_vram: 0`. Ce champ sépare CPU et GPU ; le seul
  indicateur de charge est l'appartenance au tableau `models`. S'en remettre à
  `size_vram > 0` ferait lire « déchargé » sur toute machine sans GPU.
* **Éjecter pendant une génération ne décharge rien.** Séquence mesurée sur
  `qwen2.5:7b` : génération en flux lancée, éjection tirée 6 s après. Ollama
  répond **200 en 0,0 s** avec `done_reason: "unload"`, la génération se termine
  normalement (1194 morceaux, 146 s), et **le modèle est de nouveau résident**
  ensuite — le `keep_alive` de la requête en vol l'emporte à sa complétion. Le
  200 ment donc, et c'est pour ça que `charger`/`decharger` **relisent
  `/api/ps`** et rendent l'état RÉEL au lieu d'un succès supposé.
* **Un modèle en cours de CHARGEMENT n'est pas encore dans `/api/ps`.** Mesuré :
  `models` vide pendant les premières secondes d'une génération à froid. Sans
  conséquence ici — `charger()` est synchrone et ne rend la main qu'une fois le
  modèle résident — mais à savoir avant de bâtir un indicateur de progression
  dessus.

Ollama absent est le **cas nominal**, pas une erreur : `lister_charges()` rend
`None`, exactement comme `get_ollama_installed`, et l'appelant décide du
silence. Rien n'est jamais levé vers le client.
"""

import json
import logging
import urllib.error
import urllib.request
from typing import Optional

from core.llm import ollama_host as _ollama_host

logger = logging.getLogger(__name__)

#: Sonde d'état : doit répondre vite même quand Ollama est occupé. Même choix,
#: et même raison, que le `timeout=3` de `get_ollama_installed`.
_TIMEOUT_LECTURE_S = 3

#: Charger un modèle à froid, c'est lire plusieurs Go depuis le disque : 25 s
#: mesurées sur ce poste (§3.3 bis). Un timeout court ferait échouer un
#: chargement qui, lui, se déroule bien — l'appelant, côté HTTP, tourne dans un
#: exécuteur pour que ce temps ne bloque pas la boucle d'événements.
_TIMEOUT_ACTION_S = 300


def hote_ollama() -> str:
    """Hôte normalisé, repris de `core.llm`. **Ne pas réimplémenter** :
    `OLLAMA_HOST=0.0.0.0` est une adresse d'ÉCOUTE, avec laquelle le client ne
    peut pas se connecter sous Windows (§8). Fonction et non constante pour
    rester lisible depuis les tests."""
    return _ollama_host


def _appeler(chemin: str, corps: Optional[dict], timeout: float) -> Optional[dict]:
    """Appel JSON vers Ollama. `None` si injoignable — jamais d'exception.

    urllib et non le client `ollama` partagé, pour la raison déjà écrite dans
    `get_ollama_installed` : le client attend `model.timeout_s` entre deux
    paquets, ce qui rendrait une sonde d'état muette aussi longtemps.
    """
    url = f"{hote_ollama()}{chemin}"
    donnees = json.dumps(corps).encode("utf-8") if corps is not None else None
    entetes = {"Content-Type": "application/json"} if donnees else {}
    try:
        req = urllib.request.Request(url, data=donnees, headers=entetes)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            brut = resp.read().decode("utf-8", "replace")
        return json.loads(brut) if brut.strip() else {}
    except urllib.error.HTTPError as exc:
        # Ollama répond, mais refuse : son corps porte la raison, qu'on veut
        # montrer telle quelle plutôt qu'un « erreur interne » opaque.
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8", "replace")).get("error", "")
        except Exception:
            pass
        raise _RefusOllama(detail or f"Ollama a répondu {exc.code}.") from exc
    except Exception:
        logger.warning("Ollama non joignable sur %s", hote_ollama())
        return None


class _RefusOllama(Exception):
    """Ollama a répondu et refusé (modèle inconnu, requête invalide).

    Distinct d'« injoignable » : l'un se corrige en tapant un autre nom, l'autre
    en démarrant Ollama. Les confondre donnerait le mauvais conseil.
    """


def _decrire(entree: dict) -> dict:
    """Une entrée de `/api/ps` réduite à ce que l'interface utilise.

    `processeur` est dérivé ici et pas côté frontend : la règle (`size_vram`
    sépare CPU et GPU, sans jamais dire si le modèle est chargé) est une
    connaissance d'Ollama, elle appartient au backend.
    """
    vram = entree.get("size_vram") or 0
    details = entree.get("details") or {}
    return {
        "id": entree.get("model") or entree.get("name") or "",
        "taille": entree.get("size") or 0,
        "vram": vram,
        "processeur": "gpu" if vram > 0 else "cpu",
        "expire_a": entree.get("expires_at") or "",
        "parametres": details.get("parameter_size") or "",
        "quantification": details.get("quantization_level") or "",
    }


def lister_charges() -> Optional[list[dict]]:
    """Modèles résidents en mémoire. `[]` si aucun, `None` si Ollama ne répond
    pas.

    **La distinction `[]` / `None` porte tout le reste** : une liste vide est
    l'état normal d'une machine au repos, `None` veut dire « pas d'Ollama ».
    Les confondre ferait afficher « injoignable » sur une installation saine.
    """
    data = _appeler("/api/ps", None, _TIMEOUT_LECTURE_S)
    if data is None:
        return None
    entrees = data.get("models")
    if not isinstance(entrees, list):
        # Corps inattendu (proxy, page d'erreur) : on ne croit pas la forme sur
        # parole — même leçon que le `.json()` du §8, côté serveur.
        logger.warning("/api/ps : corps inattendu depuis %s", hote_ollama())
        return []
    return [_decrire(e) for e in entrees if isinstance(e, dict)]


# ── Capacités déclarées par Ollama ───────────────────────────────────────────
#
# MESURÉ sur Ollama 0.33.3 (ce poste), sept modèles de six familles : `/api/tags`
# porte un champ `capabilities`, sous-ensemble de `completion` / `tools` /
# `vision` / `thinking`. `/api/show` (POST — le GET répond 405) rend la même
# chose, **au même ENSEMBLE près et non à la même liste** : sur `qwen3.6`,
# `/api/tags` rend `['vision','completion',…]` et `/api/show`
# `['completion','vision',…]`. D'où des `set` partout ici : une comparaison de
# listes passerait aujourd'hui et casserait au prochain modèle réordonné.
#
# Conséquence de conception : annoter la liste ne coûte **aucune requête de
# plus**. C'est le même `/api/tags` que `get_ollama_installed` interroge déjà —
# inutile d'appeler `/api/show` par modèle.
#
# Ce qui n'est PAS mesuré : une seule version d'Ollama est installée ici, donc
# « le champ existe sur toutes les versions » reste une supposition. Un champ
# absent est traité comme INCONNU, jamais comme « aucune capacité ».
_CAPACITES = {
    "outils": "tools",
    "vision": "vision",
    "raisonnement": "thinking",
}


def capacites_installees() -> Optional[dict[str, Optional[set]]]:
    """`{id: capacités}` pour chaque modèle installé. `None` si Ollama ne
    répond pas ; une valeur `None` pour un modèle dont le champ est absent.

    Deux `None` de portées différentes, et la distinction est utile : l'un dit
    « pas d'Ollama », l'autre « cet Ollama-là ne déclare rien pour ce modèle ».
    """
    data = _appeler("/api/tags", None, _TIMEOUT_LECTURE_S)
    if data is None:
        return None
    entrees = data.get("models")
    if not isinstance(entrees, list):
        logger.warning("/api/tags : corps inattendu depuis %s", hote_ollama())
        return {}
    out: dict[str, Optional[set]] = {}
    for e in entrees:
        if not isinstance(e, dict):
            continue
        mid = e.get("model") or e.get("name")
        if not mid:
            continue
        brut = e.get("capabilities")
        out[mid] = set(brut) if isinstance(brut, list) else None
    return out


def decrire_capacites(declarees: Optional[set]) -> dict:
    """Traduit un ensemble déclaré en trois états : `True` / `False` / `None`.

    **`False` et `None` ne disent pas la même chose.** `False` est un FAIT —
    Ollama a déclaré ses capacités et celle-ci n'y est pas. `None` veut dire que
    personne n'a mesuré : aucune source pour ce fournisseur, ou champ absent.
    Les confondre ferait afficher une ignorance comme une absence, ce qui est un
    mensonge sur un point que l'utilisateur lit comme un fait.

    Les trois clés sont TOUJOURS émises, `None` compris : une clé absente arrive
    `undefined` côté TypeScript, indistinguable d'un `null`, et les
    normaliseurs du frontend (§8) l'écraseraient vers « absent ».
    """
    if declarees is None:
        return {nom: None for nom in _CAPACITES}
    return {nom: (cle in declarees) for nom, cle in _CAPACITES.items()}


def _agir(model: str, corps: dict, verbe: str) -> dict:
    """Tronc commun de `charger`/`decharger` : agir, PUIS relire l'état réel.

    Le résultat ne dit jamais « ça a marché » sur la foi du code HTTP — cf.
    l'éjection pendant une génération, qui répond 200 sans rien décharger. C'est
    `/api/ps`, relu après coup, qui tranche.
    """
    try:
        rendu = _appeler("/api/generate", corps, _TIMEOUT_ACTION_S)
    except _RefusOllama as exc:
        return {"ok": False, "message": str(exc), "charges": lister_charges()}
    charges = lister_charges()
    if rendu is None or charges is None:
        return {
            "ok": False,
            "message": f"Ollama ne répond pas — impossible de {verbe} « {model} ».",
            "charges": None,
        }
    return {"ok": True, "message": "", "charges": charges}


def charger(model: str) -> dict:
    """Rend le modèle résident. `keep_alive` est volontairement ABSENT : le
    défaut d'Ollama (5 min) s'applique, plutôt qu'une durée que l'utilisateur
    n'a pas choisie."""
    res = _agir(model, {"model": model, "prompt": ""}, "charger")
    if res["ok"] and model not in [m["id"] for m in res["charges"]]:
        # Ollama a accepté mais le modèle n'est pas là : même méfiance que pour
        # l'éjection, dans l'autre sens.
        res["ok"] = False
        res["message"] = f"« {model} » n'est pas résident après chargement."
    return res


def decharger(model: str) -> dict:
    """Libère la mémoire. **Un 200 ne prouve rien** : si une génération utilise
    ce modèle, Ollama accepte, ne décharge pas, et le remet en place à la fin de
    la requête en vol (mesuré). On relit donc, et on le dit."""
    res = _agir(model, {"model": model, "prompt": "", "keep_alive": 0}, "décharger")
    if res["ok"] and model in [m["id"] for m in res["charges"]]:
        res["ok"] = False
        res["message"] = (
            f"« {model} » est toujours chargé : une génération l'utilise sans "
            f"doute — Ollama accepte la demande puis la réinstalle à la fin. "
            f"Réessayez une fois la réponse terminée."
        )
    return res
