# Épure — SSE et WebSocket, détail

> Extrait de `CLAUDE.md` (§3.6), déplacé ici le 2026-09-20 pour alléger
> le noyau chargé à chaque session. Contenu inchangé, seul l'emplacement a
> changé — voir `CLAUDE.md` pour le renvoi et la règle résumée.

---

### 3.6 SSE et WebSocket

**IMPÉRATIF — chaque trame de `/ws/chat` porte `conversation_id`.** Une seule
connexion WebSocket sert TOUTES les conversations d'un onglet (voulu — on ne
la ferme/rouvre pas à chaque bascule de fil), donc rien dans le protocole
n'identifiait, avant le 2026-09-15, à quel fil appartenait un `token` : basculer
vers une conversation B pendant qu'une conversation A générait encore laissait
le texte de A s'accumuler dans l'écran de B. Chaque
`websocket.send_text(json.dumps({...}))` de `modules/chat/router.py` porte
donc `conversation_id`, capturé au moment de l'émission (jamais relu depuis un
état mutable) ; le frontend le compare à une ref synchrone du fil affiché
(`conversationIdRef`, `Component.tsx`) et ignore tout événement qui ne
correspond pas, AVANT toute mutation d'état — un `conversation_id` absent ou
vide reste permissif (appliqué quand même), pour ne jamais faire disparaître
en silence un événement dont le serveur ne peut identifier le fil avec
certitude. Verrouillé côté serveur par `test_chat_conversation_id_trames.py`,
côté client par `frontend/src/modules/chat/Component.conversation.test.tsx`
(fuite, troncature au retour avant `done`, cohérence des stats).

Non couvert — deux limites, **ni l'une ni l'autre ne réintroduit de fuite
entre conversations** :

- **Une comparaison multi-modèles abandonnée par navigation reste orpheline.**
  `comparaisonUserMsgIdxRef` est remis à `-1` par `ouvrirConversation`/
  `nouvelleConversation` à CHAQUE changement de fil — défaut **préexistant** à
  ce correctif, pas introduit par lui. Conséquence : quitter un fil en pleine
  comparaison puis y revenir ne laisse plus aucun index valide, donc le
  panneau (jamais persisté sur disque) disparaît à la relecture, les
  `compare_token` qui continuent d'arriver sont silencieusement ignorés (déjà
  gardés par `if (!bloc...) return prev` avant ce correctif), et le bouton de
  résolution (`compare_choix`) n'existe plus. Une comparaison orpheline et
  perdue, PAS un texte tronqué affiché comme complet, et aucune fuite : le cas
  `idxComparaison >= 0 ET etaitSuspecte` (dans le handler `done`) est
  structurellement inatteignable, puisque toute bascule de fil remet l'index
  à `-1` avant qu'aucun événement ne puisse être marqué suspect pour ce fil.
- **Une course rare, celle-ci introduite par la relecture disque de ce
  correctif** : une action prise (nouveau message, nouvelle comparaison) dans
  la fenêtre étroite où une relecture déclenchée par un `done` suspect est
  encore en vol peut voir son ajout optimiste écrasé par le résultat (périmé)
  de cette relecture. Toujours la MÊME conversation ; le tour suivant (ses
  propres événements) répare l'affichage, et le message est de toute façon
  déjà persisté côté serveur — jamais perdu, juste retardé à l'écran.

**Ce que `LLMEngine.stream()` yielde** : du `str` pour le texte, et des **dicts
sentinelles** pour le reste — `{"__stats__": True, …}` (tokens et durées) et
`{"__reasoning__": True, "content": …}` (raisonnement du modèle, Ollama seul).
**IMPÉRATIF : tout consommateur filtre par `isinstance(item, str)` avant de
concaténer.** Les douze sites d'appel le font ; le seul qui ne le faisait pas
(`_stream_résumé_sse`) sérialisait `__stats__` comme un token depuis toujours, ce
qui collait un « [object Object] » à la fin de chaque résumé. Une sentinelle de
plus ne doit pas pouvoir se retrouver dans du texte.

Côté WebSocket de chat, le raisonnement a son propre type — `{"type": "reasoning",
"content": …}`, même forme que `{"type": "token", …}`. Il **n'entre pas** dans
`accumulated`, donc pas dans `history`, donc pas dans le prompt du tour suivant :
c'est ce que `test_raisonnement_stream.py` vérifie explicitement.

**Bascule `raisonnement`** — `stream(..., raisonnement: bool = True)`, réglage de
session (`memory` → clé `raisonnement`, `PATCH /context/settings`, toggle dans le
panneau Compétences). Le défaut `True` est le comportement historique, donc les
onze autres appelants n'ont rien à passer. Les deux moteurs locaux **ne se
pilotent pas de la même façon**, et c'est mesuré, pas déduit :

| | désactiver | activer |
|---|---|---|
| **Ollama** | `think=False` — ignoré proprement par un modèle sans raisonnement | **ne rien passer.** `think=True` → **400** `"qwen2.5:7b" does not support thinking`, y compris sur le modèle par défaut de `config.yaml` |
| **FLM** (`/v1`) | `extra_body={"think": False}` | `extra_body={"think": True}` — toléré même par `lfm2:1.2b`, qui ne pense pas |

Deux pièges propres à FLM : **omettre le flag ne veut pas dire « défaut du
modèle » mais « garder la valeur du dernier appel »** (état collant côté serveur,
mesuré) — donc toujours le passer, dans les deux sens ; et il passe par
`extra_body`, le SDK `openai` levant sur un paramètre inconnu. Les fournisseurs
cloud ne reçoivent rien : leur bascule n'a pas été mesurée.

**Le raisonnement de FLM remonte aussi**, depuis le 2026-08-24 et sous la même
sentinelle `__reasoning__` — donc le même `{"type": "reasoning"}` et le même bloc
repliable, sans une ligne de frontend en plus. Le champ s'appelle
**`reasoning_content`** (pas `reasoning`), il est atteignable en attribut bien que
non modélisé par le SDK (`getattr`, jamais un accès direct), et **le premier chunk
le porte VIDE** : tester la vérité, pas la présence. Mesuré : premier contenu à
91,8 s avant, premier affichage à 5,3 s après — le silence était plus long ici
que sur Ollama, sur le chemin NPU censé être le rapide.

**Réservé à `flm`.** `deepseek` publie aussi un `reasoning_content` et le remonter
serait probablement juste, mais ça n'a pas été mesuré — le vérifier veut dire
appeler une API payante. Lever la garde tiendra en retirant le test sur
`provider` ; rien d'autre à changer.

**Ce que deux mesures trop étroites ont coûté**, et c'est l'enseignement à garder :
sondé sur `qwen3.5:4b` seul, FLM « ne séparait pas le raisonnement du contenu ».
Vrai de ce modèle, faux de `qwen3:4b`. **Un modèle sondé ne dit rien de la
famille** — même piège que le §0 de `docs/remplacement-vectoriel.md` a été écrit
pour éviter, rejoué sur les modèles au lieu des wheels.

- SSE : `StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)`
  où `SSE_HEADERS` vient de `core.runtime` (`Cache-Control: no-cache`,
  `X-Accel-Buffering: no` — indispensable derrière nginx).
- WebSocket : le middleware HTTP ne s'applique pas. **IMPÉRATIF : appeler
  `await ws_require_token(websocket)` AVANT `accept()`** et `return` si False
  (`core/auth.py`). Le token arrive en query param `?token=` parce que les
  navigateurs n'autorisent pas d'en-tête sur `new WebSocket()`.

