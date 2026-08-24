import logging
import os
import time
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path
from threading import Thread
from typing import Optional

from core.instance import modele_local_defaut
from core.jsonstore import read_json, transaction, write_json
from core.paths import resolve_data_dir

logger = logging.getLogger(__name__)

# `_CLASSIFY_MODEL_GROQ = "groq:llama-3.1-8b-instant"` vivait ici : c'était le
# modèle de `classify_task`, choisi dès qu'une clé Groq existait. Retiré le
# 2026-08-24 avec son dernier appelant — la classification est locale (cf.
# `_classify_model`). Une constante sans appelant ferait croire, à la relecture,
# que ce chemin peut encore partir vers le cloud.

_KEY_MAP = {
    "groq":     "GROQ_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "mistral":  "MISTRAL_API_KEY",
    "nvidia":   "NVIDIA_API_KEY",
    "gemini":   "GEMINI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "flm":      None,
}

# Providers that use prefix:model_id notation — anything else with ":" is an Ollama model name
_KNOWN_PROVIDERS = set(_KEY_MAP.keys()) | {"gemini"}


def _is_cloud_model(model_id: str) -> bool:
    """True only when the prefix is a known remote/local-server provider."""
    if ":" not in model_id:
        return False
    return model_id.split(":", 1)[0] in _KNOWN_PROVIDERS


def _ollama_ok() -> bool:
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=1) as r:
            return r.status == 200
    except Exception:
        return False


# Fonction et non constante : cf. core.paths.resolve_data_dir.
def _presets_file() -> Path:
    return resolve_data_dir() / "orchestrator_presets.json"


EFFORT_PIPELINES: dict = {
    "direct": [],
    "low": [
        {
            "role": "contextualizer",
            "label": "Contextualisation",
            "prompt_template": "Reformule cette question en intégrant le contexte disponible : {message}",
            "recommended": "local",
        },
        {
            "role": "responder",
            "label": "Réponse",
            "prompt_template": "Réponds à cette question contextualisée : {contextualizer_output}",
            "recommended": "active",
        },
    ],
    "medium": [
        {
            "role": "analyzer",
            "label": "Analyse",
            "prompt_template": "Analyse ce problème et décompose-le en étapes claires : {message}",
            "recommended": "groq:deepseek-r1-distill-llama-70b",
        },
        {
            "role": "solver",
            "label": "Résolution",
            "prompt_template": (
                "Résous ce problème en te basant sur cette analyse :\n{analyzer_output}\n"
                "Problème : {message}"
            ),
            "recommended": "active",
        },
        {
            "role": "pedagogue",
            "label": "Reformulation pédagogique",
            "prompt_template": (
                "Reformule cette solution de façon pédagogique, en explicitant "
                "les étapes :\n{solver_output}"
            ),
            "recommended": "gemini:gemini-2.5-flash",
        },
    ],
    "high": [
        {
            "role": "analyzer",
            "label": "Analyse approfondie",
            "prompt_template": (
                "Analyse en profondeur, identifie les pièges et concepts clés : {message}"
            ),
            "recommended": "groq:deepseek-r1-distill-llama-70b",
        },
        {
            "role": "solver",
            "label": "Résolution rigoureuse",
            "prompt_template": (
                "Résous rigoureusement, justifie chaque étape :\nAnalyse : {analyzer_output}\n"
                "Problème : {message}"
            ),
            "recommended": "active",
        },
        {
            "role": "verifier",
            "label": "Vérification",
            "prompt_template": (
                "Vérifie cette résolution, identifie erreurs et imprécisions :\n{solver_output}"
            ),
            "recommended": "groq:deepseek-r1-distill-llama-70b",
        },
        {
            "role": "pedagogue",
            "label": "Synthèse finale",
            "prompt_template": (
                "Synthétise pédagogiquement :\n"
                "Résolution vérifiée : {verifier_output}"
            ),
            "recommended": "gemini:gemini-2.5-flash",
        },
    ],
}

_DEFAULT_PRESETS = [
    {
        "id": str(uuid.uuid4()),
        "nom": "Kholle maths",
        "effort": "high",
        "steps": [
            {"role": "analyzer", "model": "groq:deepseek-r1-distill-llama-70b"},
            {"role": "solver", "model": "qwen2.5:7b"},
            {"role": "verifier", "model": "groq:deepseek-r1-distill-llama-70b"},
            {"role": "pedagogue", "model": "gemini:gemini-2.5-flash"},
        ],
        "défaut": True,
    },
    {
        "id": str(uuid.uuid4()),
        "nom": "Révision rapide",
        "effort": "low",
        "steps": [
            {"role": "contextualizer", "model": "qwen2.5:7b"},
            {"role": "responder", "model": "qwen2.5:7b"},
        ],
        "défaut": True,
    },
    {
        "id": str(uuid.uuid4()),
        "nom": "Full local",
        "effort": "medium",
        "steps": [
            {"role": "analyzer", "model": "qwen2.5:7b"},
            {"role": "solver", "model": "qwen2.5:7b"},
            {"role": "pedagogue", "model": "qwen2.5:7b"},
        ],
        "défaut": True,
    },
]


def _load_presets() -> list:
    data = read_json(_presets_file(), None)
    if isinstance(data, dict):
        return data.get("presets", [])
    return list(_DEFAULT_PRESETS)


def _save_presets(presets: list) -> None:
    write_json(_presets_file(), {"presets": presets})


@contextmanager
def _presets_transaction():
    """RMW verrouillé des presets, cédant la LISTE.

    Le défaut reproduit `_load_presets` : fichier absent → on repart des presets
    livrés, jamais d'une liste vide, sinon une création concurrente juste après une
    suppression du fichier les effacerait.
    """
    with transaction(_presets_file(), {"presets": list(_DEFAULT_PRESETS)}) as doc:
        # Fichier hand-édité en liste nue (`_load_presets` tolère ce cas en
        # lecture) : impossible de le normaliser en place, donc on refuse la
        # mutation — rien n'est réécrit — plutôt que de persister une forme
        # inattendue.
        if not isinstance(doc, dict):
            raise TypeError(
                f"{_presets_file().name} : document {type(doc).__name__}, attendu un objet"
            )
        yield doc.setdefault("presets", list(_DEFAULT_PRESETS))


def _ensure_presets_file() -> None:
    if not _presets_file().exists():
        _save_presets(list(_DEFAULT_PRESETS))


_ensure_presets_file()


class OrchestratorEngine:
    def __init__(self, llm):
        self._llm = llm

    def _provider_ok(self, provider: str) -> bool:
        key = _KEY_MAP.get(provider)
        if key is None:
            return provider == "flm"
        return bool(os.environ.get(key, "").strip())

    def _resolve_model(self, model_id: str, active_model: str, local_model: str) -> str:
        if model_id == "active":
            return active_model
        if model_id == "local":
            return local_model
        # Only check availability for known cloud/server providers — Ollama model names
        # like "qwen2.5:7b" must pass through untouched even though they contain ":"
        if _is_cloud_model(model_id):
            provider = model_id.split(":", 1)[0]
            if not self._provider_ok(provider):
                logger.info("Provider %s indisponible, fallback local: %s", provider, local_model)
                return local_model
        logger.debug("Résolution modèle: %r → %r", model_id, model_id)
        return model_id

    def _classify_model(self) -> str:
        """Modèle de la classification du palier Adaptatif — **local**.

        Il rendait `groq:llama-3.1-8b-instant` dès qu'une clé Groq existait, et
        `classify_task` tourne AVANT CHAQUE MESSAGE en mode Adaptatif : c'était
        donc un appel cloud automatique par message, que personne n'avait choisi
        — le cas le plus net de la règle « pas de cloud sans choix explicite ».

        Deux choses mesurées qui ont décidé du sens :

        * la classification est un mot à rendre (`simple|moderate|complex`), avec
          `num_predict` par défaut : un modèle local la produit sans peine ;
        * `groq:llama-3.1-8b-instant` **répond 404 aujourd'hui** (retiré du
          catalogue Groq, vérifié le 2026-08-24). Cette branche « cloud »
          échouait donc déjà en silence, absorbée par le `except Exception` de
          `classify_task` qui retombe sur `{"complexity": "simple"}` — c'est-à-dire
          que le palier Adaptatif se comportait comme Direct sans le dire.

        Rendre un `str` et non `Optional[str]` : `None` laissait `LLMEngine`
        retomber sur `config.yaml`, ce qui court-circuitait le réglage.
        """
        return modele_local_defaut()

    # ── Public API ─────────────────────────────────────────────────────────────

    def classify_task(self, message: str, context: dict) -> dict:
        model = self._classify_model()
        prompt = (
            "Classifie cette demande en une seule catégorie :\n"
            "- simple : question directe, salutation, explication basique\n"
            "- moderate : explication avec exemples, résumé, flashcards\n"
            "- complex : résolution d'exercice, démonstration, analyse multi-étapes, kholle\n\n"
            f"Demande : {message[:500]}\n\n"
            "Réponds UNIQUEMENT avec un seul mot : simple | moderate | complex"
        )
        try:
            raw = self._llm.generate([{"role": "user", "content": prompt}], model=model)
            raw = raw.strip().lower()
            for level in ("complex", "moderate", "simple"):
                if level in raw:
                    return {"complexity": level}
        except Exception:
            logger.exception("Erreur classify_task")
        return {"complexity": "simple"}

    def build_steps(self, effort: str, step_models: list[dict], ctx: dict) -> list[dict]:
        """
        Merge effort pipeline templates with user-chosen models.
        step_models: [{"role": "analyzer", "model": "groq:..."}]
        Returns list of steps ready for run_pipeline.
        """
        # `active_model` reste le modèle du CHAT : c'est le sens de la valeur
        # `"active"` d'un template de palier, et le palier est un choix explicite
        # de l'utilisateur. Seul son REPLI change — il retombait sur config.yaml.
        active_model = ctx.get("modèle_actif") or modele_local_defaut()
        # `"local"` veut dire le modèle local de l'instance, donc le réglage, plus
        # `config.yaml` que personne n'édite depuis l'interface.
        local_model = modele_local_defaut()
        templates = EFFORT_PIPELINES.get(effort, [])
        model_map = {s["role"]: s["model"] for s in step_models}

        steps = []
        for tpl in templates:
            role = tpl["role"]
            recommended = tpl.get("recommended", "active")
            if role in model_map:
                chosen = self._resolve_model(model_map[role], active_model, local_model)
            else:
                chosen = self._resolve_model(recommended, active_model, local_model)
            steps.append({
                "role": role,
                "label": tpl["label"],
                "model": chosen,
                "prompt_template": tpl["prompt_template"],
                "max_tokens": 4096,
            })
        return steps

    async def run_pipeline(self, steps: list[dict], message: str, messages: list[dict], loop,
                           raisonnement: bool = True):
        """AsyncGenerator yielding pipeline events.

        ``raisonnement`` est transmis tel quel à chaque étape : un réglage qui
        s'appliquerait au chat direct mais pas au mode « effort » serait
        incompréhensible, alors que c'est justement le mode le plus lent. Le
        défaut ``True`` garde le comportement d'avant pour tout autre appelant.
        """
        import asyncio

        system_msgs = [m for m in messages if m.get("role") == "system"]
        outputs: dict[str, str] = {}
        final_output = ""
        pipeline_start = time.time()
        total_tokens = 0

        # Check Ollama once at pipeline start (non-blocking, 1s timeout)
        ollama_available = await loop.run_in_executor(None, _ollama_ok)
        if not ollama_available:
            logger.warning("Pipeline: Ollama non disponible (localhost:11434)")

        def _step_available(mdl: str) -> tuple[bool, str]:
            """Returns (available, reason). Ollama models are only unavailable if Ollama is down."""
            if _is_cloud_model(mdl):
                provider = mdl.split(":", 1)[0]
                if not self._provider_ok(provider):
                    return False, f"clé API {provider} manquante"
                return True, ""
            # Ollama model
            if not ollama_available:
                return False, "Ollama non disponible"
            return True, ""

        for i, step in enumerate(steps):
            t_start = time.time()
            # Repli d'une étape sans modèle résolu : local, jamais config.yaml.
            model = step.get("model") or modele_local_defaut()
            role = step.get("role", f"step{i + 1}")
            label = step.get("label", role)

            logger.debug("Pipeline step %d/%d : role=%s model=%s", i + 1, len(steps), role, model)

            yield {
                "type": "step_start",
                "step": i,
                "total": len(steps),
                "role": role,
                "label": label,
                "model": model,
            }

            # Build template variables with fallback for skipped/empty upstream steps
            last_non_empty = next(
                (v for v in reversed(list(outputs.values())) if v), message
            )
            fmt_vars: dict = {"message": message}
            for k, v in outputs.items():
                fmt_vars[f"{k}_output"] = v if v else last_non_empty
            try:
                step_prompt = step.get("prompt_template", "{message}").format(**fmt_vars)
            except (KeyError, IndexError):
                # Manual substitution with same fallback
                step_prompt = step.get("prompt_template", "{message}")
                for k, v in fmt_vars.items():
                    step_prompt = step_prompt.replace("{" + k + "}", v)
                # Replace any remaining unknown {X_output} with last_non_empty
                import re
                step_prompt = re.sub(r'\{[a-z_]+_output\}', last_non_empty, step_prompt)

            step_messages = system_msgs + [{"role": "user", "content": step_prompt}]

            # Stream via thread
            queue: asyncio.Queue = asyncio.Queue()

            step_max_tokens: int = step.get("max_tokens", 4096)

            def _run(msgs, q, lp, mdl, mt=step_max_tokens, _role=role):
                try:
                    for token in self._llm.stream(msgs, model=mdl, max_tokens=mt,
                                                  raisonnement=raisonnement):
                        asyncio.run_coroutine_threadsafe(q.put(token), lp)
                except Exception:
                    logger.exception("Erreur pipeline step %s (model=%s)", _role, mdl)
                    asyncio.run_coroutine_threadsafe(q.put({"__error__": True}), lp)
                finally:
                    asyncio.run_coroutine_threadsafe(q.put(None), lp)

            available, unavail_reason = _step_available(model)
            if not available:
                err_msg = f"[{model} — {unavail_reason} — étape ignorée]"
                outputs[role] = ""  # register empty so downstream templates don't break
                yield {"type": "step_error", "step": i, "role": role, "label": label, "model": model, "message": err_msg}
                logger.warning("Pipeline step %d/%d (%s · %s) ignorée: %s", i + 1, len(steps), role, model, unavail_reason)
                continue

            Thread(target=_run, args=(step_messages, queue, loop, model), daemon=True).start()

            accumulated = ""
            had_error = False
            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, dict):
                    if item.get("__error__"):
                        had_error = True
                    continue
                if isinstance(item, str):
                    accumulated += item
                    yield {"type": "token", "content": item}

            duration_ms = int((time.time() - t_start) * 1000)
            token_count = len(accumulated.split())
            tps = token_count / max((time.time() - t_start), 0.001)
            total_tokens += token_count

            logger.info(
                "Pipeline step %d/%d (%s · %s) : %dms · %d chars%s",
                i + 1, len(steps), role, model, duration_ms, len(accumulated),
                " [ERREUR]" if had_error else "",
            )

            # Always register output (even empty) so downstream templates resolve cleanly
            outputs[role] = accumulated
            if accumulated:
                final_output = accumulated

            yield {
                "type": "step_end",
                "step": i,
                "role": role,
                "label": label,
                "model": model,
                "output": accumulated,
                "stats": {
                    "tps": round(tps, 1),
                    "tokens": token_count,
                    "duration_ms": duration_ms,
                },
            }

        total_ms = int((time.time() - pipeline_start) * 1000)
        yield {
            "type": "pipeline_done",
            "final_output": final_output,
            "total_stats": {
                "duration_ms": total_ms,
                "steps": len(steps),
                "total_tokens": total_tokens,
            },
        }

    # ── Presets ───────────────────────────────────────────────────────────────

    def get_presets(self) -> list:
        return _load_presets()

    def create_preset(self, nom: str, effort: str, steps: list[dict]) -> dict:
        preset = {
            "id": str(uuid.uuid4()),
            "nom": nom,
            "effort": effort,
            "steps": steps,
            "défaut": False,
        }
        with _presets_transaction() as presets:
            presets.append(preset)
        return preset

    def delete_preset(self, preset_id: str) -> bool:
        with _presets_transaction() as presets:
            target = next((p for p in presets if p["id"] == preset_id), None)
            if target is None or target.get("défaut"):
                return False
            presets[:] = [p for p in presets if p["id"] != preset_id]
            return True
