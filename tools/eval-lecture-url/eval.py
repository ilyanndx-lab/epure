"""Mini-évaluation du budget de lecture d'URL (#75), en deux étages.

Pourquoi deux étages : la question « la réponse est-elle dans ce que le modèle
reçoit ? » ne demande aucun LLM, et c'est elle qui sépare les budgets. Le
premier essai, tout-LLM avec raisonnement, prenait ~4 min par question sur
batterie et a été tué faute de mémoire au bout d'une question sur trente.
L'étage 2 ne fait donc tourner le modèle que là où l'étage 1 montre un écart.

  étage 1 (sans LLM) : pour chaque question × budget, extraction par le code
      réel (`core.webcontent.lire_urls` : passages, embedding local partagé,
      sélection au budget) sur la page figée, puis présence de la réponse
      attendue dans l'extrait (correspondance normalisée).
  étage 2 (LLM) : contexte système réel du chat
      (`modules.chat.router._construire_ctx_lecture`), envoyé à LM Studio,
      raisonnement coupé, sur les seules questions retenues par l'étage 1.

Isolation : les dossiers de données de l'instance pointent vers un dossier
temporaire (aucune écriture dans les données réelles) ; seul le modèle
d'embedding réel est lu. Les pages viennent des fixtures, jamais du réseau.

    python tools/eval-lecture-url/eval.py etage1
    python tools/eval-lecture-url/eval.py etage2 --modele <clé LM Studio> [--decharger]
"""
import argparse
import ctypes
import gzip
import json
import os
import pathlib
import re
import socket
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
from unittest import mock

ICI = pathlib.Path(__file__).resolve().parent
BACKEND = ICI.parents[1] / "backend"
RESULTATS = ICI / "resultats"
LMSTUDIO = os.environ.get("LMSTUDIO_HOST", "http://127.0.0.1:1234").rstrip("/")

#: Sous ce seuil de mémoire libre, l'étage 2 s'arrête proprement (résultats
#: déjà écrits, modèle déchargé si demandé) au lieu de se faire tuer.
SEUIL_MEMOIRE_GO = 3.0
#: Raisonnement coupé : la réponse seule tient largement dans ce plafond.
MAX_TOKENS_SORTIE = 1024


def _isoler():
    """Dossiers de données vers un temporaire, AVANT tout import de `core`."""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="epure-eval-lecture-"))
    for var in ("EPURE_DATA_DIR", "EPURE_HISTORY_DIR", "EPURE_FICHES_DIR", "EPURE_VECTOR_DIR",
                "EPURE_WEB_DIR", "EPURE_ENCRE_DIR", "EPURE_ENCRE_DATASET_DIR"):
        (tmp / var.lower()).mkdir()
        os.environ[var] = str(tmp / var.lower())
    os.environ["EPURE_EMBEDDING_AUTOINSTALL"] = "0"
    sys.path.insert(0, str(BACKEND))
    os.chdir(BACKEND)


def normaliser(texte: str) -> str:
    """Casse, accents, apostrophes, espaces — et les espaces autour de la
    ponctuation, que l'extraction de texte insère (« Mitsui , Mitsubishi »,
    « 1/ T »)."""
    texte = unicodedata.normalize("NFKD", texte.replace("’", "'"))
    texte = "".join(c for c in texte if not unicodedata.combining(c)).casefold()
    texte = re.sub(r"\s+", " ", texte)
    return re.sub(r"\s*([,;:()/'])\s*", r"\1", texte).strip()


def memoire_libre_go() -> float:
    class Etat(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    etat = Etat()
    etat.dwLength = ctypes.sizeof(Etat)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(etat))
    return etat.ullAvailPhys / 1024 ** 3


def sur_secteur():
    """True / False, ou None si inconnu (pas de batterie, autre OS)."""
    class Etat(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte),
                    ("BatteryLifePercent", ctypes.c_byte), ("SystemStatusFlag", ctypes.c_byte),
                    ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]
    etat = Etat()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(etat)):
        return None
    return {0: False, 1: True}.get(etat.ACLineStatus)


def lire(webcontent, q, budget):
    """Extraction par le code de #75, page servie depuis la fixture."""
    html = gzip.decompress((ICI / "pages" / f"{q['page']}.html.gz").read_bytes()).decode("utf-8")

    class Reponse:
        status = 200
        headers = {"Content-Type": "text/html; charset=utf-8"}

        def geturl(self):
            return q["url"]

        def read(self, n=-1):
            corps = html.encode("utf-8")
            return corps if n is None or n < 0 else corps[:n]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    etapes = []
    dns_public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 443))]
    with mock.patch.object(webcontent, "_ouvrir", lambda req, timeout: Reponse()), \
         mock.patch.object(webcontent.socket, "getaddrinfo", lambda *a, **k: dns_public), \
         mock.patch.object(webcontent, "budget_lecture", lambda _contexte: budget):
        lectures = webcontent.lire_urls([q["url"]], q["question"], on_etape=etapes.append)
    etape = next(e for e in etapes if e["etape"] == "lecture_url")
    return lectures, etape, [e for e in etapes if e["etape"] == "reclassement_indisponible"]


def etage1(budgets):
    from core import webcontent
    questions = json.loads((ICI / "questions.json").read_text(encoding="utf-8"))
    lignes = []
    for q in questions:
        for budget in budgets:
            lectures, etape, indispo = lire(webcontent, q, budget)
            extrait = lectures[0].resultat.extrait if lectures[0].resultat else ""
            presents = {f: normaliser(f) in normaliser(extrait) for f in q["fragments"]}
            lignes.append({
                "page": q["page"], "budget": budget, "present": all(presents.values()),
                "fragments": presents, "caracteres": len(extrait),
                "passages": etape.get("passages_retenus"), "selection": etape.get("selection"),
                "embedding_indisponible": bool(indispo),
            })
            print(f"{q['page']:24s} {budget:6d} présent={lignes[-1]['present']!s:5s} "
                  f"car={len(extrait):6d} passages={etape.get('passages_retenus')} "
                  f"sélection={etape.get('selection')}", flush=True)
    RESULTATS.mkdir(exist_ok=True)
    (RESULTATS / "etage1.json").write_text(
        json.dumps({"budgets": budgets, "lignes": lignes}, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8", newline="\n")
    return lignes


def questions_etage2(lignes, petit, grand):
    """Questions où la présence diffère entre le petit et le grand budget, ou
    absente au petit."""
    par = {(l["page"], l["budget"]): l["present"] for l in lignes}
    pages = dict.fromkeys(l["page"] for l in lignes)
    return [p for p in pages if not par[(p, petit)] or par[(p, petit)] != par[(p, grand)]]


def _modeles_lmstudio():
    with urllib.request.urlopen(f"{LMSTUDIO}/api/v1/models", timeout=5) as r:
        return json.loads(r.read().decode("utf-8")).get("models") or []


def contexte_charge(modele):
    for m in _modeles_lmstudio():
        if m.get("key") == modele:
            for inst in m.get("loaded_instances") or []:
                return (inst.get("config") or {}).get("context_length")
    return None


def decharger(modele):
    """Décharge toutes les instances de `modele` (l'API exige `instance_id`)."""
    for m in _modeles_lmstudio():
        if m.get("key") != modele:
            continue
        for inst in m.get("loaded_instances") or []:
            req = urllib.request.Request(
                f"{LMSTUDIO}/api/v1/models/unload", method="POST",
                data=json.dumps({"instance_id": inst["id"]}).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=60).read()
            print(f"déchargé : {inst['id']}", flush=True)


class MemoireBasse(Exception):
    pass


def interroger(modele, systeme, utilisateur):
    """Flux LM Studio, raisonnement coupé. `reasoning_effort: "none"` est le
    seul réglage mesuré efficace sur ce Qwen3.5 (2026-10-19) : 0 token de
    raisonnement, contre 400/400 pour `chat_template_kwargs.enable_thinking:
    false` et `reasoning: "off"`, ignorés en silence."""
    corps = json.dumps({
        "model": modele, "stream": True, "max_tokens": MAX_TOKENS_SORTIE,
        "reasoning_effort": "none", "stream_options": {"include_usage": True},
        "messages": [{"role": "system", "content": systeme}, {"role": "user", "content": utilisateur}],
    }).encode("utf-8")
    req = urllib.request.Request(f"{LMSTUDIO}/v1/chat/completions", data=corps,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    premier, reponse, usage, fin, n = None, "", {}, None, 0
    with urllib.request.urlopen(req, timeout=900) as r:
        for ligne in r:
            n += 1
            if n % 50 == 0 and memoire_libre_go() < SEUIL_MEMOIRE_GO:
                raise MemoireBasse()
            ligne = ligne.decode("utf-8").strip()
            if not ligne.startswith("data:") or ligne == "data: [DONE]":
                continue
            d = json.loads(ligne[5:])
            usage = d.get("usage") or usage
            for choix in d.get("choices") or []:
                delta = choix.get("delta") or {}
                if (delta.get("content") or delta.get("reasoning_content")) and premier is None:
                    premier = time.time() - t0
                reponse += delta.get("content") or ""
                fin = choix.get("finish_reason") or fin
    details = usage.get("completion_tokens_details") or {}
    resultat = {"reponse": reponse, "t_premier_token_s": round(premier, 2) if premier is not None else None,
                "t_total_s": round(time.time() - t0, 2), "tokens_prompt": usage.get("prompt_tokens"),
                "tokens_sortie": usage.get("completion_tokens"),
                "tokens_raisonnement": details.get("reasoning_tokens"), "fin": fin}
    # Deux défauts vécus le 2026-10-19, à dire plutôt qu'à compter comme un
    # essai : un flux coupé sans `finish_reason` (modèle déchargé en cours de
    # route), et un raisonnement que `reasoning_effort` n'a pas coupé (ignoré
    # sur l'appareil LM Link distant, mais pas en local).
    if fin is None:
        resultat["anomalie"] = "flux interrompu (aucun finish_reason)"
    elif details.get("reasoning_tokens"):
        resultat["anomalie"] = "raisonnement non coupé"
    return resultat


def etage2(modele, budgets, decharger_a_la_fin):
    from core import webcontent
    import modules.chat.router as routeur

    etage1_json = json.loads((RESULTATS / "etage1.json").read_text(encoding="utf-8"))
    pages = questions_etage2(etage1_json["lignes"], budgets[0], budgets[-1])
    questions = [q for q in json.loads((ICI / "questions.json").read_text(encoding="utf-8"))
                 if q["page"] in pages]
    sortie = RESULTATS / "resultats.json"
    doc = {"meta": {"modele": modele, "contexte_charge": contexte_charge(modele),
                    "raisonnement": "coupé (reasoning_effort: none)", "budgets": budgets,
                    "max_tokens_sortie": MAX_TOKENS_SORTIE, "sur_secteur": sur_secteur(),
                    "debut": time.strftime("%Y-%m-%d %H:%M"), "questions": pages, "arret": None},
           "essais": []}
    print(f"étage 2 : {len(questions)} question(s) × {budgets}, contexte chargé "
          f"{doc['meta']['contexte_charge']}, secteur={doc['meta']['sur_secteur']}", flush=True)

    def ecrire():
        sortie.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")

    ecrire()
    try:
        for q in questions:
            for budget in budgets:
                libre = memoire_libre_go()
                if libre < SEUIL_MEMOIRE_GO:
                    raise MemoireBasse()
                lectures, etape, _ = lire(webcontent, q, budget)
                systeme = routeur._construire_ctx_lecture(lectures)
                essai = {"page": q["page"], "budget": budget, "question": q["question"],
                         "attendu": q["attendu"], "caracteres": etape.get("caracteres"),
                         "memoire_libre_go": round(libre, 1)}
                essai.update(interroger(modele, systeme, f"{q['question']} {q['url']}"))
                doc["essais"].append(essai)
                ecrire()
                print(f"{q['page']:24s} {budget:6d} ttft={essai['t_premier_token_s']}s "
                      f"prompt={essai['tokens_prompt']} sortie={essai['tokens_sortie']} "
                      f"fin={essai['fin']} {essai.get('anomalie', '')}", flush=True)
    except MemoireBasse:
        doc["meta"]["arret"] = f"mémoire libre sous {SEUIL_MEMOIRE_GO} Go"
        ecrire()
        print(f"ARRÊT : mémoire libre sous {SEUIL_MEMOIRE_GO} Go — résultats partiels écrits", flush=True)
    except (urllib.error.URLError, OSError) as exc:
        doc["meta"]["arret"] = f"LM Studio : {exc}"
        ecrire()
        print(f"ARRÊT : LM Studio a répondu {exc} — résultats partiels écrits", flush=True)
    finally:
        if decharger_a_la_fin:
            decharger(modele)
    return doc


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("etage", choices=["etage1", "etage2"])
    parser.add_argument("--budgets", type=int, nargs="+")
    parser.add_argument("--modele", help="clé LM Studio du modèle chargé (étage 2)")
    parser.add_argument("--decharger", action="store_true",
                        help="décharger le modèle de LM Studio à la fin ou à l'arrêt (étage 2)")
    args = parser.parse_args()
    _isoler()
    if args.etage == "etage1":
        etage1(args.budgets or [4000, 8000, 16000])
    else:
        if not args.modele:
            parser.error("--modele est requis pour l'étage 2")
        etage2(args.modele, args.budgets or [4000, 16000], args.decharger)
