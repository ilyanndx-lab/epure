/**
 * Ce qu'un message peut dire de lui-même : quand, et par quel modèle.
 *
 * ── La distinction que tout ce module sert à préserver ────────────────────────
 *
 * Trois situations, et il serait faux d'en confondre deux :
 *
 * | cas | horodatage | modèle |
 * |---|---|---|
 * | réponse écrite depuis ce changement | l'instant posé par le serveur | le modèle qui l'a produite |
 * | message tapé par l'utilisateur | l'instant posé par le serveur | **aucune ligne** — un message tapé n'est produit par aucun modèle |
 * | message d'avant ce changement | **non disponible** | **non disponible** |
 *
 * « Pas de modèle par nature » et « on ne sait pas » ne s'affichent donc pas
 * pareil. Les fondre en un seul « non disponible » ferait croire à une donnée
 * perdue là où il n'y a rien à perdre.
 *
 * ⚠️ **Ne jamais combler un champ absent depuis la conversation.** Elle porte un
 * `modèle`, mais c'est le DERNIER utilisé — il a pu changer plusieurs fois, et
 * s'en servir attribuerait des réponses à un modèle qui ne les a pas produites.
 * L'absence est une information ; l'inventer serait une erreur silencieuse, la
 * pire espèce.
 */

/** Ce que l'interface affiche pour un champ qu'on ne peut pas connaître. */
export const NON_DISPONIBLE = 'non disponible'

export interface MetaAffichable {
  /** Date lisible, ou `NON_DISPONIBLE`. */
  date: string
  /** Heure lisible, ou `NON_DISPONIBLE`. */
  heure: string
  /**
   * Modèle, `NON_DISPONIBLE`, ou `null` quand la notion ne s'applique pas
   * (message utilisateur) — auquel cas l'interface n'affiche pas la ligne.
   */
  modele: string | null
}

/**
 * Découpe un horodatage ISO local en date et heure lisibles.
 *
 * Analyse manuelle plutôt que `new Date(...)`, et c'est délibéré : le backend
 * écrit `datetime.now().isoformat(timespec="seconds")`, donc une heure LOCALE
 * SANS fuseau (`2026-08-27T14:03:11`). Les moteurs JS interprètent une telle
 * chaîne comme locale — mais la même valeur passée à `toLocaleString()` après un
 * aller-retour mal maîtrisé peut se retrouver décalée. Découper la chaîne rend
 * exactement ce que le serveur a écrit, ce qui est la seule chose qu'on sache.
 */
function decouper(horodatage: string): { date: string; heure: string } | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?/.exec(horodatage)
  if (!m) return null
  const [, annee, mois, jour, hh, mm, ss] = m
  return {
    date: `${jour}/${mois}/${annee}`,
    heure: ss ? `${hh}:${mm}:${ss}` : `${hh}:${mm}`,
  }
}

/**
 * Métadonnées prêtes à afficher pour un message.
 *
 * `estUtilisateur` décide de la troisième ligne : absente pour un message tapé,
 * `NON_DISPONIBLE` pour une réponse qui n'a pas le champ.
 */
export function metaAffichable(
  horodatage: string | undefined,
  modele: string | undefined,
  estUtilisateur: boolean,
): MetaAffichable {
  const decoupe = horodatage ? decouper(horodatage) : null
  return {
    date: decoupe ? decoupe.date : NON_DISPONIBLE,
    heure: decoupe ? decoupe.heure : NON_DISPONIBLE,
    modele: estUtilisateur ? null : (modele || NON_DISPONIBLE),
  }
}
