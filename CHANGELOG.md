# Changelog

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
