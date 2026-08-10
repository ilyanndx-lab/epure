# Note — une installation neuve ouvre sur Réglages, pas sur Chat

**Statut : ouverte, non traitée.** Constatée le 2026-08-10 en vérifiant l'étape A de
`docs/distribution-empaquetee.md`. Hors périmètre de ce chantier : le bug est
**préexistant et identique en développement**, il n'a rien à voir avec le service du
frontend par FastAPI. Consignée ici pour ne pas être re-découverte, et parce que
l'empaquetage la rend visible par quelqu'un d'autre qu'Ilyann.

---

## Symptôme

Sur un `localStorage` vide — donc à la première ouverture, chez n'importe qui — l'app
s'ouvre sur **Réglages** et non sur **Chat**. Reproduit dans Chrome avec un profil neuf,
sur un backend servant le frontend construit.

Ce n'est pas un écran cassé : la barre est complète, les modules sont là, tout fonctionne.
C'est le module *sélectionné* qui est le mauvais.

## Cause

`frontend/src/App.tsx` :

```ts
const [activeModule, setActiveModule] = usePersistentState<string>('epure.activeModule', 'chat')   // l. 24
…
const ordre = orderedModules(modules, config.modules_activés)                                       // l. 143
const visibleIds = new Set<string>(['settings', 'workshop', ...ordre.map(m => m.id)])               // l. 144

useEffect(() => {
  if (!visibleIds.has(activeModule)) {
    const first = ordre.map(m => m.id).find(id => visibleIds.has(id))
    setActiveModule(first ?? 'settings')                                                            // l. 150
  }
}, [activeModule, config.modules_activés, modules])
```

Au **premier rendu**, `GET /modules` n'a pas encore répondu : `modules` est vide, donc
`ordre` est vide et `visibleIds` ne contient que `settings` et `workshop`. `activeModule`
vaut `chat`, qui n'y est pas → l'effet bascule sur `settings`.

Ensuite les modules arrivent, et `activeModule` vaut désormais `settings` — qui *est*
visible. La condition de garde est satisfaite, l'effet ne fait plus rien, et **personne ne
ramène l'utilisateur sur Chat**. Le choix par défaut a été consommé par une course.

Le garde-fou fait donc exactement ce qu'on lui demande (« si le module courant devient
inaccessible, bascule vers le premier visible ») ; ce qu'il ne distingue pas, c'est
« inaccessible » de « pas encore chargé ».

## Pourquoi ça compte plus qu'avant

C'est le **premier écran** que verra le destinataire d'un paquet. Réglages est l'écran le
moins accueillant de l'application, et celui dont il n'a en principe rien à changer.

C'est aussi le frère du bug corrigé dans `b1d6eb2`, où la barre elle-même ne montrait que
Réglages sur une installation neuve, parce que le frontend lisait `modules_activés: []`
comme « aucun module » là où le backend y lit « tous les modules installés ». La barre est
réparée ; la sélection ne l'est pas.

## Pistes, non tranchées

1. **Ne rien décider avant d'avoir les modules.** Sauter l'effet tant que `modules` est
   vide *et* qu'aucune requête n'a abouti. Demande de distinguer « pas encore chargé » de
   « chargé et vide » — soit un état de chargement explicite, que le composant n'a pas
   aujourd'hui.
2. **Ne pas persister une bascule automatique.** `usePersistentState` écrit dans
   localStorage ; si la bascule de secours n'était pas persistée, le défaut `chat`
   survivrait au rechargement suivant. Corrige le symptôme durable, pas la première
   ouverture.
3. **Faire de la bascule un repli d'affichage et non un changement d'état** : rendre
   Réglages quand `activeModule` n'est pas disponible, sans réécrire `activeModule`.
   Probablement le plus juste — l'état « ce que l'utilisateur a choisi » et l'état « ce
   qu'on peut afficher maintenant » sont deux choses différentes, et le bug vient de les
   avoir confondues.

Aucune n'est vérifiée. À trancher quand ce sera le sujet, avec un test qui reproduit la
course (modules qui arrivent après le premier rendu) — sans ce test, les trois pistes se
valident par un « ça marche chez moi » qui dépend de la latence du backend.
