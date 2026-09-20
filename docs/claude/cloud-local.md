# Épure — « Le cloud ne part jamais sans qu'on l'ait demandé », détail

> Extrait de `CLAUDE.md` (§3.7), déplacé ici le 2026-09-20 pour alléger
> le noyau chargé à chaque session. Contenu inchangé, seul l'emplacement a
> changé — voir `CLAUDE.md` pour le renvoi et la règle résumée.

---

### 3.7 Le cloud ne part jamais sans qu'on l'ait demandé

**IMPÉRATIF — une tâche qui n'est pas le tour de chat de l'utilisateur tourne en
LOCAL.** Elle ne part vers un fournisseur distant que sur un choix explicite
*pour cette tâche précise*, jamais en héritant de `modèle_actif` : celui-là est
un choix fait pour **répondre à un message**, pas un mandat sur tout ce que
l'instance fait en arrière-plan.

Ce que six sites faisaient avant le 2026-08-24, en lisant `ctx["modèle_actif"]` :
choisir Groq ou Gemini pour discuter suffisait à envoyer le contenu des fiches
(12 000 caractères pour `/skills/résumé`), celui des fichiers importés,
14 000 caractères de cours (flashcards), les réponses de kholle avec le contexte
mémoire, et le profil de révision — lacunes confirmées comprises. Deux autres
partaient en cloud **en dur** : la classification du palier Adaptatif (avant
chaque message) et la réflexion de l'agent de code.

Le contrat, dans `core/instance.py` :

| fonction | rôle |
|---|---|
| `modele_local_defaut()` | `providers.local` (le réglage) → `config.yaml` → `_DEFAULT_LOCAL_MODEL`. **Le seul point de lecture** : `self._llm._model` était lu en dur à 5 endroits, tous hors du réglage. |
| `modele_pour_tache(use_cloud, modele_cloud, cle_env)` | `False` → local ; `True` → le modèle **nommé pour la tâche**, si sa clé est là, sinon repli local. |
| `est_modele_cloud(id)` | préfixe comparé à `_FOURNISSEURS_CLOUD`, jamais la présence d'un « : » — `qwen2.5:7b` en contient un. **`flm` est LOCAL** (le NPU de la machine). |

Trois règles qui se déduisent mal :

- **`use_cloud=True` ne veut pas dire « le modèle du chat »** mais « le modèle
  décidé pour cette tâche ». Le module Docs avait le drapeau et visait quand même
  `modèle_actif` : le garde-fou existait et ne gardait rien.
- **Pas de `use_cloud` là où l'utilisateur ne peut pas le poser.** Résumé
  d'import, plan de révision (`GET`, sans corps) : toujours local. Un drapeau sans
  interface pour le poser est une option que personne ne peut atteindre.
- **Jamais `None` comme modèle de tâche de fond** : `LLMEngine` retombe alors sur
  `config.yaml`, donc contourne le réglage sans que le site d'appel le sache.

**Volontairement HORS de cette règle** : les paliers Medium/High de
l'orchestrateur, dont les défauts cloud (Groq, Gemini) sont le but assumé du
palier et sont modifiables dans l'interface — c'est un choix de l'utilisateur,
pas une tâche de fond. Et l'Atelier, qui a sa propre configuration
(`providers.actif` d'instance).

Verrouillé par `test_taches_locales.py`, qui pose le pire cas — `modèle_actif`
cloud **et** toutes les clés d'API présentes — avant chaque vérification.
