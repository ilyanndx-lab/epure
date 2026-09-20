# Épure — L'Atelier, détail (moteurs, interrupteurs de paquet)

> Extrait de `CLAUDE.md` (§5), déplacé ici le 2026-09-20 pour alléger le
> noyau. Contenu inchangé, seul l'emplacement a changé — voir `CLAUDE.md`
> pour le renvoi et la règle résumée.

---

Trois moteurs de génération, diagnostiqués dans Réglages › Atelier :

| Moteur | Exigence |
|---|---|
| `ollama` | toujours disponible, modèle actif de l'instance |
| `claude_sub` | CLI `claude` + `claude setup-token`. **Ne pas définir `ANTHROPIC_API_KEY`** — elle primerait sur l'abonnement. |
| `claude_gateway` | CLI `claude` + passerelle Anthropic-compatible locale (LiteLLM exposant `/v1/messages`). `ANTHROPIC_BASE_URL` pointé dessus. |

Un moteur `aider` existe également (mode architect, conversation Plan/Construire).

**L'Atelier est désactivable, pour le paquet distribué** (`docs/distribution-empaquetee.md`
étape B) — **désactivable, pas supprimable**. Deux interrupteurs, à poser ensemble
(`tools/faire_paquet.py` le fait) mais indépendants :

| Interrupteur | Effet |
|---|---|
| `EPURE_ATELIER=0` | 404 sur `/workshop*`, `/settings/test/*`, `/settings/gateway/*`, et fermeture de `/ws/workshop` avant `accept()`. Le 404 est posé **avant** le contrôle de token : un 401 révélerait que la route existe. |
| `VITE_ATELIER=0` | l'Atelier sort du **bundle**, pas seulement de l'écran. |

**IMPÉRATIF — ne pas supprimer `core/module_workshop.py` ni `core/module_validate.py`
d'un paquet.** `core/catalogue.py` importe sept symboles du premier, qui importe le second
au niveau module : les retirer casse `POST /settings/catalogue/{id}/install` et
`DELETE /settings/modules/{id}`, c'est-à-dire l'écran Réglages du destinataire, pas
l'Atelier.

Côté frontend, `src/atelier.ts` doit rester une **comparaison directe**
(`import.meta.env.VITE_ATELIER !== '0'`). Un `?.trim()` la rend non pliable par rolldown,
la branche morte reste atteignable, et un `Workshop-*.js` de 26,1 ko **contenant le code de
l'Atelier** part quand même dans le paquet — orphelin, mais sur le disque et lisible.
Verrouillé par `test_paquet.py`.
