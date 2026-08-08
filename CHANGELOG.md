# Changelog

## 2026-08-07 — CI : découverte automatique des tests, build frontend bloquant

Le job `backend` lançait les tests **un par un, nommément**. Quatre fichiers sur
six ne tournaient donc jamais, sans que rien ne le signale : il suffisait
d'oublier une ligne. Il tourne maintenant en
`python -m unittest discover -s . -p 'test_*.py' -v` — un nouveau
`backend/test_*.py` est pris en compte sans toucher au workflow.

`test_modules_mount.py` est renommé `integration_modules_mount.py` : il charge
`core.runtime` (torch, chromadb, sentence-transformers) et n'a rien à faire dans
le job léger. Le préfixe suffit à l'exclure du motif de découverte — pas de
`skipUnless` ni de variable d'environnement à se rappeler. Il reste lancé par le
job `integration` (manuel).

Le job `frontend` fait désormais `npm run build` de façon **bloquante** :
`tsc --noEmit` ne couvre pas les erreurs de bundling, et le TSX généré par
l'Atelier atterrit dans `frontend/src/modules/generated/` (inclus dans
`tsconfig.app.json`). Un module généré qui ne bundle pas cassait l'image
frontend sans que la CI le voie.

### Dette assumée, non traitée par ce lot

- **`eslint` reste en `continue-on-error`** et **`"strict": true` reste absent
  de `tsconfig.app.json`**. Les deux sont de vraies dettes, mais les résorber
  implique de toucher ~94 erreurs (règles `react-hooks`) sur tout le codebase :
  c'est un chantier séparé, pas du durcissement. Tant que ce point n'est pas
  traité, un warning eslint ne fait pas échouer la CI — ne pas s'y fier.
- **`backend/test_module_isolation.py` n'est pas suivi par git**, donc la
  découverte automatique ne le voit pas en CI (elle ne voit que ce qui est
  commité). C'est le chantier d'isolation des modules générés, cf. CLAUDE.md §7.

## 2026-07-02 — Purge des données personnelles de l'historique git

L'intégralité de l'historique (toutes branches) a été réécrite avec
`git filter-repo --invert-paths` pour supprimer définitivement les données
personnelles de runtime, avant partage du dépôt :

- `backend/history/` (conversations sauvegardées)
- `backend/memory/` (profil élève, sessions, flashcards, quotas…)
- `backend/doc_uploads/` (documents uploadés)
- `backend/chroma_db/` (index vectoriel des fiches)
- `workspace/` (sorties du code-agent — oublié par la première passe,
  purgé par une seconde réécriture le même jour)

Ces chemins n'étaient plus suivis depuis `b8575c1` (ils sont recréés
automatiquement au démarrage — un clone frais fonctionne sans eux) ; cette
réécriture retire aussi les versions historiques. **Tous les hashes de commit
ont changé** : les clones antérieurs au 2026-07-02 doivent être re-clonés
(pas de pull — l'historique a divergé volontairement). Une sauvegarde miroir
complète de l'état antérieur est conservée localement, hors dépôt.
