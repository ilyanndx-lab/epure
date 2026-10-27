#!/usr/bin/env python3
"""Tests de `core.citations.verifier_fidelite` (#83) : ce qu'une phrase
rattachée à une source `[n]` affirme (nombres, citations entre guillemets) se
retrouve-t-il dans le passage RÉELLEMENT injecté pour cette source ?

Écrits avant le code et vus en échec. Deux familles :

  * les 8 réponses RÉELLES de l'étage 2 de #75 — référence
    (`tools/eval-lecture-url/resultats/resultats.json`, code de `main`) et
    variante (`resultats/experiences/etage2-variante.json`) — contre les
    extraits que le chat injecte pour ces quatre cas. Les extraits sont figés
    dans `resultats/fidelite/extraits.json` par
    `tools/eval-lecture-url/eval.py extraits` (code de l'étage 1, embedding
    local ; la CI n'a pas ce modèle, elle lit le fichier). Longueurs
    contrôlées contre l'étage 1 : 3 848 / 15 454 / 3 354 / 14 984 caractères ;
  * des cas synthétiques qui isolent chaque règle.

Un cas réel est « signalé » ou « non signalé » selon l'attendu de #83. Celui
de relativité 4k (non signalé, des deux côtés) n'est PAS une preuve de
fidélité : c'est une erreur d'interprétation (le modèle prend « classe des
référentiels inertiels » pour un nom), que cette heuristique ne voit pas par
construction — limite connue, écrite dans la docstring de `verifier_fidelite`.
"""

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401 — convention du dépôt, avant tout core.*

from core.citations import verifier_fidelite
from core.websearch import TRACE_MAX_ETAPES, ResultatWeb
from modules.chat import router as chat_router

RESULTATS = Path(__file__).resolve().parents[1] / "tools" / "eval-lecture-url" / "resultats"


def _charger(chemin):
    return json.loads((RESULTATS / chemin).read_text(encoding="utf-8"))


def _sources_reelles():
    """`{(page, budget): {1: titre + extrait}}` — le texte tel que
    `formater_pour_llm` l'injecte : titre puis extrait."""
    return {(c["page"], c["budget"]): {c["rang"]: f"{c['titre']} {c['extrait']}"}
            for c in _charger("fidelite/extraits.json")["cas"]}


def _reponses(chemin):
    return {(e["page"], e["budget"]): e["reponse"] for e in _charger(chemin)["essais"]}


class ExtraitsFigesTest(unittest.TestCase):
    """Les fixtures sont bien celles de l'étage 1 (extrait déterministe)."""

    def test_longueurs_identiques_a_l_etage_1(self):
        attendu = {("relativite_restreinte", 4000): 3848, ("relativite_restreinte", 16000): 15454,
                   ("photoelectrique", 4000): 3354, ("photoelectrique", 16000): 14984}
        reel = {(c["page"], c["budget"]): c["caracteres"] for c in _charger("fidelite/extraits.json")["cas"]}
        self.assertEqual(reel, attendu)
        for c in _charger("fidelite/extraits.json")["cas"]:
            self.assertEqual(len(c["extrait"]), c["caracteres"])


class ReponsesReellesEtage2Test(unittest.TestCase):
    """Les huit réponses réelles de l'étage 2."""

    @classmethod
    def setUpClass(cls):
        cls.sources = _sources_reelles()
        cls.reference = _reponses("resultats.json")
        cls.variante = _reponses("experiences/etage2-variante.json")

    def _signale(self, reponses, cle):
        return verifier_fidelite(reponses[cle], self.sources[cle])

    def _elements(self, reponses, cle):
        return {a.element for a in self._signale(reponses, cle)}

    def test_huit_reponses_presentes(self):
        self.assertEqual(len(self.reference), 4)
        self.assertEqual(len(self.variante), 4)

    def test_photoelectrique_4k_reference_signale_4_et_5(self):
        elements = self._elements(self.reference, ("photoelectrique", 4000))
        self.assertTrue({"4", "5"} <= elements, elements)

    def test_photoelectrique_4k_variante_signale_4_et_5(self):
        """Dans la variante la phrase « L'exposant varie entre 4 et 5 » n'a ni
        [n] ni formule d'attribution : elle hérite de l'attribution de la
        phrase précédente (« Selon les extraits fournis… »), même paragraphe."""
        elements = self._elements(self.variante, ("photoelectrique", 4000))
        self.assertTrue({"4", "5"} <= elements, elements)

    def test_photoelectrique_4k_les_mots_de_la_variante_sont_dans_l_extrait(self):
        """Précision utile à qui lit #83 : « électrons lents » et « ondes
        porteuses » figurent bel et bien dans l'extrait de 4 000 caractères
        (passages sans rapport avec Z). L'heuristique ne les signale donc pas :
        ce qui manque à l'extrait, ce sont les valeurs 4 et 5."""
        elements = self._elements(self.variante, ("photoelectrique", 4000))
        self.assertFalse(any("lents" in e or "porteuses" in e for e in elements), elements)

    def test_photoelectrique_4k_les_sources_rattachees_sont_listees(self):
        for reponses in (self.reference, self.variante):
            for a in self._signale(reponses, ("photoelectrique", 4000)):
                self.assertEqual(a.sources, [1])

    def test_photoelectrique_16k_non_signale(self):
        for reponses in (self.reference, self.variante):
            self.assertEqual(self._signale(reponses, ("photoelectrique", 16000)), [])

    def test_relativite_16k_non_signale(self):
        for reponses in (self.reference, self.variante):
            self.assertEqual(self._signale(reponses, ("relativite_restreinte", 16000)), [])

    def test_relativite_4k_non_signale(self):
        """Erreur d'interprétation, pas invention : limite connue (cf. docstring
        du module). Inclut une faute de frappe du modèle (« inertels »), tolérée
        comme transcription et non comme invention."""
        for reponses in (self.reference, self.variante):
            self.assertEqual(self._signale(reponses, ("relativite_restreinte", 4000)), [])


class NombresTest(unittest.TestCase):
    SRC = {1: "Titre — La valeur mesurée vaut 4.5 et la masse 4000 kg ; température −3 °C."}

    def _el(self, reponse, sources=None):
        return [a.element for a in verifier_fidelite(reponse, sources or self.SRC)]

    def test_nombre_absent_signale(self):
        self.assertEqual(self._el("La valeur vaut 42 [1]."), ["42"])

    def test_nombre_present_non_signale(self):
        self.assertEqual(self._el("La valeur vaut 4.5 [1]."), [])

    def test_virgule_decimale_contre_point(self):
        self.assertEqual(self._el("La valeur vaut 4,5 [1]."), [])

    def test_point_decimal_contre_virgule(self):
        self.assertEqual(self._el("La valeur vaut 4.5 [1].", {1: "elle vaut 4,5 ici"}), [])

    def test_en_toutes_lettres_contre_chiffre(self):
        self.assertEqual(self._el("Il y a quatre cas [1].", {1: "On compte 4 cas."}), [])

    def test_chiffre_contre_en_toutes_lettres(self):
        self.assertEqual(self._el("Il y a 4 cas [1].", {1: "On compte quatre cas."}), [])

    def test_nombre_en_toutes_lettres_absent_signale(self):
        self.assertEqual(self._el("Il y a douze cas [1].", {1: "On compte 4 cas."}), ["douze"])

    def test_un_et_une_ne_sont_pas_des_nombres(self):
        self.assertEqual(self._el("Une valeur et un cas [1].", {1: "rien"}), [])

    def test_espace_de_milliers(self):
        self.assertEqual(self._el("La masse est 4 000 kg [1]."), [])
        self.assertEqual(self._el("La masse est 4 000 kg [1]."), [])

    def test_signe_moins_unicode(self):
        self.assertEqual(self._el("La température est -3 °C [1]."), [])
        self.assertEqual(self._el("La température est −3 °C [1]."), [])
        self.assertEqual(self._el("La température est +3 °C [1]."), ["3"])  # le signe compte

    def test_exposants(self):
        src = {1: "précision de l'ordre de 10 −9 s"}
        self.assertEqual(self._el("Précision de 10⁻⁹ s [1].", src), [])
        self.assertEqual(self._el("Précision de 10^-7 s [1].", src), ["-7"])

    def test_intervalle_et_eclate(self):
        src = {1: "l'exposant varie de 4 à 5"}
        self.assertEqual(self._el("Il varie entre 4 et 5 [1].", src), [])
        self.assertEqual(self._el("Il varie de 4 à 5 [1].", src), [])
        self.assertEqual(self._el("Il varie de 4–5 [1].", src), [])
        self.assertEqual(self._el("Il varie de 4 à 7 [1].", src), ["7"])

    def test_le_numero_de_la_citation_n_est_pas_un_nombre(self):
        self.assertEqual(self._el("Voir la source [1].", {1: "aucun chiffre ici"}), [])
        self.assertEqual(self._el("Voir [12] et [1].", {1: "aucun chiffre ici"}), [])

    def test_marqueurs_de_note_de_la_source_ne_comptent_pas(self):
        src = {1: "Hertz en 1887 [ 1 ] , [ 4 ] , [ 5 ] , [ N 1 ] ."}
        self.assertEqual(sorted(self._el("Valeurs 4 et 5 [1].", src)), ["4", "5"])

    def test_nombres_dans_les_formules_non_verifies(self):
        """Limite v1 : $Z^4$ est masqué comme le reste des maths."""
        self.assertEqual(self._el("Elle croît comme $Z^4$ [1].", {1: "rien"}), [])
        self.assertEqual(self._el("Elle croît comme $$Z^5$$ [1].", {1: "rien"}), [])

    def test_nombres_dans_le_code_ignores(self):
        self.assertEqual(self._el("Voir `x = 99` [1].", {1: "rien"}), [])
        self.assertEqual(self._el("```python\nx = 99\n```\nOk [1].", {1: "rien"}), [])

    def test_nombre_dans_une_citation_verifie_par_la_citation(self):
        """Un nombre entre guillemets est vérifié comme partie de la citation,
        pas une seconde fois comme nombre isolé."""
        src = {1: "il a mesuré 4.5 mètres"}
        self.assertEqual(self._el("Il dit « mesuré 4,5 mètres » [1].", src), [])

    def test_liste_numerotee_n_est_pas_un_nombre(self):
        rep = "1.  Premier point [1]\n2.  Second point [1]"
        self.assertEqual(self._el(rep, {1: "premier point second point"}), [])


class CitationsEntreGuillemetsTest(unittest.TestCase):
    SRC = {1: "Le référentiel galiléen est appelé aussi « référentiel de Lorentz » ou référentiel inertiel : ensemble d'observateurs."}

    def _el(self, reponse, sources=None):
        return [a.element for a in verifier_fidelite(reponse, sources or self.SRC)]

    def test_citation_presente(self):
        self.assertEqual(self._el("Il est appelé « référentiel de Lorentz » [1]."), [])

    def test_citation_inventee_signalee(self):
        self.assertEqual(self._el("La page dit « le référentiel de Newton est absolu » [1]."),
                         ["le référentiel de Newton est absolu"])

    def test_guillemets_anglais(self):
        self.assertEqual(self._el("La page dit “référentiel de Lorentz” [1]."), [])
        self.assertEqual(self._el("La page dit “référentiel de Newton” [1]."), ["référentiel de Newton"])

    def test_casse_accents_espaces_normalises(self):
        self.assertEqual(self._el("Il est appelé « REFERENTIEL  de   lorentz » [1]."), [])

    def test_gras_a_l_interieur_de_la_citation(self):
        self.assertEqual(self._el("Il est appelé « **référentiel** de Lorentz » [1]."), [])

    def test_ellipse_decoupe_la_citation(self):
        self.assertEqual(self._el("Il dit « …appelé aussi référentiel de Lorentz […] ensemble d'observateurs » [1]."), [])
        self.assertEqual(self._el("Il dit « …appelé aussi référentiel de Lorentz […] ensemble de chevaux » [1]."),
                         ["ensemble de chevaux"])

    def test_guillemets_imbriques(self):
        rep = "> « …appelé aussi « **référentiel de Lorentz** » ou **« référentiel inertiel »** : ensemble d'observateurs. » [1]"
        self.assertEqual(self._el(rep), [])

    def test_faute_de_frappe_d_une_lettre_toleree(self):
        self.assertEqual(self._el("Il est appelé « référentiel de Lorenz » [1]."), [])

    def test_plusieurs_lettres_d_ecart_signalees(self):
        self.assertEqual(self._el("Il est appelé « référentiel de Lorntz » [1]."), [])  # une lettre en moins
        self.assertEqual(self._el("Il est appelé « référentiel de Lornzt » [1]."), ["référentiel de Lornzt"])

    def test_mots_presents_mais_ordre_different_signale(self):
        self.assertEqual(self._el("Il dit « Lorentz de référentiel » [1]."), ["Lorentz de référentiel"])


class RattachementTest(unittest.TestCase):
    SRC = {1: "La limite est 4 et la borne 5.", 2: "Une autre page : la limite vaut 9."}

    def _res(self, reponse, sources=None):
        return verifier_fidelite(reponse, sources or self.SRC)

    def test_phrase_sans_n_ni_attribution_non_verifiee(self):
        self.assertEqual(self._res("La limite vaut 42."), [])

    def test_phrase_avec_attribution_sans_n_rattachee_a_l_union(self):
        res = self._res("Selon les extraits fournis, l'exposant varie entre 4 et 12.")
        self.assertEqual([a.element for a in res], ["12"])
        self.assertEqual(res[0].sources, [1, 2])

    def test_nombre_present_dans_une_autre_source_de_l_union(self):
        self.assertEqual(self._res("D'après la page, la limite vaut 9."), [])

    def test_formules_d_attribution(self):
        for debut in ("Selon les extraits", "D'après la page", "Le texte indique que",
                      "D'après ces extraits", "Selon le texte fourni", "Les extraits précisent que",
                      "Dans l'extrait fourni"):
            with self.subTest(debut=debut):
                self.assertEqual([a.element for a in self._res(f"{debut}, la limite vaut 42.")], ["42"])

    def test_attribution_heritee_dans_le_meme_paragraphe(self):
        res = self._res("Selon les extraits fournis, la limite existe. Elle vaut entre 4 et 12.")
        self.assertEqual([a.element for a in res], ["12"])

    def test_attribution_non_heritee_au_paragraphe_suivant(self):
        self.assertEqual(self._res("Selon les extraits fournis, la limite existe.\n\nElle vaut 42."), [])

    def test_n_cite_restreint_aux_sources_citees(self):
        """Le 9 est dans la source 2 : une phrase qui ne cite que [1] n'en profite pas."""
        res = self._res("La limite vaut 9 [1].")
        self.assertEqual([a.element for a in res], ["9"])
        self.assertEqual(res[0].sources, [1])

    def test_n_hors_plage_seul_laisse_au_validateur_existant(self):
        self.assertEqual(self._res("La limite vaut 42 [7]."), [])

    def test_citation_apres_le_point_rattachee_a_la_phrase_precedente(self):
        res = self._res("La limite vaut 42. [1]")
        self.assertEqual([a.element for a in res], ["42"])

    def test_phrase_decoupee_au_bon_endroit(self):
        res = self._res("La limite vaut 4 [1]. La borne vaut 77 [1].")
        self.assertEqual([a.element for a in res], ["77"])
        self.assertIn("77", res[0].phrase)
        self.assertNotIn("limite", res[0].phrase)

    def test_decimal_ne_coupe_pas_la_phrase(self):
        res = self._res("La valeur 4.5 est proche [1].", {1: "valeur 4.5 mesurée"})
        self.assertEqual(res, [])

    def test_pas_de_sources_aucun_resultat(self):
        self.assertEqual(verifier_fidelite("Selon les extraits, 42 [1].", {}), [])

    def test_reponse_vide(self):
        self.assertEqual(verifier_fidelite("", self.SRC), [])

    def test_ne_modifie_rien(self):
        reponse = "Selon les extraits, 42 [1]."
        copie = str(reponse)
        verifier_fidelite(reponse, self.SRC)
        self.assertEqual(reponse, copie)

    def test_un_element_signale_une_seule_fois_par_phrase(self):
        res = self._res("La limite vaut 42 puis encore 42 [1].")
        self.assertEqual([a.element for a in res], ["42"])


class TraceNonEtayeesTest(unittest.TestCase):
    """Étape `citations_non_etayees` : réservée en fin de trace, jamais tronquée."""

    def _non_etayees(self):
        from core.citations import verifier_fidelite as v
        return v("La limite vaut 42 [1].", {1: "rien"})

    def test_etape_construite(self):
        trace = chat_router._construire_trace_finale([], None, False, self._non_etayees())
        self.assertEqual(len(trace), 1)
        etape = trace[0]
        self.assertEqual(etape["etape"], "citations_non_etayees")
        self.assertEqual(etape["affirmations"], [
            {"phrase": "La limite vaut 42 [1].", "element": "42", "type": "nombre", "sources": [1]},
        ])

    def test_pas_d_etape_sans_affirmation(self):
        self.assertEqual(chat_router._construire_trace_finale([], None, False, []), [])

    def test_place_reservee_apres_citations_invalides(self):
        from core.citations import RapportCitations
        rapport = RapportCitations(rangs_hors_plage=[7])
        beaucoup = [{"etape": "recherche_debut", "requete": str(i)} for i in range(TRACE_MAX_ETAPES + 5)]
        trace = chat_router._construire_trace_finale(beaucoup, rapport, True, self._non_etayees())
        self.assertEqual(len(trace), TRACE_MAX_ETAPES)
        self.assertEqual([e["etape"] for e in trace[-2:]], ["citations_invalides", "citations_non_etayees"])

    def test_etape_seule_sans_beaucoup_d_etapes(self):
        beaucoup = [{"etape": "recherche_debut", "requete": str(i)} for i in range(TRACE_MAX_ETAPES + 5)]
        trace = chat_router._construire_trace_finale(beaucoup, None, True, self._non_etayees())
        self.assertEqual(len(trace), TRACE_MAX_ETAPES)
        self.assertEqual(trace[-1]["etape"], "citations_non_etayees")

    def test_liste_bornee(self):
        from core.citations import verifier_fidelite as v
        rep = " ".join(f"Valeur {1000 + i} [1]." for i in range(30))
        trace = chat_router._construire_trace_finale([], None, False, v(rep, {1: "rien"}))
        self.assertEqual(len(trace[0]["affirmations"]), 10)

    def test_finalisation_de_bout_en_bout_utilise_le_texte_injecte(self):
        resultats = [ResultatWeb(rang=1, titre="Page", url="https://exemple.org/p",
                                 extrait="La limite vaut 4.", moteur="lecture")]
        _, trace = chat_router._finaliser_citations_et_trace(
            "c1", "La limite vaut 4 [1]. Elle monte à 42 [1].", resultats, "", set(), [],
        )
        self.assertEqual([e["etape"] for e in trace], ["citations_non_etayees"])
        self.assertEqual([a["element"] for a in trace[0]["affirmations"]], ["42"])

    def test_finalisation_sans_affirmation_non_etayee_trace_inchangee(self):
        resultats = [ResultatWeb(rang=1, titre="Page", url="https://exemple.org/p",
                                 extrait="La limite vaut 4.", moteur="lecture")]
        _, trace = chat_router._finaliser_citations_et_trace(
            "c1", "La limite vaut 4 [1].", resultats, "", set(), [],
        )
        self.assertEqual(trace, [])


if __name__ == "__main__":
    unittest.main()
