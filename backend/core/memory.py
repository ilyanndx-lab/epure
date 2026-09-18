import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from core.jsonstore import read_json, transaction, write_json
from core.paths import resolve_data_dir

logger = logging.getLogger(__name__)

_PROFILE_DEFAULT = {
    # `niveau` naissait en « PTSI2 » : la filière de l'auteur, servie par défaut
    # à quiconque installe Épure. Le cœur ne suppose plus rien de l'utilisateur ;
    # ce qui spécialise une instance, ce sont ses modules et sa configuration.
    # (Ce bloc n'est aujourd'hui lu par personne — cf. l'issue « identité est
    # éditable et sans effet » : à lire ou à retirer, mais pas à pré-remplir.)
    "identité": {"niveau": "", "établissement": "", "objectif": ""},
    "préférences_interaction": {
        "style": "direct, sans reformulation inutile",
        "ne_pas_faire": ["répéter la question", "sur-expliquer les bases"],
    },
    "forces": [],
    "lacunes_confirmées": [],
}
_SESSIONS_DEFAULT = {"sessions": []}
# `fichiers_actifs` et `résumé_contexte` ont été RETIRÉS le 2026-08-27 et ne
# doivent pas être recréés ici (cf. docs/conversations-persistees.md §2).
#
# C'étaient deux notions de conversation logées dans un état global : une liste
# UNIQUE de fichiers, écrasée à chaque import et relue à chaque message, sans
# aucun moyen de choisir lesquels servir — et le résumé de ces fichiers, partagé
# par tous les fils. Elles appartiennent désormais à la conversation
# (`fichiers_attachés`, `résumé_contexte` dans `backend/history/<id>.json`), qui
# est la seule portée où « les fichiers de ce contexte » veut dire quelque chose.
#
# Leur absence n'a rien coûté en migration : ce fichier est réinitialisé à chaque
# démarrage (voir `__init__` plus bas), donc elles ne survivaient déjà à aucun
# lancement.
_CONTEXT_DEFAULT = {
    "modèle_actif": "qwen2.5:7b",
    "strict_mode": False,
    # Consigne libre valant pour toute l'instance. S'appelait
    # `session_instruction` jusqu'au 2026-08-27, et le nom disait vrai : elle
    # était remise à zéro à chaque démarrage. Elle ne l'est plus (cf.
    # `_CLES_PERSISTANTES`), donc « session » serait devenu un contresens.
    #
    # Portée et position dans le prompt inchangées : toute l'instance, et AVANT
    # la consigne de la conversation — de deux consignes qui se contredisent, la
    # plus spécifique doit être lue en dernier.
    "instruction_générale": "",
    # Raisonnement du modele affiche/produit. `True` = comportement historique :
    # les modeles qui pensent pensent. Toujours lu par `.get("raisonnement", True)`
    # et jamais par indexation directe — un `context_session.json` deja sur le
    # disque n'a pas cette cle, et `get_context` rend le fichier tel quel sans
    # fusionner ce defaut.
    "raisonnement": True,
    # Pilotage du tool-calling natif (registre `core.llm._SKILLS`). PERSISTE
    # au redémarrage (cf. `_CLES_PERSISTANTES` juste en dessous) — à la
    # différence de `raisonnement`/`strict_mode`/`modèle_actif` ci-dessus, qui
    # sont des réglages de SÉANCE et redeviennent volontairement leur défaut à
    # chaque lancement. `tool_calling` est un interrupteur de
    # sécurité/coût : l'utilisateur qui désactive `recherche_approfondie` (ou
    # tout le registre) parce qu'elle consomme des tokens/appels réseau ne
    # s'attend pas à la retrouver réactivée au silence après un redémarrage du
    # backend — ce serait précisément le genre de piège que ce fichier existe
    # pour éviter ailleurs (cf. `instruction_générale`). `budget` de
    # `recherche_approfondie` : repli si l'utilisateur n'a rien réglé, même
    # valeur que `core.llm._MAX_APPELS_RECHERCHE_APPROFONDIE` (non importé ici
    # pour éviter un cycle `memory` → `llm` → `memory` via `core.runtime` ;
    # l'accord entre les deux est verrouillé par `test_tool_calling_reglages.py`).
    "tool_calling": {
        "enabled": True,
        "skills": {
            "web_search": {"enabled": True},
            "history_search": {"enabled": True},
            "recherche_approfondie": {"enabled": True, "budget": 4},
        },
    },
    # Chantier « préfixes/skills personnalisés », phase A (fondations backend
    # seules — cf. le rapport de cette itération). PERSISTE au redémarrage
    # (`_CLES_PERSISTANTES` plus bas), même famille que `tool_calling` : un
    # utilisateur qui renomme/désactive un déclencheur ne s'attend pas à le
    # retrouver au défaut après un redémarrage.
    #
    # `integres` couvre les 5 commandes historiquement en dur (4 flags
    # structurés détectés côté client — `Component.tsx` — plus `@historique`,
    # détecté ici) : elles deviennent RENOMMABLES et DÉSACTIVABLES, mais leur
    # comportement réel ne change pas d'une ligne. `personnalises` est la
    # liste, VIDE par défaut, des objets créés par l'utilisateur — un seul
    # objet porte à la fois la possibilité d'un déclenchement MANUEL
    # (`prefixe_actif` + `trigger`) et/ou AGENTIQUE (`agentique`, exposé au
    # tool-calling natif, cf. `core.llm.construire_skills_personnalises`) ;
    # les deux cases sont indépendantes, aucune, une seule ou les deux peuvent
    # être cochées.
    "prefixes": {
        "integres": {
            "cours":      {"trigger": "@cours",      "enabled": True},
            "strict":     {"trigger": "@strict",     "enabled": True},
            "web":        {"trigger": "@web",        "enabled": True},
            "image":      {"trigger": "@image",      "enabled": True},
            "historique": {"trigger": "@historique", "enabled": True},
        },
        "personnalises": [],
        # Chaque élément de `personnalises`, une fois passé par
        # `normaliser_prefixes` :
        # {"id": str, "nom": str, "trigger": str | None, "description": str,
        #  "instruction": str, "prefixe_actif": bool, "agentique": bool,
        #  "budget": int}
        # `trigger` peut être `None` si `prefixe_actif` est faux — pas besoin
        # d'un trigger pour un skill purement agentique. `budget` ne compte
        # que si `agentique` est vrai (mêmes bornes que `recherche_approfondie`
        # ci-dessus, [1, 10] — un skill personnalisé agentique peut, comme les
        # skills natifs, être rappelé plusieurs fois par tour).
    },
}

#: Bornes du curseur de budget `recherche_approfondie` — un réglage hors de cet
#: intervalle est CLAMPÉ, jamais rejeté (cf. `normaliser_tool_calling`), même
#: philosophie que le reste de ce fichier : ne jamais faire planter un tour
#: pour une valeur mal formée.
_BUDGET_RECHERCHE_APPROFONDIE_MIN = 1
_BUDGET_RECHERCHE_APPROFONDIE_MAX = 10

#: Clés de `context_session.json` qui SURVIVENT au redémarrage.
#:
#: ⚠️ Ce fichier est réinitialisé à chaque démarrage — c'est sa raison d'être, et
#: son nom le dit. Cette liste est l'exception, et elle doit rester une liste
#: NOMMÉE plutôt qu'une condition noyée dans `__init__` : quelqu'un qui
#: « simplifierait » la réinitialisation effacerait sinon en silence une consigne
#: que l'utilisateur a écrite et croit permanente.
#:
#: Pourquoi ici et pas dans `profile.json`, qui est le stockage permanent : cette
#: clé est lue et écrite par le chemin du contexte (`GET /context`,
#: `PATCH /context/settings`, le panneau Compétences). La déplacer élargirait le
#: changement bien au-delà de sa durabilité. Le prix à en payer est ce
#: commentaire, et le test qui le verrouille.
#:
#: `tool_calling` a rejoint cette liste pour la raison INVERSE d'
#: `instruction_générale` — pas un texte que l'utilisateur a écrit et qui
#: doit lui être rendu, mais un interrupteur de sécurité/coût qui ne doit
#: jamais se réinitialiser en silence (cf. le commentaire sur
#: `_CONTEXT_DEFAULT["tool_calling"]`).
_CLES_PERSISTANTES = ("instruction_générale", "tool_calling", "prefixes")

#: Budget par défaut d'un skill personnalisé AGENTIQUE dont l'utilisateur n'a
#: rien réglé — même valeur que `recherche_approfondie`, pas de raison
#: particulière de diverger. Non lu par `core.llm` (aucun cycle à éviter ici,
#: contrairement à `_MAX_APPELS_RECHERCHE_APPROFONDIE`) : ce défaut n'existe
#: QUE côté persistance, `core.llm.construire_skills_personnalises` reçoit
#: toujours un budget déjà résolu.
_BUDGET_SKILL_PERSONNALISE_DEFAUT = 4


def normaliser_tool_calling(valeur) -> dict:
    """Fusionne `valeur` (un `tool_calling` partiel venu du disque ou d'un
    `PATCH /context/settings`) PAR-DESSUS le défaut, clé par clé — jamais un
    remplacement wholesale.

    La distinction compte : `update_context` (plus bas) fait un `data.update`
    au niveau RACINE de `context_session.json`, shallow par conception — c'est
    au bon endroit pour `instruction_générale` (une chaîne) mais figerait
    `tool_calling` à sa première écriture si on l'y laissait tel quel. Un
    remplacement wholesale ferait qu'un quatrième skill ajouté un jour à
    `_CONTEXT_DEFAULT["tool_calling"]["skills"]` n'apparaîtrait plus jamais
    pour un utilisateur qui a déjà un `tool_calling` sur disque — en silence,
    puisque rien ne compare les deux structures. Ici, un skill absent de
    `valeur` retombe sur le défaut (activé) ; un skill présent dans `valeur`
    mais absent de `_CONTEXT_DEFAULT` est ignoré plutôt que reproduit tel
    quel — un vieux frontend ou un fichier corrompu ne doit pas pouvoir
    injecter une clé arbitraire dans l'état persistant.

    Le budget de `recherche_approfondie` est CLAMPÉ à
    `[_BUDGET_RECHERCHE_APPROFONDIE_MIN, _BUDGET_RECHERCHE_APPROFONDIE_MAX]`,
    jamais rejeté — une valeur invalide (hors bornes, absente, non numérique)
    retombe sur le clamp le plus proche ou le défaut, sans jamais faire
    échouer l'appelant.

    Utilisée aux DEUX points d'entrée de cette valeur — la restauration au
    démarrage (`MemoryEngine.__init__`) et `PATCH /context/settings`
    (`modules/settings/router.py`) — pour qu'une seule fonction décide de la
    forme acceptée, jamais deux validations qui pourraient diverger.
    """
    defaut = _CONTEXT_DEFAULT["tool_calling"]
    if not isinstance(valeur, dict):
        valeur = {}
    skills_recues = valeur.get("skills")
    if not isinstance(skills_recues, dict):
        skills_recues = {}

    skills: dict = {}
    for nom, reglage_defaut in defaut["skills"].items():
        recu = skills_recues.get(nom)
        recu = recu if isinstance(recu, dict) else {}
        entree = {"enabled": bool(recu.get("enabled", reglage_defaut["enabled"]))}
        if "budget" in reglage_defaut:
            try:
                budget = int(recu.get("budget", reglage_defaut["budget"]))
            except (TypeError, ValueError):
                budget = reglage_defaut["budget"]
            entree["budget"] = max(
                _BUDGET_RECHERCHE_APPROFONDIE_MIN,
                min(_BUDGET_RECHERCHE_APPROFONDIE_MAX, budget),
            )
        skills[nom] = entree

    return {"enabled": bool(valeur.get("enabled", defaut["enabled"])), "skills": skills}


def normaliser_prefixes(valeur) -> dict:
    """Fusionne/valide `valeur` (un `prefixes` partiel venu du disque ou d'un
    `PATCH /context/settings`) — même philosophie que `normaliser_tool_calling`,
    jamais un remplacement wholesale, jamais une exception.

    **`integres`** : fusion clé par clé sur le défaut, comme les `skills` de
    `tool_calling`. Un `trigger` vide/absent/non-chaîne retombe sur le
    littéral historique (`@cours`, `@web`, …) plutôt que de laisser un
    déclencheur inatteignable — un intégré SANS trigger casserait
    silencieusement la commande, à la différence d'un personnalisé purement
    agentique, pour qui l'absence de trigger est un choix valide (cf.
    ci-dessous). Une clé intégrée absente de `valeur` (poste neuf, ou fichier
    écrit avant l'ajout d'une 6e commande un jour) réapparaît au défaut,
    activée.

    **`personnalises`** : liste d'objets bien formés, un élément mal formé
    étant SILENCIEUSEMENT écarté — jamais de quoi faire planter la
    restauration au démarrage ou un `PATCH`. « Bien formé » se juge sur les
    CASES à cocher, pas sur la présence d'un `trigger` :

    - `prefixe_actif` faux ET `agentique` faux → l'objet ne fait RIEN, écarté ;
    - `prefixe_actif` vrai mais `trigger` vide → prétend être déclenchable à
      la main sans moyen de l'être, écarté ;
    - `instruction` vide → rien à injecter dans le prompt ni à révéler au
      modèle, écarté dans tous les cas ;
    - `agentique` vrai mais `description` vide → le schéma d'outil exposé au
      modèle serait sans description, écarté (une `description` vide est en
      revanche TOLÉRÉE pour un préfixe purement manuel : `description` ne sert
      qu'à documenter le skill/l'afficher, jamais injectée dans le prompt) ;
    - `id`/`nom` absents ou non-chaînes → écarté (nécessaires respectivement à
      l'identification côté frontend et au libellé injecté `[INSTRUCTION
      PERSONNALISÉE — {nom}]`, `modules/chat/router.py`).

    `budget` est clampé comme celui de `recherche_approfondie` (mêmes bornes),
    jamais rejeté ; ignoré en pratique si `agentique` est faux, mais toujours
    stocké normalisé — un utilisateur qui coche `agentique` plus tard retrouve
    la valeur qu'il avait réglée, pas un retour silencieux au défaut.

    **Trigger dupliqué** (entre deux personnalisés, ou entre un personnalisé
    et un intégré) : PAS de dédoublonnage explicite ici — cette fonction ne
    fait que VALIDER la forme, elle ne connaît pas l'ordre de déclenchement.
    La priorité au premier dans l'ordre de la liste est une conséquence
    NATURELLE du traitement séquentiel côté `modules/chat/router.py` : le
    premier bloc qui trouve son trigger dans `user_text` le RETIRE du texte,
    donc un second objet portant le même trigger littéral ne le retrouve plus
    dans ce qui reste (`@historique` est toujours traité avant la boucle des
    personnalisés — cf. ce module). Documenté ici plutôt qu'implémenté deux
    fois.

    ⚠️ Ne JAMAIS rendre `_CONTEXT_DEFAULT["prefixes"]` ni un de ses sous-dicts
    tel quel : `MemoryEngine.__init__` fait un `dict(_CONTEXT_DEFAULT)`
    SHALLOW (un seul niveau) — `integres["cours"]` par exemple resterait le
    MÊME objet partagé par tous les appelants si on ne reconstruisait pas
    chaque entrée. Chaque sous-dict rendu ici est neuf.
    """
    defaut = _CONTEXT_DEFAULT["prefixes"]
    if not isinstance(valeur, dict):
        valeur = {}

    integres_recus = valeur.get("integres")
    if not isinstance(integres_recus, dict):
        integres_recus = {}
    integres: dict = {}
    for nom, reglage_defaut in defaut["integres"].items():
        recu = integres_recus.get(nom)
        recu = recu if isinstance(recu, dict) else {}
        trigger_recu = recu.get("trigger")
        trigger = (
            trigger_recu.strip()
            if isinstance(trigger_recu, str) and trigger_recu.strip()
            else reglage_defaut["trigger"]
        )
        integres[nom] = {
            "trigger": trigger,
            "enabled": bool(recu.get("enabled", reglage_defaut["enabled"])),
        }

    personnalises_recus = valeur.get("personnalises")
    if not isinstance(personnalises_recus, list):
        personnalises_recus = []
    personnalises: list[dict] = []
    for item in personnalises_recus:
        if not isinstance(item, dict):
            continue
        id_ = item.get("id")
        if not isinstance(id_, str) or not id_.strip():
            continue
        nom = item.get("nom")
        if not isinstance(nom, str) or not nom.strip():
            continue
        instruction = item.get("instruction")
        instruction = instruction.strip() if isinstance(instruction, str) else ""
        if not instruction:
            continue
        prefixe_actif = bool(item.get("prefixe_actif"))
        agentique = bool(item.get("agentique"))
        if not prefixe_actif and not agentique:
            continue
        trigger_recu = item.get("trigger")
        trigger = (
            trigger_recu.strip()
            if isinstance(trigger_recu, str) and trigger_recu.strip()
            else None
        )
        if prefixe_actif and not trigger:
            continue
        description = item.get("description")
        description = description.strip() if isinstance(description, str) else ""
        if agentique and not description:
            continue
        try:
            budget = int(item.get("budget", _BUDGET_SKILL_PERSONNALISE_DEFAUT))
        except (TypeError, ValueError):
            budget = _BUDGET_SKILL_PERSONNALISE_DEFAUT
        budget = max(
            _BUDGET_RECHERCHE_APPROFONDIE_MIN,
            min(_BUDGET_RECHERCHE_APPROFONDIE_MAX, budget),
        )
        personnalises.append({
            "id": id_.strip(),
            "nom": nom.strip(),
            "trigger": trigger,
            "description": description,
            "instruction": instruction,
            "prefixe_actif": prefixe_actif,
            "agentique": agentique,
            "budget": budget,
        })

    return {"integres": integres, "personnalises": personnalises}


# Le cache LRU des sections retenues (clé : `message[:100]`) a disparu avec
# l'appel LLM qu'il servait à amortir : `retrieve_relevant_context` ne dépend plus
# du message, il n'y a donc plus rien à mémoriser.


class MemoryEngine:
    def __init__(self, llm=None):
        # Plus aucun appel LLM sur le chemin d'un message depuis
        # `retrieve_relevant_context` (cf. sa docstring). L'argument reste au
        # contrat du constructeur — `core/runtime.py` l'injecte — pour un futur
        # usage HORS chemin critique ; s'en servir à chaque message est
        # précisément l'incident qui a été corrigé.
        self._llm = llm
        # Résolu à la CONSTRUCTION du moteur, pas à l'import du module :
        # cf. core.paths.resolve_data_dir.
        data_dir = resolve_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        self._profile_path = data_dir / "profile.json"
        self._sessions_path = data_dir / "memory_sessions.json"
        self._context_path = data_dir / "context_session.json"

        if not self._profile_path.exists():
            self._write(self._profile_path, _PROFILE_DEFAULT)
        if not self._sessions_path.exists():
            self._write(self._sessions_path, _SESSIONS_DEFAULT)
        # Le contexte de session est réinitialisé à chaque démarrage — SAUF les
        # clés de `_CLES_PERSISTANTES`, reprises du fichier existant.
        #
        # Le reset reste le comportement par défaut, et c'est voulu : le modèle
        # actif, le mode strict, le raisonnement sont des réglages de séance, et
        # les retrouver posés d'un lancement à l'autre a déjà surpris. La consigne
        # générale, elle, est un texte que l'utilisateur a écrit : la lui effacer
        # au redémarrage était défendable tant qu'elle s'appelait « instruction de
        # session » et qu'aucune consigne ne persistait ; ça ne l'est plus depuis
        # que la conversation en porte une qui, elle, survit.
        #
        # Lecture AVANT écriture, et repli sur le défaut clé par clé : un fichier
        # absent, vide ou illisible ne doit pas faire échouer le démarrage — il
        # rend simplement une instance neuve.
        ancien = self._read(self._context_path)
        contexte = dict(_CONTEXT_DEFAULT)
        for cle in _CLES_PERSISTANTES:
            valeur = ancien.get(cle)
            if cle == "tool_calling":
                # Dict, pas une chaîne : le filtre `isinstance(str)` ci-dessous
                # l'aurait ignoré EN SILENCE — c'est exactement le piège que
                # `normaliser_tool_calling` (qui fusionne plutôt que remplace,
                # cf. sa docstring) a été écrit pour fermer. Appelée même sur un
                # `ancien` sans cette clé (poste neuf) : rend alors une copie
                # fraîche du défaut, jamais l'objet `_CONTEXT_DEFAULT` partagé.
                contexte[cle] = normaliser_tool_calling(valeur)
            elif cle == "prefixes":
                # Même raisonnement que `tool_calling` juste au-dessus : un
                # dict, jamais une chaîne, donc le filtre `isinstance(str)`
                # ci-dessous l'ignorerait en silence sans ce cas dédié.
                contexte[cle] = normaliser_prefixes(valeur)
            elif isinstance(valeur, str) and valeur:
                contexte[cle] = valeur
        self._write(self._context_path, contexte)

    # ── I/O helpers ────────────────────────────────────────────────────────

    def _read(self, path: Path) -> dict:
        return read_json(path, {})

    def _write(self, path: Path, data: dict) -> None:
        try:
            write_json(path, data)
        except Exception:
            logger.exception("Erreur écriture %s", path)

    # ── Profile ────────────────────────────────────────────────────────────

    def load_profile(self) -> dict:
        return self._read(self._profile_path)

    def save_profile(self, data: dict) -> None:
        self._write(self._profile_path, data)

    def profile_transaction(self):
        """RMW verrouillé du profil, pour les appelants qui chargent, modifient
        et réécrivent.

        Nécessaire parce que ce couple load/save est effectué DEPUIS L'EXTÉRIEUR
        (ConsolidationEngine.apply_consolidation), lancé dans des Thread explicites
        par les modules kholle et chat : deux consolidations simultanées
        chargeaient le même profil et la seconde écrasait les lacunes ajoutées par
        la première. Un verrou dans `save_profile` n'y changerait rien — la course
        est entre le load et le save.
        """
        return transaction(self._profile_path, {})

    # ── Sessions ───────────────────────────────────────────────────────────

    def get_all_sessions(self) -> list:
        return self._read(self._sessions_path).get("sessions", [])

    def get_sessions(self, days: int = 7) -> list:
        if days <= 0:
            return self.get_all_sessions()
        cutoff = (datetime.now() - timedelta(days=days)).date().isoformat()
        return [
            s for s in self.get_all_sessions()
            if not s.get("archivée") and s.get("date", "") >= cutoff
        ]

    def add_session(
        self,
        matière: str,
        fichier: str,
        erreurs: list,
        réussies: int,
        ratées: int,
    ) -> None:
        with transaction(self._sessions_path, {"sessions": []}) as data:
            data.setdefault("sessions", []).append({
                "date": datetime.now().date().isoformat(),
                "matière": matière,
                "fichier": fichier,
                "erreurs": erreurs,
                "réussies": réussies,
                "ratées": ratées,
                "archivée": False,
            })

    def archive_sessions(self, dates: list) -> None:
        with transaction(self._sessions_path, {"sessions": []}) as data:
            for s in data.get("sessions", []):
                if s.get("date") in dates:
                    s["archivée"] = True

    def archive_old_sessions(self, days: int = 30) -> None:
        cutoff = (datetime.now() - timedelta(days=days)).date().isoformat()
        with transaction(self._sessions_path, {"sessions": []}) as data:
            for s in data.get("sessions", []):
                if s.get("date", "") < cutoff:
                    s["archivée"] = True

    def promote_lacunes(self) -> None:
        recent = self.get_sessions(days=30)
        counts: dict[str, int] = {}
        for s in recent:
            for err in s.get("erreurs", []):
                counts[err] = counts.get(err, 0) + 1

        with self.profile_transaction() as profile:
            lacunes = set(profile.get("lacunes_confirmées", []))
            changed = False
            for err, n in counts.items():
                if n >= 3 and err not in lacunes:
                    lacunes.add(err)
                    changed = True
            if changed:
                profile["lacunes_confirmées"] = sorted(lacunes)

    # ── Context session ────────────────────────────────────────────────────

    def get_context(self) -> dict:
        return self._read(self._context_path)

    def update_context(self, **kwargs) -> None:
        # Le site le plus chaud du dépôt : appelé à chaque message (modèle actif,
        # fichiers actifs, résumé de contexte) depuis le pool de threads.
        with transaction(self._context_path, {}) as data:
            data.update(kwargs)

    # ── Selective memory retrieval ─────────────────────────────────────────

    def _available_sections(self) -> list[str]:
        """Return which sections actually have data (avoids LLM hallucinating missing sections)."""
        available: list[str] = []
        profile = self.load_profile()
        if profile.get("lacunes_confirmées"):
            available.append("lacunes")
        if profile.get("forces"):
            available.append("forces")
        prefs = profile.get("préférences_interaction", {})
        if prefs.get("style") or prefs.get("ne_pas_faire"):
            available.append("style")
        recent = self.get_sessions(days=7)
        if any(s.get("erreurs") for s in recent):
            available.append("sessions_récentes")
        return available

    def retrieve_relevant_context(self, message: str) -> list[str]:
        """Sections de profil à injecter — lues sur le disque, sans aucun appel LLM.

        Cette fonction demandait au modèle LOCAL (Ollama, modèle par défaut de
        l'instance) quelles sections étaient pertinentes, sous un garde-fou
        ``future.result(timeout=2.0)``. Mesuré sur `epure_tray.log` : **30 appels,
        30 retombées sur le fallback, zéro sélection jamais utilisée.** Le coût,
        lui, était réel et payé à CHAQUE message — y compris quand le modèle actif
        est un fournisseur cloud, qui n'a rien à voir avec Ollama :

        - 2,000 s fermes de latence, le timeout étant toujours atteint ;
        - et un appel qui n'était pas annulé pour autant : ``shutdown(wait=False)``
          ne tue pas le thread, resté bloqué dans ``ollama_client.chat()`` avec son
          read-timeout de 300 s (`core/llm.py`), donc Ollama continuait de charger
          le modèle. Mesuré à froid — modèle non résident, ce qui est le cas dès
          5 min d'inactivité (`keep_alive` par défaut) : 13,8 s dont 10,2 s de
          chargement, soit ~12 s de lecture disque de 4,7 Go en concurrence avec la
          requête cloud qui suivait. À chaud le même appel prend 1,0 s et réussit :
          la panne était donc invisible en usage soutenu et systématique après une
          pause. C'est très exactement le symptôme « le premier message depuis un
          moment est lent, même en cloud ».

        Ce que cette sélection arbitrait : 28 tokens de prompt — mesuré sur le
        `memory/profile.json` réel avec la métrique du code lui-même
        (``len(result.split())``, celle qui écrit ``~N tokens`` dans le log) : 45
        pour le profil complet, 17 pour le fallback ``['style']``. Deux secondes et
        un chargement de modèle pour choisir 28 tokens n'est pas défendable — on injecte
        donc toutes les sections qui ont des données. Déterministe, sans réseau, et
        sans cache à invalider (le résultat ne dépend plus du message).

        Reste ouvert : ``sessions_récentes`` est la seule section non bornée (les
        erreurs des 5 dernières sessions). Elle est vide aujourd'hui ; si elle
        grossit, ce qu'il faudra est une troncature, pas un LLM.
        """
        # Seuil historique : un message très court n'a rien à personnaliser.
        if len(message.strip()) < 20:
            return []
        return self._available_sections()

    # ── System prompt builder ──────────────────────────────────────────────

    def build_system_context(self, message: Optional[str] = None) -> str:
        """
        Build the system context string.
        If message is provided: selectively inject only relevant profile sections.
        If message is None: inject all (legacy behaviour, used by @mémoire skill).
        Active context (résumé, instruction, strict_mode) is always included.
        """
        profile = self.load_profile()
        prefs = profile.get("préférences_interaction", {})
        style = prefs.get("style", "")
        ne_pas = prefs.get("ne_pas_faire", [])
        forces = profile.get("forces", [])
        lacunes = profile.get("lacunes_confirmées", [])
        recent = self.get_sessions(days=7)
        ctx = self.get_context()

        # Decide which profile sections to include
        if message is not None:
            sections = self.retrieve_relevant_context(message)
            include_style = "style" in sections
            include_lacunes = "lacunes" in sections
            include_forces = "forces" in sections
            include_sessions = "sessions_récentes" in sections
        else:
            # Legacy: inject everything
            include_style = bool(style or ne_pas)
            include_lacunes = bool(lacunes)
            include_forces = bool(forces)
            include_sessions = True

        parts: list[str] = []

        # Profile block (selective)
        profile_lines: list[str] = []
        if include_style and (style or ne_pas):
            if style:
                profile_lines.append(f"Style attendu : {style}")
            if ne_pas:
                profile_lines.append("À éviter : " + ", ".join(ne_pas))
        if include_forces and forces:
            profile_lines.append("Points forts : " + " ; ".join(forces[:5]))
        if include_lacunes and lacunes:
            profile_lines.append("Lacunes confirmées : " + " ; ".join(lacunes))
        if profile_lines:
            parts.append("[PROFIL ÉLÈVE]\n" + "\n".join(profile_lines))

        # Recent errors (selective)
        if include_sessions and recent:
            errors: list[str] = []
            for s in recent[-5:]:
                for e in s.get("erreurs", []):
                    errors.append(f"- {e} ({s.get('date', '')})")
            if errors:
                parts.append("[ERREURS RÉCENTES]\n" + "\n".join(errors))

        # Le bloc [CONTEXTE ACTIF] a quitté cette fonction le 2026-08-27.
        #
        # Il injectait `ctx["résumé_contexte"]`, c'est-à-dire le résumé des
        # fichiers d'une conversation — une notion que `MemoryEngine` n'a aucun
        # moyen de connaître, puisqu'il ne sait pas de quelle conversation il
        # s'agit. Tant que le résumé était global, l'incohérence ne se voyait pas ;
        # elle devient une faute dès qu'il y en a un par fil.
        #
        # C'est désormais le routeur du chat qui l'ajoute à ses `sys_parts`, où la
        # conversation est connue. Ce moteur redevient ce que son nom annonce : le
        # PROFIL de l'utilisateur, et rien d'autre.

        # Consigne générale — toujours injectée, et AVANT celle de la
        # conversation, que le routeur du chat ajoute après (`sys_parts`). De deux
        # consignes qui se contredisent, la plus spécifique doit être lue en
        # dernier.
        #
        # Le libellé du prompt reprend le vocabulaire de sa jumelle
        # (`[INSTRUCTION DE CETTE CONVERSATION]`) : côté modèle « instruction »,
        # côté interface « consigne ». Deux vocabulaires, chacun cohérent.
        instruction = ctx.get("instruction_générale", "")
        if instruction:
            parts.append(f"[INSTRUCTION GÉNÉRALE]\n{instruction}")

        # Strict mode — always injected
        if ctx.get("strict_mode"):
            parts.append(
                "[MODE STRICT]\n"
                "Réponds de façon concise et directe. "
                "Pas d'introduction ni de reformulation inutile."
            )

        result = "\n\n".join(parts)
        est_tokens = len(result.split())
        if message is not None:
            used = []
            if include_style: used.append("style")
            if include_lacunes: used.append("lacunes")
            if include_forces: used.append("forces")
            if include_sessions: used.append("sessions_récentes")
            logger.info("Memory context: %r → sections=%s ~%d tokens", message[:40], used, est_tokens)
        else:
            logger.info("Memory context: legacy → ~%d tokens", est_tokens)
        return result
