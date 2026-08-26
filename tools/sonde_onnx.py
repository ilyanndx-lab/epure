"""Sonde ARM64 : onnxruntime est-il executable sur cette machine ?

A jouer sur la machine cible AVANT tout chantier. Ne touche rien d'installe :
telecharge le modele dans le dossier temporaire, calcule un embedding, affiche
un verdict.

    python sonde_onnx.py

Ce qu'elle repond, dans l'ordre ou ca peut echouer :
  1. la wheel `onnxruntime` s'installe-t-elle (existe-t-il un win_arm64) ;
  2. ses binaires sont-ils signes SUR CETTE MACHINE ;
  3. l'import passe-t-il, ou Smart App Control le bloque-t-il comme sklearn ;
  4. onnxruntime sait-il EXECUTER le graphe (import reussi != inference reussie) ;
  5. le vecteur produit est-il celui qu'on attend (reference figee, poste x64).

Sans accent et sans filet Unicode dans les sorties : la console Windows en
francais est en cp1252, et un U+2500 y leve UnicodeEncodeError avant le premier
test.
"""
import hashlib
import os
import platform
import subprocess
import sys
import tempfile
import time
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
BASE = "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/" + REVISION
FICHIERS = {
    "model.onnx": ("onnx/model.onnx",
                   "6fd5d72fe4589f189f8ebc006442dbb529bb7ce38f8082112682524616046452"),
    "vocab.txt": ("vocab.txt",
                  "07eced375cec144d27c900241f3e339478dec958f92fddbc551f295c992038a3"),
}

PHRASE = "La transformee de Laplace converge pour Re(p) > sigma."

#: Vecteur de reference mesure sur le poste x64 avec CETTE sonde. Huit
#: composantes suffisent a distinguer "ca marche" de "ca calcule autre chose".
REFERENCE = [-0.102448, -0.039062, 0.055366, -0.052971,
             -0.020424, 0.098243, -0.016061, 0.051692]

CACHE = os.path.join(tempfile.gettempdir(), "epure-sonde-onnx")


def etape(n, titre):
    print("\n-- %d. %s %s" % (n, titre, "-" * max(0, 58 - len(titre))))


def telecharger(nom):
    chemin = os.path.join(CACHE, nom)
    rel, sha = FICHIERS[nom]
    if os.path.exists(chemin):
        if hashlib.sha256(open(chemin, "rb").read()).hexdigest() == sha:
            print("   %s deja present, sha256 conforme" % nom)
            return chemin
        os.remove(chemin)
    print("   telechargement de %s ..." % nom)
    t0 = time.perf_counter()
    urllib.request.urlretrieve(BASE + "/" + rel, chemin)
    donnees = open(chemin, "rb").read()
    digest = hashlib.sha256(donnees).hexdigest()
    print("   %.1f Mo en %.1f s, sha256 %s"
          % (len(donnees) / 1e6, time.perf_counter() - t0,
             "conforme" if digest == sha else "DIVERGENT (" + digest + ")"))
    if digest != sha:
        raise SystemExit("ECHEC : le fichier telecharge ne correspond pas a l'empreinte attendue.")
    return chemin


def tokeniser(phrase, vocab):
    """WordPiece minimal, juste assez pour cette phrase (la vraie implementation
    vit dans le depot ; ici on ne teste pas le tokenizer mais onnxruntime).
    """
    for signe in ">()":
        phrase = phrase.replace(signe, " " + signe + " ")
    ids = [vocab["[CLS]"]]
    for mot in phrase.lower().split():
        debut = 0
        while debut < len(mot):
            fin = len(mot)
            trouve = None
            while debut < fin:
                bout = mot[debut:fin] if debut == 0 else "##" + mot[debut:fin]
                if bout in vocab:
                    trouve = vocab[bout]
                    break
                fin -= 1
            if trouve is None:
                ids.append(vocab["[UNK]"])
                break
            ids.append(trouve)
            debut = fin
    ids.append(vocab["[SEP]"])
    return ids


def main():
    print("Python %s -- %s" % (sys.version.split()[0], sys.executable))
    print("machine : %s | %s" % (platform.machine(), platform.platform()))
    os.makedirs(CACHE, exist_ok=True)

    etape(1, "installation de la wheel onnxruntime")
    res = subprocess.run([sys.executable, "-m", "pip", "install", "onnxruntime"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    for ligne in (res.stdout + res.stderr).strip().splitlines()[-6:]:
        print("   " + ligne)
    if res.returncode != 0:
        raise SystemExit("ECHEC etape 1 : aucune wheel installable pour cette machine.")

    etape(2, "signature Authenticode des binaires livres")
    ps = ("$m = & '%s' -c \"import onnxruntime,os;print(os.path.dirname(onnxruntime.__file__))\";"
          "Get-ChildItem $m -Recurse -Include *.pyd,*.dll | ForEach-Object {"
          "$s = Get-AuthenticodeSignature $_.FullName;"
          "'{0,-42} {1,-10} {2}' -f $_.Name, $s.Status, $s.SignerCertificate.Subject }"
          % sys.executable.replace("'", "''"))
    res = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    for ligne in (res.stdout or res.stderr).strip().splitlines()[:8]:
        print("   " + ligne)

    etape(3, "import -- c'est ici que sklearn tombait")
    t0 = time.perf_counter()
    import onnxruntime as ort
    print("   OK en %.2f s -- onnxruntime %s" % (time.perf_counter() - t0, ort.__version__))
    print("   providers : %s" % ort.get_available_providers())
    import numpy as np
    print("   numpy %s importe" % np.__version__)

    etape(4, "execution reelle du graphe (import reussi != inference reussie)")
    modele = telecharger("model.onnx")
    fichier_vocab = telecharger("vocab.txt")
    t0 = time.perf_counter()
    sess = ort.InferenceSession(modele, providers=["CPUExecutionProvider"])
    print("   session construite en %.2f s" % (time.perf_counter() - t0))

    vocab = {}
    with open(fichier_vocab, encoding="utf-8") as f:
        for i, ligne in enumerate(f):
            vocab[ligne.rstrip("\n")] = i
    ids = tokeniser(PHRASE, vocab)
    entrees = {i.name for i in sess.get_inputs()}
    tab = np.array([ids], dtype=np.int64)
    feed = {"input_ids": tab, "attention_mask": np.ones_like(tab),
            "token_type_ids": np.zeros_like(tab)}
    t0 = time.perf_counter()
    sortie = sess.run(None, {k: v for k, v in feed.items() if k in entrees})[0]
    print("   inference en %.2f s, forme %s, %d tokens"
          % (time.perf_counter() - t0, sortie.shape, len(ids)))
    vecteur = sortie.mean(axis=1)[0]
    vecteur = vecteur / np.linalg.norm(vecteur)

    etape(5, "le vecteur est-il le bon")
    obtenu = [round(float(x), 6) for x in vecteur[:8]]
    print("   obtenu    : %s" % obtenu)
    if REFERENCE is None:
        print("   (pas de reference figee dans ce fichier -- valeurs ci-dessus a y reporter)")
    else:
        print("   reference : %s" % REFERENCE)
        ecart = max(abs(a - b) for a, b in zip(obtenu, REFERENCE))
        print("   ecart max : %.6f" % ecart)
        if ecart > 1e-3:
            raise SystemExit("ECHEC : onnxruntime tourne mais ne calcule pas le bon resultat.")

    print("\n" + "=" * 66)
    print("VERDICT : onnxruntime est utilisable sur cette machine.")
    print("=" * 66)


if __name__ == "__main__":
    main()
