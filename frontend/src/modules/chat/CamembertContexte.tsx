/**
 * Camembert du contexte restant — une JAUGE, pas un camembert à deux parts.
 *
 * La distinction n'est pas cosmétique, c'est ce qui rend l'objet lisible. Un
 * camembert à deux parts représente deux GRANDEURS comparables (60 % de ceci,
 * 40 % de cela) : deux teintes, une légende, une lecture de proportions. Ici il
 * n'y a qu'UNE grandeur — le contexte occupé — mesurée contre une limite fixe et
 * connue d'avance. La part non occupée n'est pas une seconde donnée, c'est le
 * FOND de la jauge. Elle se dessine donc dans une version éteinte de la teinte
 * du remplissage, jamais comme une couleur autonome à interpréter.
 *
 * ── Ce que la jauge encode, et dans quel sens ────────────────────────────────
 *
 * L'arc REMPLI est l'espace RESTANT : plein à fil neuf, vide quand la fenêtre
 * est saturée. C'est la lecture « jauge de carburant » — la seule qui rende
 * l'alerte immédiate (« ça se vide ») ; une jauge qui se remplit oblige à savoir
 * qu'un arc long est une mauvaise nouvelle. Les seuils portent donc sur le
 * restant : au-dessus de 50 % vert, de 20 à 50 % ambre, sous 20 % rouge.
 *
 * ── Le nombre est TOUJOURS visible, et ce n'est pas un ornement ──────────────
 *
 * Mesuré le 2026-09-20 sur les jetons réels (`src/styles/tokens.css`), avec le
 * validateur de palette du projet :
 *
 *     thème sombre, sur `bg-elevated` #241f17  → les trois tons passent 3:1
 *     thème clair,  sur #ffffff                → 1,74 / 1,67 / 2,77 : SOUS 3:1
 *
 * Sur le thème clair, un anneau de 3 px dans ces tons est donc à la limite de
 * l'invisible sur le fond crème. La règle du projet est explicite : un contraste
 * insuffisant oblige à un RELIEF, c'est-à-dire un libellé visible — il n'est pas
 * facultatif. D'où ce pourcentage écrit à côté de l'anneau, qui n'est pas une
 * redondance de confort mais la condition de lisibilité du thème clair.
 *
 * Deuxième raison, mesurée elle aussi : l'écart ambre↔vert vaut ΔE 7,3 en
 * protanopie. En dessous de 8, l'écart de teinte n'est plus une information
 * fiable — il ne redevient légitime qu'accompagné d'un second canal. Les deux
 * canaux sont ici la LONGUEUR de l'arc (qui porte la donnée) et le NOMBRE écrit.
 * La couleur ne fait que doubler ce que les deux autres disent déjà ; elle n'est
 * jamais seule à porter quoi que ce soit.
 *
 * ── Pourquoi le nombre ne prend PAS la couleur de la sévérité ────────────────
 *
 * Le texte porte les jetons de texte (`text-secondary`), pas la teinte de
 * données. Un ambre clair en corps 11 px est illisible, et colorer les chiffres
 * ferait porter l'information par la couleur seule — exactement ce que le canal
 * multiple ci-dessus cherche à éviter. L'identité vient de l'anneau, à côté.
 *
 * ── Détails qui ont l'air de détails ─────────────────────────────────────────
 *
 * - **Bouts d'arc carrés (`butt`), pas arrondis.** Sur un rayon de 10 px, un
 *   bout arrondi déborde d'environ 1,5 px à chaque extrémité, soit ~5 % de la
 *   circonférence. Une jauge à 5 % se dessinerait à ~10 % : un doublement dans
 *   la zone précise où l'information compte le plus (le fil est presque plein).
 * - **`pathLength={100}`** : la longueur du trait vaut 100, donc le pourcentage
 *   s'écrit directement dans `strokeDasharray` sans calcul de circonférence —
 *   rien à arrondir, rien à oublier de mettre à jour si le rayon change.
 * - **Rotation par l'attribut SVG** (`<g transform="rotate(-90 12 12)">`) et non
 *   par une classe CSS : l'origine d'une transformation CSS sur un élément SVG
 *   dépend de `transform-box`, et le repère du `viewBox` est ici sans ambiguïté.
 * - **`font-mono`** sur le nombre : la valeur change à chaque tour, une police à
 *   chasse fixe évite que le bandeau se réorganise à chaque chiffre.
 * - **Une infobulle NATIVE (`title=`)**, pas le composant `Tooltip` partagé :
 *   celui-ci est en `whitespace-nowrap`, et centré sur un élément collé au bord
 *   droit du bandeau il sortirait de l'écran. Les infobulles natives se replient
 *   seules. C'est aussi l'usage déjà en place dans cet en-tête.
 *
 * L'infobulle ne fait qu'AJOUTER (le détail des tokens, l'origine du chiffre) :
 * elle ne conditionne rien, le nombre et l'arc sont là sans elle.
 */

interface CamembertContexteProps {
  /** Fenêtre de contexte du modèle ACTIF, en tokens. `null` = inconnue. */
  fenetre: number | null
  /** Tokens du contexte au dernier tour. `null` = aucun tour encore mesuré. */
  utilise: number | null
  /** Origine du chiffre de `fenetre`, nommée par le backend (« ollama /api/ps »). */
  source?: string | null
}

/**
 * Teinte de la sévérité, sur le RESTANT. Les trois noms de classes sont écrits
 * en toutes lettres : Tailwind lit le source statiquement, une classe composée
 * à l'exécution ne serait pas générée dans la feuille de style.
 */
function teinteSevreur(restantPourcent: number): string {
  if (restantPourcent < 20) return 'stroke-error'
  if (restantPourcent <= 50) return 'stroke-warning'
  return 'stroke-success'
}

const nombre = (n: number) => n.toLocaleString('fr-FR')

export default function CamembertContexte({ fenetre, utilise, source }: CamembertContexteProps) {
  // Deux absences qui n'en font qu'une : sans dénominateur il n'y a pas de
  // proportion, et sans numérateur il n'y a rien à proportionner. Dans les deux
  // cas on n'affiche RIEN — pas un anneau vide, pas un « ? % », pas un « 100 % »
  // de consolation. Un indicateur qui invente un chiffre est pire que pas
  // d'indicateur : la règle est celle de `core/fenetre_contexte.py`, qui rend
  // `None` plutôt qu'un défaut inventé.
  if (fenetre === null || fenetre <= 0) return null
  if (utilise === null) return null

  // Borné avant de diviser : un backend qui annoncerait plus de tokens que la
  // fenêtre n'en tient (changement de `num_ctx` en cours de session, modèle
  // rechargé) donnerait sinon un restant négatif, donc un pourcentage négatif et
  // un `strokeDasharray` absurde.
  const utilise_borne = Math.min(Math.max(utilise, 0), fenetre)
  const restant = fenetre - utilise_borne
  const pourcent = Math.round((restant / fenetre) * 100)
  const teinte = teinteSevreur(pourcent)

  const detail = `${nombre(utilise_borne)} / ${nombre(fenetre)} tokens occupés`
  const origine = source ? ` · fenêtre lue sur ${source}` : ''
  const alerte = pourcent < 20 ? ' · il reste peu de place' : ''
  const etat = `Contexte restant : ${pourcent} % — ${detail}`

  return (
    <div
      className="flex items-center gap-1.5 px-2 py-1.5 rounded-md border border-line bg-elevated shrink-0"
      title={`${etat}${origine}${alerte}`}
    >
      <svg viewBox="0 0 24 24" width={24} height={24} role="img" aria-label={etat} className="shrink-0">
        <g transform="rotate(-90 12 12)">
          {/* Le fond de la jauge : même teinte, éteinte. Pas de gris neutre —
              la sévérité doit se lire sur tout l'anneau, pas seulement sur l'arc. */}
          <circle cx={12} cy={12} r={10} fill="none" strokeWidth={3} className={`${teinte} opacity-20`} />

          {/* À 0 % il n'y a rien à dessiner ; à 100 % le tiret ferait « 100 0 »,
              un motif dégénéré. Les deux cas sortent donc du `strokeDasharray`. */}
          {pourcent > 0 && pourcent < 100 && (
            <circle
              cx={12}
              cy={12}
              r={10}
              fill="none"
              strokeWidth={3}
              strokeLinecap="butt"
              pathLength={100}
              strokeDasharray={`${pourcent} ${100 - pourcent}`}
              className={teinte}
            />
          )}
          {pourcent >= 100 && (
            <circle cx={12} cy={12} r={10} fill="none" strokeWidth={3} className={teinte} />
          )}
        </g>
      </svg>

      <span className="text-[11px] font-mono text-secondary min-w-[2.25rem] text-right">
        {pourcent} %
      </span>
    </div>
  )
}
