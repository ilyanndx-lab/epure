# Limite du démontage de routes — fastapi ≥ 0.137

**État : bug ouvert, non corrigé.** Ce document n'annonce pas une solution. Il
consigne ce qui a été mesuré, pour que la prochaine personne à vouloir monter
fastapi — moi compris, dans six mois — ne recommence pas l'enquête à zéro et ne
cherche pas la cause au mauvais endroit.

**Antériorité, à lire d'abord : ce bug est plus vieux que le catalogue.** Il ne
vient ni de `core/catalogue.py`, ni de l'UI des réglages, ni des endpoints
d'installation. Le catalogue n'a fait que le *révéler*, en ajoutant les deux
premiers tests qui suppriment un module et vérifient que son API se taise.
`_drop_module_routes` vivait déjà dans `core/module_workshop.py`, appelé par
`_remount`, et était déjà cassé pour toute version de fastapi ≥ 0.137.

**Résumé en trois lignes.** `_drop_module_routes` retire les routes d'un module
en filtrant `app.router.routes`. À partir de fastapi 0.137.0, `include_router`
n'y met plus les routes du module mais **une seule entrée `_IncludedRouter`**
qui les masque. Le filtre ne trouve donc rien à retirer, et la route d'un module
supprimé continue de répondre **200**.

---

## 1. Ce que fait le dépôt, et pourquoi ça tient à un fil

`core/module_workshop.py:_drop_module_routes` :

```python
modname = f"modules.{module_id}.router"
app.router.routes[:] = [
    r for r in app.router.routes
    if getattr(getattr(r, "endpoint", None), "__module__", None) != modname
]
```

Deux hypothèses implicites, toutes deux sur des internes :

1. les routes d'un router inclus sont **à plat** dans `app.router.routes` ;
2. chaque élément de cette liste porte un `endpoint` dont le `__module__`
   identifie le module propriétaire.

Aucune n'est garantie par un contrat public. Vérifié sur fastapi 0.141.1 : il
n'existe **aucune API publique de démontage** — ni `remove_route`, ni `pop`, ni
`exclude`, ni `unmount`, sur `APIRouter` (fastapi), `Router` (starlette) ou
`Starlette`. Le filtrage manuel n'est pas un raccourci qu'on aurait pris par
paresse : c'est le seul moyen existant. D'où les deux tests qui le surveillent.

## 2. Le symptôme mesuré

Sur une app nue (4 routes de doc) plus un module inclus, avec le
`_drop_module_routes` du dépôt appelé verbatim :

```
fastapi 0.141.1 / starlette 1.6.0
routes: 5 avant include_router -> 6 apres      # sur une app avec /health
_drop_module_routes : routes 5 -> 5            # app nue : rien n'est retiré
GET /hello/ping apres demontage : 200
```

**`5 → 5`** : la liste ne bouge pas d'un élément. Et la route répond encore.

Sur la vraie app (`main.app`, arbre de modules temporaire de `_test_env`) :

```
FAIL: test_apres_reinstallation_c_est_la_nouvelle_version_qui_sert
FAIL: test_hello_ne_repond_plus_apres_suppression
AssertionError: 200 != 404
Ran 207 tests in 17.932s
FAILED (failures=2, skipped=2)
```

Exactement deux échecs sur 207, et ce sont les deux tests de route fantôme.
Rien d'autre ne casse en 0.141 : ni le montage, ni l'Atelier, ni les chemins.
C'est utile à savoir — la tentation, devant un bump rouge, est de croire que
tout est à reprendre.

**Ce n'est pas une hypothèse de laboratoire : la CI était rouge.** Le run
`31277621748` (commit `8fdc31c`, branche `catalogue/v1`) montre les deux mêmes
échecs, avec `fastapi-0.141.1` / `starlette-1.6.0` résolus par le
`pip install fastapi …` non épinglé. Le poste de dev, en 0.136.3, passait. Le
symptôme n'était donc pas « un test instable en CI » mais « la CI teste une
autre version que le poste de dev ».

**Corollaire sur un commit antérieur.** `8fdc31c` (« `_drop_module_routes`
mutait une liste qu'il remplaçait ») corrige un vrai défaut — réaffecter
`app.router.routes` au lieu de la muter en place laisse les détenteurs de
l'ancienne liste router vers les routes supprimées — mais ce n'était **pas** la
cause de l'échec en CI : le code actuel mute bien en place (`[:] =`) et échoue
quand même en ≥ 0.137. La CI est restée rouge après ce commit.

## 3. La borne exacte : 0.136.3 passe, 0.137.0 casse

Bissection, une version de fastapi par ligne, **avec starlette 1.6.0 dans tous
les cas** (c'est ce qui isole la cause) :

| fastapi | starlette | `_IncludedRouter` | `app.router.routes` | `GET /hello/ping` | verdict |
|---|---|---|---|---|---|
| 0.136.3 | 1.6.0 | absent | 5 → **4** | **404** | démonte |
| 0.137.0 | 1.6.0 | présent | 5 → 5 | 200 | fantôme |
| 0.137.2 | 1.6.0 | présent | 5 → 5 | 200 | fantôme |
| 0.138.0 | 1.6.0 | présent | 5 → 5 | 200 | fantôme |
| 0.138.2 | 1.6.0 | présent | 5 → 5 | 200 | fantôme |
| 0.139.0 | 1.6.0 | présent | 5 → 5 | 200 | fantôme |
| 0.139.2 | 1.6.0 | présent | 5 → 5 | 200 | fantôme |
| 0.140.0 | 1.6.0 | présent | 5 → 5 | 200 | fantôme |
| 0.140.13 | 1.6.0 | présent | 5 → 5 | 200 | fantôme |
| 0.141.1 | 1.6.0 | présent | 5 → 5 | 200 | fantôme |

**La bascule est en 0.137.0, pas en 0.141.** C'est important parce que la
première lecture de l'incident plaçait la limite « au-delà de 0.140 » : faux,
et de quatre mineures. Une plage validée trop large aurait laissé passer
0.137–0.140 en silence, c'est-à-dire précisément ce que le garde-fou est censé
empêcher.

**Et la limite est côté fastapi, pas côté starlette.** Deux mesures le
montrent :

- fastapi **0.136.3 + starlette 1.6.0** → `Ran 207 tests … OK (skipped=2)`.
  La suite complète passe avec la starlette la plus récente.
- fastapi **0.137.0 + starlette 1.6.0** → les deux échecs.

Le message d'échec historique (« internes Starlette changés ? ») visait donc le
mauvais paquet. `starlette` est bien épinglé dans `requirements.txt`, mais pour
la reproductibilité seule : fastapi le déclare `starlette>=0.46.0` **sans borne
haute**, donc sa version était subie et non choisie.

## 4. La structure constatée en 0.141 — inspection verbatim

Une app avec une route locale `/health` et un module inclus par
`app.include_router(router, prefix="")`. Sortie brute de l'inspection
d'attributs, sans retouche :

```
[4] fastapi.routing.APIRoute
     path='/health' endpoint='__main__'

[5] fastapi.routing._IncludedRouter
     path=None endpoint=None
     dir(non-dunder) = ['_build_effective_context', '_effective_candidates',
       '_effective_candidates_version', '_effective_low_priority_routes',
       '_effective_low_priority_routes_version', '_effective_routes_lock',
       '_handle_selected', '_match', 'effective_candidates',
       'effective_low_priority_routes', 'effective_route_contexts', 'handle',
       'include_context', 'matches', 'original_router', 'url_path_for']
     __dict__ keys   = ['_effective_candidates', '_effective_candidates_version',
       '_effective_low_priority_routes',
       '_effective_low_priority_routes_version', '_effective_routes_lock',
       'include_context', 'original_router']
```

Trois faits à retenir de cette sortie :

- l'entrée est un objet **`fastapi.routing._IncludedRouter`**, pas une route :
  `path` vaut `None` et **`endpoint` vaut `None`** ;
- les vraies routes ne sont plus là : elles vivent derrière
  **`original_router`** (l'objet `APIRouter` du module) ;
- le reste des attributs est un **cache** (`_effective_candidates`) gouverné par
  un **numéro de version** (`_effective_candidates_version`), avec son verrou.

Le filtre du dépôt teste `r.endpoint.__module__ != modname`. Sur cette entrée,
`endpoint` est `None`, donc `getattr(None, "__module__", None)` vaut `None`,
donc `None != "modules.hello.router"` est **vrai**, donc l'entrée est
**conservée**. Le filtre ne « échoue » pas : il fait exactement ce qu'on lui a
écrit, sur une liste qui ne contient plus ce qu'il cherche. D'où `5 → 5`.

## 5. Le mécanisme : `include_router` empile, un compteur invalide

`fastapi/routing.py`, fin de `include_router` (0.141.1) :

```python
self.routes.append(
    _IncludedRouter(original_router=router, include_context=include_context)
)
self._mark_routes_changed()
```

Le cache de correspondance, et le compteur dont il dépend :

```python
def effective_candidates(self) -> list["_EffectiveRouteContext | _IncludedRouter"]:
    routes_version = self.original_router._get_routes_version()
    if routes_version == self._effective_candidates_version:
        return self._effective_candidates
    with self._effective_routes_lock:
        ...
        self._effective_candidates_version = routes_version
        return effective_candidates
```

```python
def _mark_routes_changed(self) -> None:
    self._routes_version += 1

def _get_routes_version(self, seen: set[int] | None = None) -> int:
    ...
    version = self._routes_version
    for route in self.routes:
        if isinstance(route, _IncludedRouter):
            version += route.original_router._get_routes_version(seen)
    return version
```

**Le point qui décide de tout : la version est un compteur d'appels, pas une
empreinte du contenu.** Elle ne bouge que si quelqu'un appelle
`_mark_routes_changed()`. Muter la liste `routes` en place ne l'incrémente pas.
Mesuré, sur 0.141.1 :

```
app.router a _effective_candidates ?  False
version vue par le cache (original_router) = 1
apres vidage en place de original_router.routes : version = 1 (routes 1 -> 0)
```

On peut donc **vider entièrement** le router inclus : le cache garde sa version,
se croit à jour, et continue de servir les routes qu'il a mémorisées.

Second fait de cette sortie, qui explique la suite : **le router de tête n'a pas
de cache** (`app.router` n'a pas d'attribut `_effective_candidates`). Sa liste
`routes` est parcourue à chaque requête. Ce qu'on retire *au premier niveau*
prend effet immédiatement ; ce qu'on retire *en dessous* passe par le cache.

## 6. Les tentatives

Chacune mesurée sur 0.141.1, sur une app neuve, verdict = code HTTP de
`GET /hello/ping` après démontage (404 attendu).

### Tentative 1 — descendre dans `original_router.routes`

Filtrer récursivement : pour chaque `_IncludedRouter` rencontré, appliquer le
même filtre à `original_router.routes`.

```
routes 6 -> 6 | GET /hello/ping : 200 -> 200  => FANTOME (sert encore)
```

**Pourquoi ça échoue.** La route est bien retirée de la liste, mais rien
n'incrémente `_routes_version` : `effective_candidates()` retrouve la même
version, renvoie son cache, et la route continue d'être servie. Le démontage a
lieu dans la structure et n'a aucun effet sur le routage. C'est le pire des cas
de figure pour un débogage : la liste inspectée depuis un shell paraît correcte.

### Tentative 1 bis — la même, plus une invalidation sur `app.router`

Ajouter `app.router._mark_routes_changed()` après le filtrage.

```
routes 6 -> 6 | GET /hello/ping : 200 -> 200  => FANTOME (sert encore)
```

**Pourquoi ça échoue.** Le cache ne lit pas la version du router *parent* mais
celle du router *inclus* : `self.original_router._get_routes_version()`.
Incrémenter le compteur de `app.router` n'entre dans ce calcul que si l'app est
elle-même atteinte comme `original_router` d'un `_IncludedRouter` — ce qui n'est
pas le cas au premier niveau. On invalide un cache que personne ne consulte.

### Tentative 2 — retirer l'entrée `_IncludedRouter` elle-même

Décider qu'un `_IncludedRouter` appartient au module, puis le retirer de
`app.router.routes` (et garder le filtre à plat, pour rester compatible
≤ 0.136 où `include_router` aplatit encore).

```
routes 6 -> 5 | GET /hello/ping : 200 -> 404  => DEMONTE
```

Et sur la vraie app, les quatre tests concernés passent :

```
test_hello_ne_repond_plus_apres_suppression ... ok
test_apres_reinstallation_c_est_la_nouvelle_version_qui_sert ... ok
test_la_suppression_ne_laisse_rien_dans_sys_modules ... ok
test_install_monte_le_routeur_a_chaud ... ok
RESULTAT tentative 2 sur la vraie app : OK
```

**Celle-ci ne se solde donc pas par un échec — et c'est le résultat le plus
important de cette enquête.** Elle fonctionne parce qu'elle opère au premier
niveau, seul endroit sans cache (§5). Elle n'a pas été adoptée pour des raisons
qui sont des coûts, pas des blocages ; ils sont listés en §7, option B, avec les
topologies mesurées où elle laisse un fantôme.

### Variante — filtrage récursif *plus* invalidation au bon endroit

Pour mémoire, puisqu'elle marche aussi : la tentative 1 devient correcte si on
appelle `_mark_routes_changed()` sur **chaque router effectivement filtré**,
et non sur l'app.

```
routes 6 -> 6 | GET /hello/ping : 200 -> 404  => DEMONTE
RESULTAT variante [4] sur la vraie app : OK
entrees _IncludedRouter a original_router VIDE dans l'app : 4
```

Elle laisse en place des entrées `_IncludedRouter` vides — quatre après la
suite de tests. Inoffensives au routage, mais elles s'accumulent à chaque cycle
installation/désinstallation.

## 7. Les options restantes, avec leur coût

| Option | Ce que ça coûte |
|---|---|
| **A. Rester épinglé en 0.136.3** *(état actuel)* | fastapi gelé : plus de correctif de sécurité amont sans lever la garde. Coût nul aujourd'hui, croissant avec le temps. C'est un report de décision, pas une solution. |
| **B. Adopter la tentative 2** | Dépend du nom privé `fastapi.routing._IncludedRouter`, absent en 0.136.3 (vérifié) → code à double chemin, gardé par `hasattr`, qui doit rester correct sur **deux** dispositions internes privées à la fois. Plus les trous de la décision d'appartenance, mesurés ci-dessous. |
| **C. Adopter la variante récursive** | Dépend de **deux** membres privés (`_mark_routes_changed`, et `_IncludedRouter` pour la descente), tous absents en 0.136.3. Laisse des entrées orphelines (4 mesurées). Même double chemin que B. |
| **D. Ne plus démonter à chaud : redémarrer le backend après désinstallation** | Zéro dépendance aux internes — la seule option qui ne parie sur rien. Coût : les flux SSE/WS en cours sont coupés, l'UI doit gérer l'attente, et `epure_tray.py` doit piloter le redémarrage. Techniquement modeste, franc à relire. Dégrade l'UX d'une opération rare. |
| **E. Neutraliser le fantôme au lieu de le démonter** | Laisser la route en place et refuser à la requête ce qui n'est plus installé (garde en middleware ou dépendance, qui consulte l'état des modules). N'utilise que de l'API publique, donc **indépendant de la version** — c'est le seul candidat qui sort de la course aux internes. Coût : un contrôle par requête de module, un point de passage supplémentaire à ne pas oublier, et le schéma OpenAPI continue d'annoncer la route si on ne l'invalide pas. **Non mesuré** : aucune ligne n'en a été écrite, contrairement à A–C. |

### Les trous de la décision d'appartenance (options B et C)

Décider « ce `_IncludedRouter` est celui du module `X` » suppose de regarder ses
routes et de vérifier qu'elles viennent toutes de `modules.X.router`. Mesuré sur
0.141.1 :

| Topologie | Résultat |
|---|---|
| Le `router.py` du module inclut un sous-router défini dans un **autre fichier** du module | **FANTÔME sur toutes ses routes**, y compris celles écrites dans `router.py` — l'appartenance est refusée en bloc |
| Toutes les routes du module vivent dans un sous-fichier | **FANTÔME** |
| Deux modules montés, on en retire un | Correct : l'autre reste intact, pas de victime collatérale |
| Router de module **vide** (aucune route) | Entrée orpheline laissée (`routes 4 → 5 → 5`) |

Les deux premières lignes ne sont **pas atteignables aujourd'hui** : un module,
c'est trois fichiers (CLAUDE.md §3.3), et l'installation comme l'approbation ne
copient que `manifest.json` + `router.py` — un second fichier Python ne peut pas
arriver dans `backend/modules/<id>/`. Le risque n'est donc pas actuel ; il est
qu'on desserre ce contrat un jour (un module à plusieurs fichiers est une
demande naturelle) et que le démontage se remette à laisser des fantômes **en
silence**, sans qu'aucun test ne le dise. Si B ou C est retenu, ces topologies
doivent devenir des tests le même jour.

Autre fragilité commune à toutes les options sauf D et E : l'identification
repose sur `endpoint.__module__`. Un module qui enregistre une route dont la
fonction est définie ailleurs (un helper de `core`) sort du filtre. Ce défaut
est antérieur et indépendant de 0.137.

## 8. Ce qui tient la frontière aujourd'hui

Rien dans le code ne corrige le démontage. Ce qui empêche l'oubli :

- **`backend/test_versions_epinglees.py`** échoue si `fastapi.__version__` sort
  de `0.136.x`, avec le diagnostic et un renvoi vers ce fichier. Vérifié :
  vert en 0.136.1 et 0.136.3, rouge en 0.137.0 et en 0.141.1. Il vérifie aussi
  que `ci.yml` et `requirements.txt` épinglent les mêmes versions — c'est la
  version validée qui fait foi, pas la dernière publiée.
- **`backend/requirements.txt`** épingle `fastapi==0.136.3` et
  `starlette==1.2.0`.
- **`.github/workflows/ci.yml`** installe ces deux versions épinglées, au lieu
  de laisser pip résoudre la dernière publiée à chaque push.
- **Les deux tests de route fantôme** restent en place et passent sur la version
  épinglée. Ils continuent de vérifier le comportement voulu — ils ne sont pas
  la frontière de version, ils sont la définition de ce qui doit rester vrai.

## 9. Ce qui n'a pas été mesuré

À ne pas prêter à ce document :

- **L'option E n'a pas été implémentée**, pas même en brouillon. Son coût est
  estimé, pas constaté.
- **Rien en dessous de 0.136.1.** La borne basse de la plage validée n'est pas
  une version connue pour marcher, c'est une version non testée.
- **La raison amont du changement.** Le `CHANGELOG` de fastapi n'a pas été
  consulté : la structure a été lue dans le code installé. On ne sait donc pas
  si `_IncludedRouter` est un choix définitif ou une étape, ni si un moyen
  public de démontage est prévu. À vérifier avant d'écrire du code contre ces
  internes (options B et C).
- **Les mesures viennent d'un poste Windows en Python 3.14** ; la CI tourne en
  3.12. La reproduction en CI (run `31277621748`) donne les mêmes deux échecs,
  ce qui suffit à écarter un artefact de version de Python pour *ce* symptôme —
  pas pour le reste.
- **Le chemin de l'Atelier n'a pas été exercé en ≥ 0.137.** Il appelle le même
  `_remount`, et le test `test_apres_reinstallation_c_est_la_nouvelle_version_qui_sert`
  prouve déjà, côté catalogue, que réinstaller sert l'ancienne version. Donc
  approuver un module généré deux fois dans la même session sert très
  probablement l'ancien code — mais c'est une déduction, pas une mesure : aucun
  test n'approuve deux fois de suite.
