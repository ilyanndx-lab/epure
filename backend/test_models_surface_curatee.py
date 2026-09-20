#!/usr/bin/env python3
"""Surfaces curatées (NVIDIA, DeepSeek) dans `core.models.ModelsRegistry._build`
— ancien `test_models_deepseek.py`, élargi à NVIDIA le 2026-09-20 pour la même
classe de problème : un id figé dans une liste Python que le fournisseur a
retiré côté serveur.

## DeepSeek

Avant le 2026-09-10, l'API officielle DeepSeek exposait `deepseek-chat`/
`deepseek-reasoner` sur `/v1/models`, jamais les noms curatés
(`deepseek-v4-pro`/`deepseek-v4-flash`) — la validation live aurait donc
marqué à tort les deux modèles indisponibles, d'où un repli sur la présence
de clé seule (`_add("deepseek", _DEEPSEEK_STATIC, None)`, comme Gemini).
Depuis la bascule vers `deepseek-flash` (DeepSeek V4.1 Flash, GA le
2026-09-10), la référence officielle documente `id: "deepseek-flash"` /
`id: "deepseek-v4-pro"` en toutes lettres dans la réponse de `/v1/models` :
les ids curatés correspondent maintenant à la surface live, donc DeepSeek
peut suivre le même patron que NVIDIA/Mistral (surface curatée, live valide
la disponibilité) plutôt que le patron Gemini (clé seule).

## NVIDIA NIM

Mesuré en direct le 2026-09-19 sur `/v1/models` : trois des quatre entrées de
`_NVIDIA_STATIC` n'existaient plus. `nvidia/llama-3.1-nemotron-nano-8b-v1` a
un remplaçant direct dans la même gamme produit
(`nvidia/nemotron-nano-3-30b-a3b`) ; `deepseek-ai/deepseek-v4-flash` a une
version datée toujours au catalogue (`deepseek-ai/deepseek-v4-flash-0731`) ;
`deepseek-ai/deepseek-r1` n'a AUCUN remplaçant DeepSeek-raisonnement chez NIM
et a été retiré sans être recopié ailleurs (cf. commentaire de
`_NVIDIA_STATIC` dans `core/models.py`). Ce fichier vérifie donc aussi
l'ABSENCE des trois anciens ids, pas seulement la présence des nouveaux —
c'est cette absence qui, pour Groq, avait un précédent payé
(`test_taches_locales.CataloguesSansModeleMortTest`) : un id mort survit en
étant recopié d'un site à l'autre, jamais en restant seul dans une liste que
personne ne relit.

Ce que ce fichier NE reproduit PAS : un appel réseau réel. `urllib.request.
urlopen` est mocké, comme `test_models_lmstudio.py` le fait pour LM Studio —
la CI n'a ni clé DeepSeek ni clé NVIDIA valide, et un test qui en dépendrait
mesurerait la disponibilité d'un tiers, pas le code d'Épure.

Usage :
    python test_models_surface_curatee.py
"""

import asyncio
import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole les chemins AVANT tout import de core.*

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import core.models as core_models  # noqa: E402

#: Neutralise les autres fournisseurs cloud pendant `_build()` : ce fichier
#: teste NVIDIA/DeepSeek, pas une interaction avec Groq/Cerebras/Mistral.
_AUTRES_CLES = ("GROQ_API_KEY", "CEREBRAS_API_KEY", "MISTRAL_API_KEY")

#: Ids retirés côté fournisseur avant ce chantier — ne doivent plus jamais
#: réapparaître dans `_NVIDIA_STATIC`, même si une future entrée les recopie
#: par erreur d'un autre fichier (même piège que Groq, cf. docstring).
_NVIDIA_IDS_MORTS = (
    "nvidia/llama-3.1-nemotron-nano-8b-v1",
    "deepseek-ai/deepseek-r1",
    "deepseek-ai/deepseek-v4-flash",
)


class _FakeResponse:
    """Réponse HTTP minimale compatible avec `with urlopen(...) as resp`."""

    def __init__(self, body: str):
        self._body = body.encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class DeepSeekLiveFetchTest(unittest.TestCase):
    def setUp(self):
        cles = _AUTRES_CLES + ("NVIDIA_API_KEY", "DEEPSEEK_API_KEY")
        self._sauvegarde = {n: os.environ.get(n) for n in cles}
        for n in _AUTRES_CLES + ("NVIDIA_API_KEY",):
            os.environ.pop(n, None)
        os.environ["DEEPSEEK_API_KEY"] = "sk-test-deepseek"
        self.registry = core_models.ModelsRegistry()

    def tearDown(self):
        for n, v in self._sauvegarde.items():
            if v is None:
                os.environ.pop(n, None)
            else:
                os.environ[n] = v

    def _build(self) -> dict:
        return asyncio.run(self.registry._build())

    def _entrees_deepseek(self, catalogue: dict) -> dict[str, dict]:
        return {
            m["id"]: m
            for modeles in catalogue.values()
            for m in modeles
            if m["provider"] == "deepseek"
        }

    def test_cle_valide_deepseek_flash_apparait_disponible(self):
        """`deepseek-flash` (id API réel depuis le 2026-09-10) doit ressortir
        du catalogue, marqué disponible, quand le live le liste."""
        corps = '{"data": [{"id": "deepseek-flash"}, {"id": "deepseek-v4-pro"}]}'
        with mock.patch.object(
            core_models.urllib.request, "urlopen", return_value=_FakeResponse(corps),
        ):
            catalogue = self._build()
        entrees = self._entrees_deepseek(catalogue)
        self.assertIn("deepseek:deepseek-flash", entrees)
        self.assertTrue(entrees["deepseek:deepseek-flash"]["_disponible"])
        self.assertTrue(entrees["deepseek:deepseek-v4-pro"]["_disponible"])

    def test_ancien_id_v4_flash_absent_de_la_surface_curatee(self):
        """`deepseek-v4-flash` est retiré côté API (2026-09-10) : il ne doit
        plus apparaître dans le catalogue, même si le live le listait encore
        (alias serveur — cf. core/models.py, commentaire `_DEEPSEEK_STATIC`)."""
        corps = '{"data": [{"id": "deepseek-flash"}, {"id": "deepseek-v4-flash"}]}'
        with mock.patch.object(
            core_models.urllib.request, "urlopen", return_value=_FakeResponse(corps),
        ):
            catalogue = self._build()
        entrees = self._entrees_deepseek(catalogue)
        self.assertNotIn("deepseek:deepseek-v4-flash", entrees)

    def test_cle_invalide_verdict_visible_pas_de_crash(self):
        """401/403 → `_fetch_models` rend `[]` → les deux ids curatés sont
        marqués indisponibles. Pas d'exception, pas de repli silencieux sur
        « disponible faute de mieux »."""
        erreur = urllib.error.HTTPError("https://api.deepseek.com/v1/models", 401, "Unauthorized", {}, None)
        with mock.patch.object(
            core_models.urllib.request, "urlopen", side_effect=erreur,
        ):
            catalogue = self._build()
        entrees = self._entrees_deepseek(catalogue)
        self.assertEqual(set(entrees), {"deepseek:deepseek-v4-pro", "deepseek:deepseek-flash"})
        for mid, entree in entrees.items():
            with self.subTest(id=mid):
                self.assertFalse(entree["_disponible"], f"{mid} devrait être marqué indisponible")

    def test_panne_reseau_replie_sur_disponibilite_par_cle(self):
        """Une panne (timeout, DNS...) n'est pas un refus de clé : `_fetch_models`
        rend `None`, et `_add` retombe sur `_disponible=None` (verdict inconnu,
        pas « indisponible ») — distinct du cas clé invalide ci-dessus."""
        with mock.patch.object(
            core_models.urllib.request, "urlopen", side_effect=OSError("réseau indisponible"),
        ):
            catalogue = self._build()
        entrees = self._entrees_deepseek(catalogue)
        self.assertEqual(set(entrees), {"deepseek:deepseek-v4-pro", "deepseek:deepseek-flash"})
        for mid, entree in entrees.items():
            with self.subTest(id=mid):
                self.assertIsNone(entree["_disponible"])

    def test_non_regression_groq_reste_independant_de_deepseek(self):
        """Le câblage de DeepSeek dans `_build()` ne doit rien changer au
        comportement déjà mesuré de Groq (curatage propre, repli statique)."""
        os.environ["GROQ_API_KEY"] = "sk-test-groq"
        try:
            corps_groq = '{"data": [{"id": "openai/gpt-oss-120b"}]}'

            def _urlopen(req, timeout=4):
                if "deepseek" in req.full_url:
                    raise OSError("DeepSeek indisponible pour ce test")
                return _FakeResponse(corps_groq)

            with mock.patch.object(core_models.urllib.request, "urlopen", side_effect=_urlopen):
                catalogue = self._build()
            groq_ids = {
                m["id"] for modeles in catalogue.values() for m in modeles if m["provider"] == "groq"
            }
            self.assertEqual(groq_ids, {"groq:openai/gpt-oss-120b"})
        finally:
            os.environ.pop("GROQ_API_KEY", None)


class NvidiaSurfaceCurateeTest(unittest.TestCase):
    def setUp(self):
        cles = _AUTRES_CLES + ("NVIDIA_API_KEY", "DEEPSEEK_API_KEY")
        self._sauvegarde = {n: os.environ.get(n) for n in cles}
        for n in _AUTRES_CLES + ("DEEPSEEK_API_KEY",):
            os.environ.pop(n, None)
        os.environ["NVIDIA_API_KEY"] = "sk-test-nvidia"
        self.registry = core_models.ModelsRegistry()

    def tearDown(self):
        for n, v in self._sauvegarde.items():
            if v is None:
                os.environ.pop(n, None)
            else:
                os.environ[n] = v

    def _build(self) -> dict:
        return asyncio.run(self.registry._build())

    def _entrees_nvidia(self, catalogue: dict) -> dict[str, dict]:
        return {
            m["id"]: m
            for modeles in catalogue.values()
            for m in modeles
            if m["provider"] == "nvidia"
        }

    def test_aucun_id_mort_dans_la_surface_curatee(self):
        """Les trois ids retirés côté NIM (mesuré le 2026-09-19) ne doivent
        plus jamais être proposés — pas seulement absents aujourd'hui, mais
        absents de la LISTE PYTHON elle-même, indépendamment de ce que le live
        répond (cf. le précédent Groq, `test_taches_locales.py`)."""
        for mort in _NVIDIA_IDS_MORTS:
            with self.subTest(id=mort):
                self.assertNotIn(mort, core_models._NVIDIA_STATIC)

    def test_les_remplacants_apparaissent_disponibles_avec_live(self):
        """`nemotron-nano-3-30b-a3b` et `deepseek-v4-flash-0731` (remplaçants
        mesurés) doivent ressortir disponibles quand le live les liste — comme
        `nemotron-3-super-120b-a12b`, jamais retiré."""
        corps = (
            '{"data": ['
            '{"id": "nvidia/nemotron-3-super-120b-a12b"}, '
            '{"id": "nvidia/nemotron-nano-3-30b-a3b"}, '
            '{"id": "deepseek-ai/deepseek-v4-flash-0731"}'
            ']}'
        )
        with mock.patch.object(
            core_models.urllib.request, "urlopen", return_value=_FakeResponse(corps),
        ):
            catalogue = self._build()
        entrees = self._entrees_nvidia(catalogue)
        self.assertEqual(
            set(entrees),
            {
                "nvidia:nvidia/nemotron-3-super-120b-a12b",
                "nvidia:nvidia/nemotron-nano-3-30b-a3b",
                "nvidia:deepseek-ai/deepseek-v4-flash-0731",
            },
        )
        for mid, entree in entrees.items():
            with self.subTest(id=mid):
                self.assertTrue(entree["_disponible"], f"{mid} devrait être disponible")

    def test_id_retire_resterait_absent_meme_si_le_live_le_listait_encore(self):
        """Bout en bout : même si `/v1/models` listait encore `deepseek-r1`
        (résurrection côté fournisseur, ou API qui garde un alias comme
        DeepSeek le fait pour `v4-flash`), il ne doit pas apparaître —
        `_NVIDIA_STATIC` est la source, le live ne fait que VALIDER."""
        corps = (
            '{"data": ['
            '{"id": "nvidia/nemotron-3-super-120b-a12b"}, '
            '{"id": "deepseek-ai/deepseek-r1"}, '
            '{"id": "nvidia/llama-3.1-nemotron-nano-8b-v1"}'
            ']}'
        )
        with mock.patch.object(
            core_models.urllib.request, "urlopen", return_value=_FakeResponse(corps),
        ):
            catalogue = self._build()
        entrees = self._entrees_nvidia(catalogue)
        self.assertNotIn("nvidia:deepseek-ai/deepseek-r1", entrees)
        self.assertNotIn("nvidia:nvidia/llama-3.1-nemotron-nano-8b-v1", entrees)


if __name__ == "__main__":
    unittest.main(verbosity=2)
