# Fixtures de test

Réponses réseau RÉELLES enregistrées sur disque, pour des tests qui doivent
parser un format externe sans accès réseau (`_test_env` isole les données de
runtime, pas le réseau — c'est aux tests eux-mêmes de ne pas en dépendre).

Une fixture inventée à la main (des balises HTML avec des `href="x"`) ne
prouve rien : elle valide le parseur contre lui-même, jamais contre le format
réel. C'est exactement ce qui avait laissé passer le bug de
`backend/test_web_search.py` (href jamais capturé) — le fixture d'alors était
inventé et ne pouvait pas le détecter.

## Contenu

- `ddg_html_python.html` — réponse de `https://html.duckduckgo.com/html/?q=python+programming`,
  enregistrée le 2026-09-02. Utilisée par `test_web_search.py` pour le
  fallback HTML de la recherche `@web` (`core/websearch.py`).

## Régénérer une fixture

```powershell
curl.exe -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36" `
  "https://html.duckduckgo.com/html/?q=python+programming" `
  -o backend/fixtures/ddg_html_python.html
```

Si la structure DuckDuckGo change et que `test_web_search.py` commence à
échouer sur la détection « structure modifiée », c'est le signal pour
régénérer ce fichier — pas pour relâcher l'assertion.
