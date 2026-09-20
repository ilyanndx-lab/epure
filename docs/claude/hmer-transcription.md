# Épure — Transcription manuscrite (HMER), détail

> Extrait de `CLAUDE.md` (§3.8), déplacé ici le 2026-09-20 pour alléger
> le noyau chargé à chaque session. Contenu inchangé, seul l'emplacement a
> changé — voir `CLAUDE.md` pour le renvoi et la règle résumée.

---

### 3.8 Transcription manuscrite — la seule pile lourde du dépôt, et elle ne sort pas

`core/hmer.py`, phase 2 du module `encre` (`docs/module-encre.md`). Rend une page
de tracés en bitmap, la donne à **`pix2text-mfr`** (TrOCR ré-entraîné sur
formules, ONNX, CPU), rend du LaTeX. Déclenché par un BOUTON, une page à la fois
(`POST /encre/pages/{id}/transcrire`), jamais en tâche de fond.

**Ce que la transcription EST : un index, pas une sortie.** ExpRate mesuré en
phase 0 sur 50 expressions manuscrites réelles : **24,0 %**. C'est assez pour
retrouver une page au milieu des fiches, et très loin de ce qu'il faudrait pour
la relire. D'où : texte affiché brut, en lecture seule, sans rendu mathématique,
et aucune post-correction par LLM (`docs/module-encre.md` en fait une phase
séparée — un LLM transforme volontiers une expression juste en expression
plausible et fausse).

**IMPÉRATIF — `core/hmer.py` n'importe QUE la bibliothèque standard au niveau
module.** `core/runtime.py` fait `from core.hmer import HmerEngine` au niveau
module ; `optimum`/`transformers`/`Pillow` sont importés **dans les méthodes**.
Un import en tête de fichier ne coûterait pas « quelques secondes » : il ferait
échouer à la COLLECTE tout test qui importe `main`, puisque le job rapide de la
CI n'installe aucune de ces dépendances — l'incident `readability-lxml` rejoué
(§8). Mesuré : 16,7 s d'import à chaud, 54,2 s à froid, parce
qu'`optimum.onnxruntime` fait un `import torch` de niveau module.

**IMPÉRATIF — `optimum-onnx` et `transformers` sont dans `HORS_PAQUET_PIP`, et
c'est ce qui autorise leur existence.** `torch` part avec eux (≈765 Mo sur ce
poste, plusieurs Go de plus sur Linux où il déclare `nvidia-*` et `triton`), et
`tokenizers`/`regex` aussi — les deux `.pyd` non signés dont le blocage par Smart
App Control est mesuré dans ce dépôt. Aucun module livré n'importe `core/hmer.py`
(`encre` n'est ni dans `MODULES_COEUR` ni dans `modules-catalogue/`), donc aucun
destinataire n'en a besoin. **L'invariant a changé de mécanisme le 2026-09-07** :
`test_dependances_declarees.py` interdisait ces paquets de DÉCLARATION, il exige
maintenant leur ABSENCE DU LIVRABLE (`HmerHorsPaquetTest`). C'est plus faible et
il faut le savoir : retirer ces deux entrées de `HORS_PAQUET_PIP` livrerait des
gigaoctets et deux binaires non signés, sans qu'aucun autre garde-fou proteste.

**`torch` est requis, mesuré, pas supposé.** La question valait d'être posée —
l'inférence est en ONNX Runtime, on n'appelle jamais torch. Vérification dans un
venv propre : `pip uninstall torch` puis `from optimum.onnxruntime import
ORTModelForVision2Seq` → `optimum/onnxruntime/modeling_seq2seq.py:23, import
torch, ModuleNotFoundError`. Il n'existe pas de version sans torch de cette pile ;
il existe le choix de réécrire le décodage seq2seq en numpy pur, que
`docs/module-encre.md` §1 réserve au jour où ce module deviendrait livrable.

Cinq points qui se déduisent mal :

- **Les poids se chargent depuis un dossier LOCAL, jamais depuis le hub.**
  `_hf_offline_if_cached()` pose `HF_HUB_OFFLINE=1` dès que le cache Whisper
  existe (§3.2), c'est-à-dire sur ce poste : un
  `from_pretrained("breezedeus/pix2text-mfr")` échouerait, en disant « absent du
  cache ». On télécharge par `urllib` + sha256 dans `resolve_hmer_dir()` sur une
  **révision épinglée**, puis on charge ce dossier — idiome de `core/voice.py`.
  L'épinglage n'est pas de la prudence rituelle : la baseline de la phase 0 a été
  mesurée sur ces poids-là.
- **Le canevas est dimensionné sur le bounding box des POINTS**, jamais sur la
  taille logique de la page côté frontend, puis recadré au contenu (+10 % de
  marge). Ce recadrage est le geste qui a fait passer la phase 0 de **0 % à 24 %**
  d'ExpRate — une encre occupant 2 % du canevas devient illisible une fois écrasée
  en 384×384. Il est presque neutre sur une page rendue ici (le canevas est déjà
  serré) ; il porte tout le gain le jour où l'entrée vient d'une photo. C'est
  pour ça que `test_hmer.py` l'éprouve sur une image fabriquée à marges blanches
  et non de bout en bout, où il ne prouverait rien.
- **`.convert("RGB")` à l'entrée du modèle, et pas plus tôt.** Le rendu est en
  niveaux de gris (`L`) — un canal, et le seuil du recadrage raisonne en
  luminance — mais `DeiTImageProcessor` lève `Unsupported number of image
  dimensions: 2` sur une image à un canal. Trouvé par un essai de bout en bout et
  par rien d'autre : aucun test de rendu ne pouvait le voir.
- **La source RAG est `encre:<id>`, pas un chemin de fichier fabriqué**
  (`RAGEngine.index_page_encre`, méthode dédiée — `index_file` lit le disque et
  relève un `mtime`, ce qu'une page n'a pas). Conséquence à ne pas oublier :
  `GET /rag/files/ouvrir` passait le contrôle d'appartenance au corpus puis
  levait sur `FileResponse`, donc 500. Refus explicite désormais
  (`core.rag.est_source_virtuelle`).
- **`set_transcription` ne touche pas `date_modification`.** Transcrire ne
  MODIFIE pas la page : faire avancer cette date remonterait la page en tête de
  liste sans qu'un trait ait bougé. La transcription porte sa propre date.

