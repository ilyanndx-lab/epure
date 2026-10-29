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
  * les 20 réponses des tirages de fidélité (`resultats/fidelite/tirages.json`,
    5 tirages × 4 cas, 2026-10-29), rejouées hors ligne avec le message de
    l'utilisateur tel que l'éval l'a envoyé (question + URL) ;
  * des cas synthétiques qui isolent chaque règle.

Un cas réel est « signalé » ou « non signalé » selon l'attendu de #83, revu
sur #85 : la réponse de RÉFÉRENCE relativité 4k est signalée (sa citation
« dans tous les référentiels inertels (ou inertiels) » remplace « galiléens »
par un mot inventé — la tolérance d'un mot par tranche de cinq, qui la
laissait passer, est retirée). La VARIANTE relativité 4k reste non signalée,
ce qui n'est PAS une preuve de fidélité : son erreur est d'interprétation (elle
prend « inertiels » pour un nom), que cette heuristique ne voit pas par
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


def _messages_utilisateur():
    """`{page: message}` — question + URL, comme `eval.py` l'envoie au modèle."""
    questions = json.loads((RESULTATS.parent / "questions.json").read_text(encoding="utf-8"))
    return {q["page"]: f"{q['question']} {q['url']}" for q in questions}


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
        cls.messages = _messages_utilisateur()

    def _signale(self, reponses, cle):
        return verifier_fidelite(reponses[cle], self.sources[cle], texte_utilisateur=self.messages[cle[0]])

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

    def test_relativite_4k_reference_signale_la_citation_alteree(self):
        """« dans tous les référentiels inertels (ou inertiels) » : l'extrait dit
        « galiléens (ou inertiels ) ». Un mot remplacé dans une citation est
        une altération, signalée. La citation courte « référentiels inertels »
        du premier paragraphe, elle, n'est qu'à une lettre de « référentiels
        inertiels » (présent dans l'extrait) : tolérée comme faute de frappe."""
        signalees = self._signale(self.reference, ("relativite_restreinte", 4000))
        self.assertEqual([(a.type, a.element) for a in signalees],
                         [("citation", "dans tous les référentiels inertels (ou inertiels)")])
        self.assertEqual(signalees[0].sources, [1])

    def test_relativite_4k_variante_non_signale(self):
        """Erreur d'interprétation, pas invention : limite connue (cf. docstring
        du module) — les mots cités sont dans l'extrait."""
        self.assertEqual(self._signale(self.variante, ("relativite_restreinte", 4000)), [])


class TiragesFideliteTest(unittest.TestCase):
    """Les 20 tirages du 2026-10-29, rejoués sans modèle. Attendus posés par
    Ilyann sur #85 après lecture des réponses brutes."""

    @classmethod
    def setUpClass(cls):
        cls.sources = _sources_reelles()
        cls.messages = _messages_utilisateur()
        cls.essais = {(e["page"], e["budget"], e["tirage"]): e
                      for e in _charger("fidelite/tirages.json")["essais"]}

    def _signale(self, page, budget, tirage):
        e = self.essais[(page, budget, tirage)]
        return verifier_fidelite(e["reponse"], self.sources[(page, budget)],
                                 texte_utilisateur=self.messages[page])

    def test_vingt_tirages(self):
        self.assertEqual(len(self.essais), 20)

    def test_les_mots_de_la_question_ne_sont_jamais_signales(self):
        """« deux autres noms », cité entre guillemets par le modèle, et
        « deux » viennent de la question, pas de la source."""
        for (page, budget, tirage) in self.essais:
            with self.subTest(page=page, budget=budget, tirage=tirage):
                elements = {a.element for a in self._signale(page, budget, tirage)}
                self.assertNotIn("deux autres noms", elements)
                self.assertNotIn("deux", elements)

    def test_photoelectrique_4k_t3_signale(self):
        """« entre 3 et 4 » dans une phrase qui ne cite que [4] et [5], hors
        liste (renvois de notes de la page recopiés), et dans la conclusion
        « Réponse basée sur l'analyse des extraits »."""
        self.assertIn("4", {a.element for a in self._signale("photoelectrique", 4000, 3)})

    def test_photoelectrique_4k_t5_signale(self):
        """« D'après les lois physiques sous-jacentes évoquées dans ces textes,
        … entre 4 et 5 »."""
        elements = {a.element for a in self._signale("photoelectrique", 4000, 5)}
        self.assertTrue({"4", "5"} <= elements, elements)

    def test_photoelectrique_4k_tous_les_tirages_signales(self):
        for tirage in range(1, 6):
            with self.subTest(tirage=tirage):
                self.assertTrue(self._signale("photoelectrique", 4000, tirage))

    def test_relativite_16k_t2_et_t4_signales(self):
        """« (ou simplement « inerte ») » : mot absent de l'extrait."""
        for tirage in (2, 4):
            with self.subTest(tirage=tirage):
                self.assertIn("inerte", {a.element for a in self._signale("relativite_restreinte", 16000, tirage)})

    def test_photoelectrique_4k_apres_t2_t3_t4_rien_sur_les_phrases_marquees(self):
        """Mesure « après » de #86 (`tirages-non-invention.json`) : les
        valeurs que le modèle annonce « hors extrait » ne sont plus signalées."""
        apres = {e["tirage"]: e for e in _charger("fidelite/tirages-non-invention.json")["essais"]
                 if e["page"] == "photoelectrique" and e["budget"] == 4000}
        for tirage in (2, 3, 4):
            with self.subTest(tirage=tirage):
                e = apres[tirage]
                self.assertEqual(verifier_fidelite(e["reponse"], self.sources[("photoelectrique", 4000)],
                                                   texte_utilisateur=self.messages["photoelectrique"]), [])

    def test_photoelectrique_16k_jamais_signale(self):
        for tirage in range(1, 6):
            with self.subTest(tirage=tirage):
                self.assertEqual(self._signale("photoelectrique", 16000, tirage), [])


class MessageUtilisateurTest(unittest.TestCase):
    """Ce que l'utilisateur a écrit n'est pas une invention du modèle."""

    def _el(self, reponse, user, sources=None):
        return [a.element for a in verifier_fidelite(reponse, sources or {1: "rien"}, texte_utilisateur=user)]

    def test_nombre_du_message_etaye(self):
        self.assertEqual(self._el("Il y a 12 cas [1].", ""), ["12"])
        self.assertEqual(self._el("Il y a 12 cas [1].", "Pourquoi 12 cas ?"), [])

    def test_nombre_en_lettres_du_message_etaye(self):
        self.assertEqual(self._el("Il cite deux autres noms [1].", "Quels sont les deux autres noms ?"), [])
        self.assertEqual(self._el("Il cite 2 noms [1].", "Quels sont les deux autres noms ?"), [])

    def test_citation_reprise_du_message_etayee(self):
        self.assertEqual(self._el("La page ne donne pas « deux autres noms » [1].",
                                  "D'après cette page, quels sont les deux autres noms ?"), [])

    def test_citation_absente_du_message_et_de_la_source_signalee(self):
        self.assertEqual(self._el("Elle dit « trois noms distincts » [1].", "Quels sont les deux autres noms ?"),
                         ["trois noms distincts"])

    def test_les_nombres_d_une_url_du_message_ne_comptent_pas(self):
        """Une URL désigne la page, elle n'affirme rien : « 2013 » dans un
        chemin n'étaye pas « 2013 » dans la réponse."""
        self.assertEqual(self._el("Prototype de 2013 [1].", "Lis https://exemple.org/2013/turing"), ["2013"])


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

    def test_un_mot_remplace_signale_meme_dans_une_longue_citation(self):
        """Aucune tolérance au niveau du mot (`_MOTS_DIFFERENTS_PAR_CINQ = 0`) :
        un seul mot changé sur huit suffit à signaler."""
        self.assertEqual(self._el("Il dit « le référentiel galiléen est appelé aussi référentiel de Newton » [1]."),
                         ["le référentiel galiléen est appelé aussi référentiel de Newton"])

    def test_citation_exacte_coupee_par_une_ellipse_non_signalee(self):
        """Chaque fragment autour de « … » ou « ... » est cherché séparément :
        une citation exacte qui saute un passage n'est pas une altération."""
        for ellipse in ("…", "...", " … ", " ... "):
            with self.subTest(ellipse=ellipse):
                rep = f"Il dit « Le référentiel galiléen est appelé{ellipse}ensemble d'observateurs » [1]."
                self.assertEqual(self._el(rep), [])

    def test_fragment_altere_apres_une_ellipse_signale_seul(self):
        rep = "Il dit « Le référentiel galiléen est appelé ... ensemble de chevaux » [1]."
        self.assertEqual(self._el(rep), ["ensemble de chevaux"])

    def test_mots_presents_mais_ordre_different_signale(self):
        self.assertEqual(self._el("Il dit « Lorentz de référentiel » [1]."), ["Lorentz de référentiel"])


class HorsExtraitTest(unittest.TestCase):
    """#86 : une phrase marquée « hors extrait » se déclare elle-même non
    sourcée — la consigne de non-invention le demande — et n'est pas vérifiée."""
    SRC = {1: "La limite est 4 et la borne 5.", 2: "Une autre page : la limite vaut 9."}

    def _el(self, reponse):
        return [a.element for a in verifier_fidelite(reponse, self.SRC)]

    def test_phrase_marquee_exclue(self):
        for phrase in ("Hors extrait, d'après mes connaissances : la limite vaut 42 [1].",
                       "HORS EXTRAIT : la limite vaut 42 [1].",
                       "Hors des extraits, la limite vaut 42 [1].",
                       "Hors de l'extrait, la limite vaut 42 [1].",
                       "La limite vaut 42 (hors extrait) [1].",
                       "*Hors extrait, d’après mes connaissances générales : la limite vaut 42.*",
                       "Selon les extraits fournis, hors extrait, la limite vaut 42."):
            with self.subTest(phrase=phrase):
                self.assertEqual(self._el(phrase), [])

    def test_phrase_suivante_verifiee_normalement(self):
        self.assertEqual(self._el("Hors extrait : la limite vaut 42. Elle vaut 77 [1]."), ["77"])

    def test_la_marque_ne_s_herite_pas(self):
        """L'attribution d'une phrase précédente traverse la phrase marquée :
        celle-ci n'en ouvre ni n'en ferme aucune."""
        self.assertEqual(self._el("Selon les extraits, la limite existe. Hors extrait, elle vaut 42. "
                                  "Elle vaut 77."), ["77"])

    def test_la_phrase_marquee_n_ouvre_pas_d_attribution(self):
        """« Hors extrait (les extraits sont concis), … » contient « extraits » :
        ce n'est pas pour autant une invocation de la source."""
        self.assertEqual(self._el("Hors extrait (les extraits sont concis), l'exposant vaut 42. Il vaut 77."), [])

    def test_mot_extrait_seul_n_est_pas_la_marque(self):
        self.assertEqual(self._el("L'extrait dit 42 [1]."), ["42"])


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

    def test_attribution_par_un_nom_designant_la_source(self):
        """Toute phrase qui nomme la source — « extrait(s) », « texte(s) »,
        « page », « source(s) », « document » — s'y rattache. Deux formules
        réelles (photoélectrique 4k, t3 et t5) en tête."""
        for phrase in ("Réponse basée sur l'analyse des extraits : la limite vaut 42.",
                       "D'après les lois physiques sous-jacentes évoquées dans ces textes, la limite vaut 42.",
                       "Cet extrait laisse penser que la limite vaut 42.",
                       "Le texte original donne une limite de 42.",
                       "Sur cette page, la limite vaut 42.",
                       "Les sources convergent vers 42.",
                       "La source mentionne 42.",
                       "Le document donne 42."):
            with self.subTest(phrase=phrase):
                res = self._res(phrase)
                self.assertEqual([(a.element, a.sources) for a in res], [("42", [1, 2])])

    def test_attribution_par_nom_heritee_jusqu_au_premier_n(self):
        res = self._res("Réponse basée sur l'analyse des extraits : la limite existe. Elle vaut 42. "
                        "Elle vaut 4 [1]. Elle monte à 77.")
        self.assertEqual([a.element for a in res], ["42"])

    def test_nom_de_source_entre_guillemets_n_attribue_pas(self):
        """Le mot est dans une citation, pas dans le discours du modèle."""
        self.assertEqual(self._res("Le titre « Texte intégral » vaut 42."), [])

    def test_attribution_heritee_dans_le_meme_paragraphe(self):
        res = self._res("Selon les extraits fournis, la limite existe. Elle vaut entre 4 et 12.")
        self.assertEqual([a.element for a in res], ["12"])

    def test_attribution_heritee_s_arrete_au_premier_n_explicite(self):
        """Après un [n] explicite, le modèle a repris la main sur ses renvois :
        une phrase suivante sans [n] n'est plus rattachée d'office."""
        res = self._res("Selon les extraits fournis, la limite existe. Elle vaut 4 [1]. Elle monte à 42.")
        self.assertEqual(res, [])

    def test_le_n_explicite_qui_arrete_l_heritage_reste_verifie(self):
        res = self._res("Selon les extraits fournis, la limite existe. Elle vaut 42 [1]. Elle monte à 77.")
        self.assertEqual([(a.element, a.sources) for a in res], [("42", [1])])

    def test_attribution_avec_n_dans_la_meme_phrase_ne_se_prolonge_pas(self):
        self.assertEqual(self._res("Selon la page [1], la limite existe. Elle vaut 42."), [])

    def test_attribution_non_heritee_au_paragraphe_suivant(self):
        self.assertEqual(self._res("Selon les extraits fournis, la limite existe.\n\nElle vaut 42."), [])

    def test_n_cite_restreint_aux_sources_citees(self):
        """Le 9 est dans la source 2 : une phrase qui ne cite que [1] n'en profite pas."""
        res = self._res("La limite vaut 9 [1].")
        self.assertEqual([a.element for a in res], ["9"])
        self.assertEqual(res[0].sources, [1])

    def test_n_tous_hors_liste_verifie_contre_l_union(self):
        """Renvois de notes de la page recopiés comme des [n] (photoélectrique
        4k, t3 à t5) : la phrase ne se rattache à aucune source offerte, elle
        est comparée à toutes. `valider_citations` signale en plus le rang."""
        res = self._res("La limite vaut 42 [7].")
        self.assertEqual([(a.element, a.sources) for a in res], [("42", [1, 2])])
        self.assertEqual(self._res("La limite vaut 9 [7] et 4 [8]."), [])

    def test_n_hors_liste_mele_a_un_n_valide_restreint_au_valide(self):
        res = self._res("La limite vaut 9 [1] [7].")
        self.assertEqual([(a.element, a.sources) for a in res], [("9", [1])])

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
