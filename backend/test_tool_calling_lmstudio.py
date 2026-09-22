#!/usr/bin/env python3
"""Tool-calling natif sur LM Studio — et SURTOUT rien de changé pour les six
autres fournisseurs de `_stream_openai`.

`_stream_openai` est partagé par sept fournisseurs (groq, cerebras, mistral,
nvidia, deepseek, flm, lmstudio). Le tool-calling n'y est activé que pour
`lmstudio` ; pour les six autres, le corps de requête envoyé doit rester celui
d'avant ce chantier, **à l'octet**. Le routeur du chat (`modules/chat/router.py`,
`_stream`) passe `outils=outils_actifs` à `stream()` QUEL QUE SOIT le
fournisseur : le vrai risque de régression est donc un fournisseur cloud qui
recevrait une liste d'outils non vide — c'est exactement le cas éprouvé par
`NonRegressionSixFournisseursTest`, écrit et passé au vert sur le code d'AVANT
le chantier, puis gardé tel quel.

**Tout est mocké** (client `openai`, sonde de capacité, `core.websearch`,
`core.webcontent`) : CI-safe. Le bout en bout sur un vrai LM Studio est dans
`integration_tool_calling_lmstudio.py` (préfixe `integration_`, hors
`discover`, cf. CLAUDE.md §2).

Les chunks rejoués reprennent la forme MESURÉE sur LM Studio
(`mistralai/ministral-3-3b`, diagnostic préalable au chantier) : l'`id` et le
`name` n'arrivent que dans le PREMIER fragment d'un `tool_call`, les
`arguments` arrivent token par token (`""` → `"{"` → `"\\""` → `"ville"`…),
et la fin est signalée par `finish_reason: "tool_calls"`. Construits avec les
VRAIES classes du SDK `openai`, comme `test_llm_lmstudio.py`.

Usage :
    python test_tool_calling_lmstudio.py
"""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole EPURE_DATA_DIR AVANT tout import de core.*

os.environ["EPURE_ALLOWED_HOSTS"] = "localhost,127.0.0.1,::1"
os.environ.setdefault("EPURE_CORS_ORIGINS", "http://localhost:5173")
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from openai.types.chat import ChatCompletionChunk  # noqa: E402
from openai.types.chat.chat_completion_chunk import (  # noqa: E402
    Choice as ChoiceOai, ChoiceDelta, ChoiceDeltaToolCall, ChoiceDeltaToolCallFunction,
)
from openai.types.completion_usage import CompletionUsage  # noqa: E402

import core.webcontent as webcontent_mod  # noqa: E402
import core.websearch as websearch_mod  # noqa: E402
from core import llm as module_llm  # noqa: E402
from core.llm import LLMEngine  # noqa: E402

_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")

_SIX_AUTRES = ("groq", "cerebras", "mistral", "nvidia", "deepseek", "flm")

#: Historique réaliste d'un tour de chat : system prompt, échange précédent, et
#: une clé HORS `{role, content}` (`images`) que la normalisation historique
#: doit continuer de retirer — c'est elle qui aurait trahi une normalisation
#: élargie par mégarde.
_HISTORIQUE = [
    {"role": "system", "content": "Tu es Épure."},
    {"role": "user", "content": "Bonjour", "images": ["x.png"]},
    {"role": "assistant", "content": "Bonjour !"},
    {"role": "user", "content": "Quel temps fait-il à Lyon ?"},
]


# ── Fabrique de chunks (vraies classes du SDK) ───────────────────────────────

def _chunk(content=None, tool_calls=None, finish_reason=None, usage=None):
    delta = ChoiceDelta(role="assistant", content=content, tool_calls=tool_calls)
    return ChatCompletionChunk(
        id="chatcmpl-test", object="chat.completion.chunk", created=1787582789,
        model="mistralai/ministral-3-3b",
        choices=[] if usage is not None else [
            ChoiceOai(index=0, delta=delta, finish_reason=finish_reason)],
        usage=usage,
    )


def _frag(index, arguments="", id=None, name=None):
    """Un fragment de `tool_call` tel que LM Studio le diffuse : `id`/`name`
    seulement sur le premier fragment d'un index, `arguments` morcelés."""
    fn = ChoiceDeltaToolCallFunction(name=name, arguments=arguments)
    return ChoiceDeltaToolCall(index=index, id=id, type="function" if id else None, function=fn)


def _fragments_tool_call(index, nom, arguments_json, call_id, taille=2):
    """Découpe `arguments_json` en fragments de `taille` caractères, précédés
    du fragment d'ouverture mesuré (`id` + `name` + `arguments=""`)."""
    chunks = [_chunk(tool_calls=[_frag(index, "", id=call_id, name=nom)])]
    for i in range(0, len(arguments_json), taille):
        chunks.append(_chunk(tool_calls=[_frag(index, arguments_json[i:i + taille])]))
    return chunks


def _usage(prompt, completion):
    return _chunk(usage=CompletionUsage(prompt_tokens=prompt, completion_tokens=completion,
                                        total_tokens=prompt + completion))


def _resultat(rang, titre="Titre", url="https://exemple.fr/page", extrait="Extrait."):
    return websearch_mod.ResultatWeb(rang=rang, titre=titre, url=url, extrait=extrait,
                                     moteur="ddg-html")


# ── Client openai factice ────────────────────────────────────────────────────

class _ClientFactice:
    """`client.chat.completions.create(**kw)` : capture chaque appel, rend le
    N-ième round de `rounds`. Un round de trop lève (défaut du code sous test).
    `refuse_stream_options` rejoue un fournisseur qui refuse `stream_options`
    (le repli de `_stream_openai` doit rester inchangé lui aussi)."""

    def __init__(self, rounds=None, refuse_stream_options=False):
        self.appels: list[dict] = []
        self._it = iter(rounds if rounds is not None else [[_chunk(content="ok"), _usage(10, 1)]])
        self._refuse = refuse_stream_options
        client = self

        class _Completions:
            def create(_self, **kw):
                # Copie PROFONDE via JSON : `msgs` est muté par la boucle
                # d'outils entre deux rounds, il faut figer ce qui est PARTI.
                client.appels.append(json.loads(json.dumps(kw)))
                if client._refuse and "stream_options" in kw:
                    raise RuntimeError("stream_options non supporté")
                return iter(next(client._it))

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


def _payload_attendu_avant_chantier(moteur, provider, model_id, messages, raisonnement,
                                    avec_usage=True):
    """Le corps que `_stream_openai` construisait AVANT ce chantier — même
    ordre de clés que son `dict(...)` d'origine, pour une comparaison à
    l'octet de `json.dumps`."""
    kw = dict(
        model=model_id,
        messages=[{"role": m["role"], "content": m["content"]} for m in messages],
        stream=True,
        temperature=moteur._gen["temperature"],
        max_tokens=moteur._budget(None, raisonnement),
    )
    if provider == "flm":
        kw["extra_body"] = {"think": bool(raisonnement)}
    if avec_usage:
        kw["stream_options"] = {"include_usage": True}
    return kw


def _octets(kw) -> str:
    return json.dumps(kw, ensure_ascii=False)


class NonRegressionSixFournisseursTest(unittest.TestCase):
    """Écrit et vérifié AVANT toute modification de `_stream_openai`.

    Pour chacun des six fournisseurs, même appel que le routeur du chat —
    outils actifs, skills dynamiques, callback de trace, `rang_web_existant`
    non nul — et le corps envoyé doit être celui d'avant, à l'octet. La sonde
    de capacité LM Studio est piégée : l'appeler pour un autre fournisseur
    serait déjà une régression (un aller-retour HTTP ajouté sur chaque tour).
    """

    def setUp(self):
        self.moteur = LLMEngine(config_path=_CONFIG)
        piege = mock.patch.object(
            module_llm, "_capacites_lmstudio_fraiches",
            side_effect=AssertionError("sonde LM Studio appelée hors lmstudio"),
            create=True,
        )
        piege.start()
        self.addCleanup(piege.stop)

    def _tour(self, provider, client, **kwargs):
        with mock.patch.object(LLMEngine, "_openai_client", return_value=client):
            return list(self.moteur.stream(list(_HISTORIQUE), model=f"{provider}:un-modele",
                                           **kwargs))

    def _kwargs_routeur(self):
        return dict(
            outils=["web_search", "history_search", "recherche_approfondie", "mon_skill"],
            budgets_override={"recherche_approfondie": 4},
            skills_dynamiques=module_llm.construire_skills_personnalises(
                [{"nom": "Mon skill", "description": "d", "instruction": "i", "budget": 2}]
            ),
            on_etape_recherche=lambda e: None,
            rang_web_existant=3,
        )

    def test_payload_identique_a_l_octet_avec_outils_actifs(self):
        for provider in _SIX_AUTRES:
            for raisonnement in (True, False):
                with self.subTest(provider=provider, raisonnement=raisonnement):
                    client = _ClientFactice()
                    self._tour(provider, client, raisonnement=raisonnement,
                               **self._kwargs_routeur())
                    self.assertEqual(len(client.appels), 1)
                    attendu = _payload_attendu_avant_chantier(
                        self.moteur, provider, "un-modele", _HISTORIQUE, raisonnement)
                    self.assertEqual(_octets(client.appels[0]), _octets(attendu))

    def test_payload_identique_avec_ou_sans_outils(self):
        for provider in _SIX_AUTRES:
            with self.subTest(provider=provider):
                avec, sans = _ClientFactice(), _ClientFactice()
                self._tour(provider, avec, **self._kwargs_routeur())
                self._tour(provider, sans)
                self.assertEqual(_octets(avec.appels), _octets(sans.appels))

    def test_repli_sans_stream_options_inchange(self):
        for provider in _SIX_AUTRES:
            with self.subTest(provider=provider):
                client = _ClientFactice(refuse_stream_options=True)
                self._tour(provider, client, **self._kwargs_routeur())
                self.assertEqual(len(client.appels), 2)
                self.assertEqual(
                    _octets(client.appels[1]),
                    _octets(_payload_attendu_avant_chantier(
                        self.moteur, provider, "un-modele", _HISTORIQUE, True,
                        avec_usage=False)),
                )

    def test_sortie_inchangee_un_seul_stats_aucune_sentinelle_outil(self):
        for provider in _SIX_AUTRES:
            with self.subTest(provider=provider):
                client = _ClientFactice(rounds=[[
                    _chunk(content="Il fait "), _chunk(content="beau."),
                    _chunk(finish_reason="stop"), _usage(40, 3),
                ]])
                sortie = self._tour(provider, client, **self._kwargs_routeur())
                self.assertEqual("".join(p for p in sortie if isinstance(p, str)), "Il fait beau.")
                dicts = [p for p in sortie if isinstance(p, dict)]
                self.assertEqual([list(d)[0] for d in dicts], ["__stats__"])
                self.assertEqual(dicts[0]["prompt_tokens"], 40)
                self.assertEqual(dicts[0]["contexte_tokens"], 40)


# ── Chemin LM Studio ─────────────────────────────────────────────────────────

_MODELE = "mistralai/ministral-3-3b"


class _Scene:
    """Le temps d'un test : client factice, capacités LM Studio figées, et
    pipeline `core.websearch`/`core.webcontent` remplacé. `reformuler_requete`
    est PIÉGÉE — le tool-calling ne doit jamais l'appeler (le modèle a déjà
    choisi ses mots-clés, cf. `_executer_outil_web_search`)."""

    def __init__(self, rounds, capacites=None, resultats_par_recherche=None):
        self.client = _ClientFactice(rounds=rounds)
        self.capacites = {_MODELE: True} if capacites is None else capacites
        self.sondes = 0
        self.recherches: list[str] = []
        self.etapes: list[dict] = []
        self._resultats_it = iter(resultats_par_recherche or [])

    def _sonde(self):
        self.sondes += 1
        return self.capacites

    def _rechercher(self, requete, on_etape=None):
        self.recherches.append(requete)
        try:
            return next(self._resultats_it)
        except StopIteration:
            return [_resultat(1), _resultat(2)]

    def __enter__(self):
        self._patches = [
            mock.patch.object(module_llm, "_capacites_lmstudio_fraiches", self._sonde),
            mock.patch.object(LLMEngine, "_openai_client", return_value=self.client),
            mock.patch.object(websearch_mod, "rechercher", self._rechercher),
            mock.patch.object(websearch_mod, "reformuler_requete",
                              side_effect=AssertionError("reformuler_requete appelée")),
            mock.patch.object(webcontent_mod, "recuperer_contenu",
                              lambda resultats, requete, on_etape=None: resultats),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()

    def tour(self, outils=("web_search",), rang_web_existant=0, **kwargs):
        moteur = LLMEngine(config_path=_CONFIG)
        return list(moteur.stream(
            [{"role": "user", "content": "Quel temps fait-il à Lyon ?"}],
            model=f"lmstudio:{_MODELE}",
            outils=list(outils) if outils is not None else None,
            on_etape_recherche=self.etapes.append,
            rang_web_existant=rang_web_existant, **kwargs,
        ))


def _textes(sortie):
    return "".join(p for p in sortie if isinstance(p, str))


def _sentinelles(sortie, cle):
    return [p for p in sortie if isinstance(p, dict) and p.get(cle)]


def _round_final(texte="Il fait beau [1].", prompt=120, completion=8):
    return [_chunk(content=texte), _chunk(finish_reason="stop"), _usage(prompt, completion)]


def _round_outil(appels, prompt=60, completion=12, taille=2, contenu_avant=None):
    """Un round qui se termine par `finish_reason="tool_calls"`. `appels` :
    liste de `(index, nom, arguments_json, id)`, sérialisés l'un après l'autre."""
    chunks = []
    if contenu_avant:
        chunks.append(_chunk(content=contenu_avant))
    for index, nom, args, call_id in appels:
        chunks += _fragments_tool_call(index, nom, args, call_id, taille=taille)
    chunks += [_chunk(finish_reason="tool_calls"), _usage(prompt, completion)]
    return chunks


class CapaciteLmStudioTest(unittest.TestCase):

    def test_modele_entraine_recoit_les_outils(self):
        with _Scene([_round_final()]) as scene:
            scene.tour()
        self.assertEqual([t["function"]["name"] for t in scene.client.appels[0]["tools"]],
                         ["web_search"])

    def test_modele_non_entraine_ou_inconnu_ne_recoit_rien(self):
        for capacites in ({_MODELE: False}, {}, {"autre/modele": True}):
            with self.subTest(capacites=capacites):
                with _Scene([_round_final()], capacites=capacites) as scene:
                    sortie = scene.tour()
                self.assertNotIn("tools", scene.client.appels[0])
                self.assertEqual(len(scene.client.appels), 1)
                self.assertEqual(_textes(sortie), "Il fait beau [1].")

    def test_sans_outils_actifs_la_sonde_n_est_meme_pas_appelee(self):
        for outils in (None, [], ["inexistant"]):
            with self.subTest(outils=outils):
                with _Scene([_round_final()]) as scene:
                    scene.tour(outils=outils)
                self.assertEqual(scene.sondes, 0)
                self.assertNotIn("tools", scene.client.appels[0])

    def test_sans_capacite_le_payload_est_celui_d_avant(self):
        """Même garantie à l'octet que les six autres fournisseurs, pour
        LM Studio quand il n'a pas (ou pas prouvé) la capacité."""
        moteur = LLMEngine(config_path=_CONFIG)
        with _Scene([_round_final()], capacites={_MODELE: False}) as scene:
            scene.tour()
        attendu = _payload_attendu_avant_chantier(
            moteur, "lmstudio", _MODELE,
            [{"role": "user", "content": "Quel temps fait-il à Lyon ?"}], True)
        self.assertEqual(_octets(scene.client.appels[0]), _octets(attendu))


class SondeCapacitesTest(unittest.TestCase):
    """`core.models.capacites_outils_lmstudio` sur la forme MESURÉE de
    `/api/v1/models`, et le cache de `core.llm`."""

    _V1 = {"models": [
        {"type": "llm", "key": "mistralai/ministral-3-3b",
         "capabilities": {"vision": True, "trained_for_tool_use": True}},
        {"type": "llm", "key": "mistral-7b-instruct-v0.2",
         "capabilities": {"vision": False, "trained_for_tool_use": False}},
        {"type": "llm", "key": "qwen/qwen3.8-27b",
         "capabilities": {"trained_for_tool_use": True},
         "variants": ["qwen/qwen3.8-27b@q4_k_m"]},
        {"type": "embedding", "key": "text-embedding-nomic-embed-text-v1.5", "capabilities": {}},
        {"type": "llm", "key": "sans-champ"},
        {"type": "llm", "key": "chaine", "capabilities": {"trained_for_tool_use": "true"}},
    ]}

    def _repondre(self, corps):
        reponse = mock.MagicMock()
        reponse.read.return_value = json.dumps(corps).encode("utf-8")
        reponse.__enter__.return_value = reponse
        return mock.patch("urllib.request.urlopen", return_value=reponse)

    def test_forme_mesuree(self):
        from core.models import capacites_outils_lmstudio
        with self._repondre(self._V1):
            caps = capacites_outils_lmstudio()
        self.assertEqual(caps, {
            "mistralai/ministral-3-3b": True,
            "mistral-7b-instruct-v0.2": False,
            "qwen/qwen3.8-27b": True,
            "qwen/qwen3.8-27b@q4_k_m": True,
            "text-embedding-nomic-embed-text-v1.5": False,
            "sans-champ": False,
            "chaine": False,
        })

    def test_injoignable_rend_none(self):
        from core.models import capacites_outils_lmstudio
        with mock.patch("urllib.request.urlopen", side_effect=OSError("refusé")):
            self.assertIsNone(capacites_outils_lmstudio())

    def test_cache_et_repli_injoignable(self):
        self.addCleanup(setattr, module_llm, "_capacites_lmstudio_cache", (0.0, {}))
        module_llm._capacites_lmstudio_cache = (0.0, {})
        with mock.patch("core.models.capacites_outils_lmstudio",
                        return_value={_MODELE: True}) as sonde:
            self.assertTrue(module_llm._capacite_tools_lmstudio(_MODELE))
            self.assertTrue(module_llm._capacite_tools_lmstudio(_MODELE))
            self.assertEqual(sonde.call_count, 1)
        module_llm._capacites_lmstudio_cache = (0.0, {})
        with mock.patch("core.models.capacites_outils_lmstudio", return_value=None):
            self.assertFalse(module_llm._capacite_tools_lmstudio(_MODELE))


class FragmentationTest(unittest.TestCase):

    def test_un_tool_call_fragmente_caractere_par_caractere(self):
        args = json.dumps({"requete": "météo Lyon aujourd'hui"}, ensure_ascii=False)
        rounds = [
            _round_outil([(0, "web_search", args, "call_4ZfXa91")], taille=1),
            _round_final(),
        ]
        with _Scene(rounds) as scene:
            sortie = scene.tour()
        # Beaucoup de fragments réellement rejoués, pas un seul bloc.
        self.assertGreater(len(rounds[0]), 20)
        self.assertEqual(scene.recherches, ["météo Lyon aujourd'hui"])
        second = scene.client.appels[1]["messages"]
        self.assertEqual(second[-2], {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "call_4ZfXa91", "type": "function",
                            "function": {"name": "web_search", "arguments": args}}],
        })
        self.assertEqual(second[-1]["role"], "tool")
        self.assertEqual(second[-1]["tool_call_id"], "call_4ZfXa91")
        self.assertIn("[1]", second[-1]["content"])
        self.assertEqual(_textes(sortie), "Il fait beau [1].")
        # Trace : l'étape d'origine tool-calling, distincte du classifieur.
        self.assertEqual(scene.etapes[0], {"etape": "tool_call_web_search",
                                           "requete": "météo Lyon aujourd'hui"})
        appels = _sentinelles(sortie, "__tool_call__")
        self.assertEqual(len(appels), 1)
        self.assertEqual(appels[0]["outil"], "web_search")
        self.assertEqual([r.rang for r in appels[0]["resultats"]], [1, 2])

    def test_un_seul_stats_agrege(self):
        rounds = [
            _round_outil([(0, "web_search", '{"requete": "a"}', "c1")], prompt=60, completion=12),
            _round_final(prompt=300, completion=20),
        ]
        with _Scene(rounds) as scene:
            sortie = scene.tour()
        stats = _sentinelles(sortie, "__stats__")
        self.assertEqual(len(stats), 1)
        self.assertEqual(stats[0]["prompt_tokens"], 360)
        self.assertEqual(stats[0]["output_tokens"], 32)
        self.assertEqual(stats[0]["contexte_tokens"], 300)
        self.assertIs(sortie[-1], stats[0])

    def test_deux_tool_calls_entrelaces_par_index(self):
        """Fragments des index 0 et 1 alternés chunk par chunk — seul l'index
        permet de les recoller, l'`id` n'étant présent qu'au premier."""
        a0 = '{"requete": "météo Lyon"}'
        a1 = '{"requete": "pollution Lyon"}'
        f0 = _fragments_tool_call(0, "web_search", a0, "call_A", taille=3)
        f1 = _fragments_tool_call(1, "web_search", a1, "call_B", taille=3)
        entrelaces = []
        for i in range(max(len(f0), len(f1))):
            entrelaces += f0[i:i + 1] + f1[i:i + 1]
        rounds = [
            entrelaces + [_chunk(finish_reason="tool_calls"), _usage(60, 12)],
            _round_final(),
        ]
        with _Scene(rounds, resultats_par_recherche=[
            [_resultat(1), _resultat(2)], [_resultat(1), _resultat(2), _resultat(3)],
        ]) as scene:
            sortie = scene.tour(rang_web_existant=1)
        self.assertEqual(scene.recherches, ["météo Lyon", "pollution Lyon"])
        second = scene.client.appels[1]["messages"]
        self.assertEqual([tc["id"] for tc in second[-3]["tool_calls"]], ["call_A", "call_B"])
        self.assertEqual([tc["function"]["arguments"] for tc in second[-3]["tool_calls"]], [a0, a1])
        self.assertEqual([m["tool_call_id"] for m in second[-2:]], ["call_A", "call_B"])
        # Renumérotation : après 1 résultat du classifieur, puis à la suite.
        rangs = [[r.rang for r in s["resultats"]] for s in _sentinelles(sortie, "__tool_call__")]
        self.assertEqual(rangs, [[2, 3], [4, 5, 6]])

    def test_plafond_de_deux_appels_web_par_tour(self):
        rounds = [
            _round_outil([(0, "web_search", '{"requete": "a"}', "c1")]),
            _round_outil([(0, "web_search", '{"requete": "b"}', "c2")]),
            _round_final(),
        ]
        with _Scene(rounds) as scene:
            scene.tour()
        self.assertEqual(scene.recherches, ["a", "b"])
        self.assertIn("tools", scene.client.appels[1])
        self.assertNotIn("tools", scene.client.appels[2])

    def test_outil_inconnu_en_boucle_reste_borne(self):
        """Un nom inconnu ne décrémente aucun budget : sans le garde-fou de
        rounds, un modèle qui insiste bouclerait sans fin. Budget web = 2 →
        au plus 2 rounds avec outils, puis un round de conclusion SANS."""
        inconnu = [(0, "outil_invente", "{}", "cx")]
        rounds = [_round_outil(inconnu), _round_outil(inconnu), _round_final()]
        with _Scene(rounds) as scene:
            sortie = scene.tour()
        self.assertEqual(scene.recherches, [])
        self.assertEqual(len(scene.client.appels), 3)
        self.assertIn("tools", scene.client.appels[1])
        self.assertNotIn("tools", scene.client.appels[2])
        self.assertEqual(_textes(sortie), "Il fait beau [1].")
        self.assertEqual(scene.client.appels[1]["messages"][-1]["content"],
                         "Outil inconnu : outil_invente")


class ArgumentsInvalidesTest(unittest.TestCase):

    def test_json_invalide_abandonne_proprement_puis_repond_sans_outil(self):
        rounds = [
            _round_outil([(0, "web_search", '{"requete": "météo', "call_X")]),
            _round_final(texte="Je ne peux pas vérifier, mais…"),
        ]
        with _Scene(rounds) as scene:
            sortie = scene.tour()
        self.assertEqual(scene.recherches, [])
        self.assertEqual(scene.etapes, [{"etape": "tool_call_abandonne",
                                         "raison": "arguments_invalides",
                                         "outils": ["web_search"]}])
        self.assertEqual(len(scene.client.appels), 2)
        relance = scene.client.appels[1]
        self.assertNotIn("tools", relance)
        # Le round cassé n'entre pas dans l'historique de relance.
        self.assertEqual(relance["messages"], scene.client.appels[0]["messages"])
        self.assertEqual(_textes(sortie), "Je ne peux pas vérifier, mais…")
        self.assertEqual(_sentinelles(sortie, "__tool_call__"), [])
        self.assertEqual(len(_sentinelles(sortie, "__stats__")), 1)

    def test_arguments_qui_ne_sont_pas_un_objet(self):
        rounds = [_round_outil([(0, "web_search", '["météo"]', "c1")]), _round_final()]
        with _Scene(rounds) as scene:
            scene.tour()
        self.assertEqual(scene.recherches, [])
        self.assertEqual(scene.etapes[0]["raison"], "arguments_invalides")

    def test_un_seul_appel_invalide_ecarte_tout_le_round(self):
        rounds = [
            _round_outil([(0, "web_search", '{"requete": "ok"}', "c1"),
                          (1, "web_search", '{"requete": ', "c2")]),
            _round_final(),
        ]
        with _Scene(rounds) as scene:
            scene.tour()
        self.assertEqual(scene.recherches, [])
        self.assertEqual(scene.etapes[0]["outils"], ["web_search", "web_search"])

    def test_texte_deja_emis_n_est_pas_duplique(self):
        rounds = [_round_outil([(0, "web_search", "{cassé", "c1")],
                               contenu_avant="Voici ce que je sais : il fait doux.")]
        with _Scene(rounds) as scene:
            sortie = scene.tour()
        self.assertEqual(len(scene.client.appels), 1)
        self.assertEqual(_textes(sortie), "Voici ce que je sais : il fait doux.")

    def test_flux_coupe_avec_fragments_en_attente(self):
        coupe = _fragments_tool_call(0, "web_search", '{"requete": "mét', "c1")
        rounds = [coupe + [_chunk(finish_reason="length"), _usage(60, 2048)], _round_final()]
        with _Scene(rounds) as scene:
            sortie = scene.tour()
        self.assertEqual(scene.recherches, [])
        self.assertEqual(scene.etapes[0]["raison"], "flux_interrompu")
        self.assertNotIn("tools", scene.client.appels[1])
        self.assertEqual(_textes(sortie), "Il fait beau [1].")


class ContenuMelangeTest(unittest.TestCase):

    def test_texte_avant_et_dans_le_meme_chunk_que_le_tool_call(self):
        args = '{"requete": "météo Lyon"}'
        premier = _chunk(content=" Je cherche.",
                         tool_calls=[_frag(0, "", id="call_M", name="web_search")])
        suite = [_chunk(tool_calls=[_frag(0, args[i:i + 4])]) for i in range(0, len(args), 4)]
        rounds = [
            [_chunk(content="Un instant."), premier] + suite
            + [_chunk(finish_reason="tool_calls"), _usage(60, 12)],
            _round_final(texte=" Il fait beau [1]."),
        ]
        with _Scene(rounds) as scene:
            sortie = scene.tour()
        self.assertEqual(scene.recherches, ["météo Lyon"])
        self.assertEqual(_textes(sortie), "Un instant. Je cherche. Il fait beau [1].")
        assistant = scene.client.appels[1]["messages"][-2]
        self.assertEqual(assistant["content"], "Un instant. Je cherche.")
        self.assertEqual(assistant["tool_calls"][0]["function"]["arguments"], args)


class HistoriqueInchangeTest(unittest.TestCase):

    def test_la_normalisation_historique_reste_role_content(self):
        """L'historique passé par l'appelant n'est ni muté ni élargi : seuls
        les messages construits pendant la boucle portent `tool_calls`/
        `tool_call_id`."""
        historique = [dict(m) for m in _HISTORIQUE]
        copie = json.loads(json.dumps(historique))
        rounds = [_round_outil([(0, "web_search", '{"requete": "a"}', "c1")]), _round_final()]
        with _Scene(rounds) as scene:
            moteur = LLMEngine(config_path=_CONFIG)
            list(moteur.stream(historique, model=f"lmstudio:{_MODELE}", outils=["web_search"]))
        self.assertEqual(historique, copie)
        envoyes = scene.client.appels[1]["messages"]
        self.assertEqual(envoyes[:len(_HISTORIQUE)],
                         [{"role": m["role"], "content": m["content"]} for m in _HISTORIQUE])


if __name__ == "__main__":
    unittest.main(verbosity=2)
