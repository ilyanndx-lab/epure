# Épure — Tool-calling natif (détail)

Annexe de CLAUDE.md §3.9. Le noyau donne les règles ; ici, le détail, les
chiffres et les mesures. Code : `backend/core/llm.py` (registre `_SKILLS`,
`_stream_ollama`, `_stream_openai`), `backend/modules/chat/router.py`
(construction de `outils`/`budgets_override`), réglages persistants dans
`core/memory.py` (`tool_calling`, `normaliser_tool_calling`).

## 1. Ce que c'est, et ce que ce n'est pas

Le MODÈLE décide, en cours de génération, d'appeler un outil. C'est un
mécanisme **distinct** du classificateur heuristique (`@web`,
`core/websearch.py::detecter_intention_recherche`) et de l'injection eager de
`@historique`, qui agissent AVANT le tour sans que le modèle ait son mot à
dire. Les deux coexistent volontairement (§5).

Seul le tour de chat direct passe des outils (`LLMEngine.stream(outils=…)`).
Les onze autres appelants de `stream()` (résumés, agent de code, Atelier…) ne
passent rien : `outils=None` est le comportement historique, à l'octet.

## 2. Outils du registre

| Outil | Ce qu'il fait | Réseau | Disque / exécution | Budget défaut | Citable `[n]` |
|---|---|---|---|---|---|
| `web_search` | DuckDuckGo + lecture des pages (`core.websearch`, `core.webcontent`) | oui | cache mémoire seulement (5 min, 64 entrées) | 2 | oui |
| `recherche_approfondie` | même exécuteur, schéma qui pousse à itérer | oui | idem | 4 (réglable 1–10), **désactivée par défaut** | oui |
| `history_search` | `HistoryEngine.search_history` (store vectoriel) | non en régime normal — cf. §7 | lecture | 2 | non |
| skills personnalisés agentiques | renvoient le texte d'instruction écrit par l'utilisateur | non | aucun | 4 par skill (1–10) | non |

Réglages : Réglages › Tool calling (interrupteur général + par skill + budget
de `recherche_approfondie`), persistés (`_CLES_PERSISTANTES`).

**`recherche_approfondie` désactivée par défaut depuis le 2026-09-23.**
Activée, elle était proposée à CHAQUE tour direct et ajoutait jusqu'à 4
recherches aux 2 de `web_search`. Le bouton « Recherche approfondie » du chat
la force pour UN message (`deep_search_override`), même désactivée, même avec
l'interrupteur général coupé, avec le budget configuré. Un
`context_session.json` qui porte déjà `"enabled": true` le garde : la fusion ne
réécrit jamais une valeur présente. Verrouillé par `test_tool_calling_defauts.py`.

## 3. Fournisseurs et détection de capacité

**Seuls Ollama et LM Studio reçoivent des outils.** Les six autres
fournisseurs de `_stream_openai` (groq, cerebras, mistral, nvidia, deepseek,
flm) et Gemini envoient le corps d'avant à l'octet — verrouillé par
`test_tool_calling_lmstudio.NonRegressionSixFournisseursTest`.

Règle commune : **on n'expose jamais un outil à un modèle sans savoir qu'il le
supporte.** Absent, injoignable ou inconnu = aucun outil.

| Fournisseur | Source de la capacité | Cache |
|---|---|---|
| Ollama | `capabilities` contient `tools` dans `/api/tags` (`core.ollama_memoire.capacites_installees`) | 30 s |
| LM Studio | `capabilities.trained_for_tool_use is True` dans `/api/v1/models` (`core.models.capacites_outils_lmstudio`) | 30 s |

LM Studio — mesuré le 2026-09-22 : `/api/v0/models` (`capabilities:
["tool_use"]`, absent sinon) et `/api/v1/models` (`true`/`false` explicite)
concordaient sur les 7 modèles du poste ; v1 est lu parce qu'il dit « non »
explicitement. Son mode d'outils « par défaut » pour les modèles non
entraînés (prompt injecté, parsing de `[TOOL_REQUEST]…[END_TOOL_REQUEST]`,
« les résultats varient selon le modèle » d'après sa doc) est **filtré** —
non rejoué sur un vrai modèle.

Différences de protocole, toutes mesurées : Ollama rend `tool_calls` en UN
chunk, arguments déjà en dict, corrélation par `tool_name` (pas d'`id`).
LM Studio diffuse des deltas OpenAI FRAGMENTÉS — `id`/`name` dans le premier
fragment seulement, `arguments` token par token, fin sur `finish_reason:
"tool_calls"` — accumulés par `index`, JSON parsé à la fin, corrélation par
`tool_call_id`. Le dispatch est commun : `_executer_appels_outil`.

## 4. Budgets, plafond d'allers-retours, pire cas

Chaque skill a son budget d'INVOCATIONS (pas de rounds), indépendant des
autres. En plus, borne dure `_plafond_rounds_outils` = somme des budgets + 1
round de conclusion (`_schemas_du_round` retire les outils au dernier round et
émet `tool_call_plafond_atteint`) : un outil INCONNU ne décrémente aucun
budget, et sans ce plafond un petit modèle qui l'appelait en boucle ne
s'arrêtait jamais. Ollama garde en plus UN round de grâce quand le modèle émet
un `tool_calls` alors qu'aucun outil ne lui est offert (il reçoit « budget
épuisé »/« outil inconnu » puis conclut) : pire cas Ollama = plafond + 1.

Pire cas par tour (bornes tirées du code, pas une mesure) :

| Configuration | Invocations max | Allers-retours LLM max | Recherches web max |
|---|---|---|---|
| défaut (web 2 + historique 2) | 4 | 5 (6 sur Ollama) | 2 (+1 si `@web`) |
| + recherche approfondie (bouton ou réglage, budget 4) | 8 | 9 (10) | 6 (+1 classificateur, que le bouton déclenche toujours) |
| chaque skill personnalisé agentique | +budget (≤ 10) | +budget | — |

Une recherche : API DuckDuckGo puis repli HTML (8 s de timeout chacune), puis
jusqu'à 5 pages lues en parallèle (3 s chacune, 6 s pour l'étape) — **≈ 22 s
au pire par recherche**. Chaque aller-retour renvoie le prompt ENTIER
augmenté des résultats précédents, avec son propre plafond de tokens
(`num_predict`/`max_tokens` par round) : `prompt_tokens` du `__stats__` est la
somme (ce qui se facture), `contexte_tokens` le dernier round. Mesuré : 15–25 s
par tour avec un appel `web_search` (intégration Ollama) ; ~50 s pour la
suite d'intégration LM Studio complète.

## 5. Coexistence avec le classificateur heuristique

Le classificateur (`@web`, ou détection d'intention) cherche AVANT le tour et
injecte son contexte dans le prompt ; le tool-calling peut chercher PENDANT.
Les deux peuvent agir sur le même tour : `rang_web_existant` renumérote les
résultats de l'outil à la suite de ceux du classificateur, pour que deux
résultats ne partagent jamais un rang `[n]` (les citations se résolvent par
rang). Les résultats des deux origines finissent dans `web_resultats` (via la
sentinelle `__tool_call__`, skills `citable` seulement) et sont validés par
`core.citations` de la même façon. Aucune des deux ne désactive l'autre. Le
bouton « Recherche approfondie » pose toujours `web_search_override` : le
classificateur part donc aussi, en plus de l'outil là où il est câblé, seul
ailleurs.

Trace (panneau de recherche) : `tool_call_<nom>` (émis par l'exécuteur),
`tool_call_abandonne` (LM Studio, appel illisible), `tool_call_plafond_atteint`
— tous poussés avec `conversation_id` (§3.6). L'en-tête replié dit « Recherche
abandonnée » / « Limite de recherches atteinte » plutôt que « Recherche
web… » quand aucune recherche n'a abouti.

## 6. LM Studio : appel d'outil cassé → continuation

JSON d'arguments invalide (petit modèle) ou flux coupé en plein appel : tout
le round est écarté (l'API exige un `role="tool"` par `tool_call_id`), étape
`tool_call_abandonne`, puis un round SANS outil. Si une phrase était déjà
partie (« Je vais chercher… »), ce round **continue** le message assistant au
lieu de le répéter (`_continuer_sans_outil`), avec une consigne système
(`_CONSIGNE_SANS_OUTIL`). Pas de relance sur `finish_reason: "length"`.

**Mesuré sur UN seul modèle** (`mistralai/ministral-3-3b`, LM Studio du poste,
2026-09-22) et appliqué à tous les modèles LM Studio entraînés aux outils :
continuation native d'un dernier message `assistant`, sans drapeau (3/3 à
température 0) ; scénario réel à 0,7 avec prompt système, 30 tirages : 0
répétition du préambule, 0 pseudo-appel d'outil (comptage par motif). Sans
consigne, le modèle promettait parfois encore une recherche (lecture des
sorties, pas un comptage). Rejouable : `integration_tool_calling_lmstudio.py`.
À re-mesurer avant de supposer le même comportement d'un autre modèle.

## 7. Téléchargement automatique du modèle d'embedding

`history_search` — comme le reclassement des passages web de `web_search` —
passe par le store vectoriel (`core.runtime.vector_store`), dont la
construction appelle `core.embedding_install.exiger_pile()`. Si le modèle
d'embedding (~90 Mo, `model.onnx`) est absent, cela **lance son
téléchargement en arrière-plan** (réseau + écriture dans
`resolve_embedding_dir()`), une fois par process — sauf si
`EPURE_EMBEDDING_AUTOINSTALL=0` (valeur par défaut : activé ; `_test_env.py`
le coupe pour la suite de tests). Le tour, lui, ne bloque pas :
`search_history` rend `[]` et le reclassement est ignoré tant que le modèle
n'est pas là.
