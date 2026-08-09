"""Synthèse Piper : le WAV renvoyé doit contenir de l'audio.

Ces tests existent à cause d'une panne qui ne se voyait pas. `PiperEngine`
appelait `PiperVoice.synthesize(text, wav)` — la signature de piper-tts 1.2. En
1.3, `synthesize` est devenu un **générateur** d'`AudioChunk` dont le 2e
paramètre est une `SynthesisConfig`, et l'écriture dans un fichier WAV est
passée dans `synthesize_wav`. Conséquences en cascade, toutes silencieuses :

* l'appel ne lève rien — il fabrique un générateur, qui n'est jamais consommé ;
* aucune trame n'est écrite, et le format WAV n'est jamais posé ;
* c'est `wave.close()` qui finit par lever « # channels not specified ».

Autrement dit : une rupture d'API amont se manifestait par une erreur du module
`wave` qui ne nommait ni piper, ni la voix, ni la version. `/voice/synthesize`
renvoyait 500 sur un modèle parfaitement chargé. Mesuré sur la machine d'Ilyann
avec piper-tts 1.4.2, en allant jusqu'au bout de la chaîne HTTP.

Le vrai modèle (76 Mo) n'est pas chargé ici : ce qu'on vérifie, c'est le contrat
entre `PiperEngine` et `PiperVoice`. La doublure reproduit la signature de
piper-tts 1.4.2 et **refuse** l'ancienne forme d'appel — un retour en arrière
échouerait donc au lieu de rendre un WAV vide.

Usage :
    python test_voice_synthese.py
"""

import io
import os
import sys
import unittest
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole les arbres AVANT tout import de core.*

from core.voice import PiperEngine  # noqa: E402


class _Config:
    sample_rate = 22050


class _FausseVoix:
    """Doublure fidèle au contrat de piper-tts 1.4.2 — y compris ses refus."""

    def __init__(self, chunks=(b"\x01\x02" * 100,)):
        self._chunks = chunks
        self.config = _Config()
        self.set_wav_format_recu = None

    def synthesize(self, text, syn_config=None, include_alignments=False):
        # L'ancien appel passait le Wave_write en 2e position. Piper ne s'en
        # plaindrait pas (il ne lit `syn_config` qu'à l'itération, qui n'a
        # jamais lieu) ; ici on le refuse pour que la régression soit visible.
        if isinstance(syn_config, wave.Wave_write):
            raise AssertionError(
                "PiperEngine appelle synthesize(text, wav) — signature de "
                "piper-tts 1.2, retirée depuis. Utiliser synthesize_wav."
            )
        yield from ()

    def synthesize_wav(self, text, wav_file, syn_config=None,
                       set_wav_format=True, include_alignments=False):
        self.set_wav_format_recu = set_wav_format
        for chunk in self._chunks:
            if set_wav_format:
                wav_file.setframerate(self.config.sample_rate)
                wav_file.setsampwidth(2)
                wav_file.setnchannels(1)
            wav_file.writeframes(chunk)


def _moteur(voix) -> PiperEngine:
    """PiperEngine sans disque ni réseau : seule `synthesize` est sous test."""
    moteur = PiperEngine.__new__(PiperEngine)
    moteur._voice = "fr_FR-upmc-medium"
    moteur._piper_voice = voix
    return moteur


class SyntheseTest(unittest.TestCase):
    def test_le_wav_contient_de_l_audio(self):
        voix = _FausseVoix()
        donnees = _moteur(voix).synthesize("bonjour")
        self.assertEqual(donnees[:4], b"RIFF")
        with wave.open(io.BytesIO(donnees), "rb") as w:
            self.assertGreater(w.getnframes(), 0, "WAV sans trame : la synthèse n'a rien produit")
            self.assertEqual(w.getframerate(), 22050)
            self.assertEqual(w.getnchannels(), 1)
            self.assertEqual(w.getsampwidth(), 2)

    def test_le_format_est_pose_par_epure_pas_par_piper(self):
        """`set_wav_format=False` n'est pas cosmétique — cf. le test suivant."""
        voix = _FausseVoix()
        _moteur(voix).synthesize("bonjour")
        self.assertIs(voix.set_wav_format_recu, False)

    def test_un_texte_sans_audio_rend_un_wav_vide_et_valide(self):
        """Piper ne pose le format qu'au 1er chunk : sans chunk, l'en-tête manque.

        Ponctuation seule, blancs, emoji — des entrées banales — produisaient un
        `wave.Error` remonté en 500. Poser le format nous-mêmes rend un WAV vide,
        qui est une réponse parfaitement valide.
        """
        donnees = _moteur(_FausseVoix(chunks=())).synthesize("...")
        self.assertEqual(donnees[:4], b"RIFF")
        with wave.open(io.BytesIO(donnees), "rb") as w:
            self.assertEqual(w.getnframes(), 0)
            self.assertEqual(w.getframerate(), 22050)

    def test_l_ancienne_signature_serait_detectee(self):
        """Contrôle du contrôle : la doublure refuse bien l'appel obsolète.

        Le WAV est fermé à la main, format posé d'avance, plutôt que par un
        `with` — sinon `Wave_write.__exit__` lève « # channels not specified »
        et **masque** l'AssertionError attendue. C'est exactement le masquage
        qui a caché le bug d'origine : il s'est reproduit en écrivant ce test.
        """
        voix = _FausseVoix()
        wav = wave.open(io.BytesIO(), "wb")
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(22050)
        try:
            with self.assertRaises(AssertionError):
                list(voix.synthesize("bonjour", wav))
        finally:
            wav.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
