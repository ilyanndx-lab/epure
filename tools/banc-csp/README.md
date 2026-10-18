# Banc CSP (Edge headless + CDP)

Mesure manuelle de la CSP telle que le navigateur l'applique réellement :
violations relevées dans la page et rapports envoyés à `/csp/report`.
**Pas câblé en CI** : il faut un Edge, un backend lancé et une interface
construite.

## Prérequis

- Microsoft Edge, testé en **155.0.4283.62** (chemin par défaut
  `C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`, sinon
  `$EDGE`) ;
- Node ≥ 22 (`WebSocket` et `fetch` globaux), testé en 24.16 ;
- Git Bash (`sh`, `mktemp`, `cygpath`) ;
- le backend sur :8000 servant le build (`.\tools\dev-epure.ps1`), ou Vite
  sur :5173 pour le mode dev.

## Commande (Windows, depuis la racine du dépôt)

```powershell
bash tools/banc-csp/banc.sh                              # sonde_connect, build sur 127.0.0.1 puis localhost
bash tools/banc-csp/banc.sh csp http://127.0.0.1:8000/   # sonde_csp, build
bash tools/banc-csp/banc.sh csp http://localhost:5173/   # sonde_csp, dev
```

Chaque page est ouverte dans un Edge à profil temporaire neuf, donc avec un
localStorage vide : l'appairage par `/pair` est réel. Seuls les processus Edge
de ce profil sont arrêtés à la fin.

## Ce que mesure chaque sonde

- **`sonde_connect.mjs`** : charge l'interface, attend 9 s, puis relève
  les violations `connect-src` de deux manières indépendantes : l'événement
  `securitypolicyviolation`, écouté avant tout script, et les POST vers
  `/csp/report` vus au réseau. Elle affiche aussi les origines appelées.
  Verdict `AUCUNE` ou `VIOLATIONS`.
- **`sonde_csp.mjs`** : affiche les en-têtes CSP de la page, puis provoque
  deux violations volontaires : une image distante (`img-src`, bloquant) et
  un `fetch` vers 203.0.113.7 (`connect-src`, Report-Only). Elle relève pour
  chaque rapport envoyé l'URL, le `Content-Type`, le corps et, si CDP le
  voit, le statut. CDP ne montre jamais la réponse d'un rapport
  cross-origin (dev). En même origine, il peut aussi la manquer : relevé le
  2026-10-18, deux rapports sans statut côté CDP, mais 204 au journal
  d'accès. **La référence du statut, c'est le journal d'accès du backend**
  (`POST /csp/report … 204`).

## Résultats de référence (2026-10-18, Edge 155)

| PR | Sonde | Page | Résultat |
|---|---|---|---|
| #72 | `csp` | dev (Vite :5173 → :8000) | rapports `img-src` enforce + report, `connect-src` report, `application/csp-report`, **204** ×3 (journal d'accès) |
| #72 | `csp` | build (`127.0.0.1:8000`) | mêmes rapports, même origine, **204** (Network ; rejoué le 2026-10-18 : 204 au journal d'accès) |
| #73 | `connect` | build `127.0.0.1:8000` | **0 violation `connect-src`** (avant #73 : 14, toutes vers `localhost:8000`) |
| #73 | `connect` | build `localhost:8000` | **0 violation `connect-src`** |

Depuis #73, une seule image bloquée produit un seul rapport `img-src`
(`enforce`) au lieu de deux.
