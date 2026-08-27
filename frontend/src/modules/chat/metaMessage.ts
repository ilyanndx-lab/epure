/**
 * Ce qu'un message peut dire de lui-même : quand, et par quel modèle.
 *
 * ── La distinction que tout ce module sert à préserver ────────────────────────
 *
 * Trois situations, et il serait faux d'en confondre deux :
 *
 * | cas | horodatage | modèle |
 * |---|---|---|
 * | réponse écrite depuis ce changement | l'instant posé par le serveur | **modèle** — celui qui l'a produite |
 * | message tapé depuis ce changement | l'instant posé par le serveur | **envoyé à** — celui à qui la question est posée |
 * | message d'avant ce changement | **non disponible** | **non disponible** |
 *
 * Le LIBELLÉ change avec le rôle, pas la valeur. C'est lui qui empêche
 * d'affirmer quelque chose de faux : écrire « modèle : qwen2.5 » sous un message
 * tapé par l'utilisateur laisserait entendre que ce texte vient du modèle.
 * « envoyé à » dit exactement ce qui s'est passé.
 *
 * La première version n'affichait aucune ligne sous un message utilisateur, au
 * motif qu'il n'est produit par aucun modèle. Vrai de la formulation, faux du
 * besoin : dans un fil où l'on change de modèle en cours de route, savoir à qui
 * une question donnée a été posée est précisément ce qu'on cherche.
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
  /** Modèle, ou `NON_DISPONIBLE` s'il n'a pas été enregistré. */
  modele: string
  /**
   * Intitulé de la ligne modèle : « modèle » pour une réponse, « envoyé à »
   * pour une question. C'est ce mot qui porte la différence de sens.
   */
  libelleModele: string
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
 * `estUtilisateur` ne décide plus de la PRÉSENCE de la ligne modèle mais de son
 * INTITULÉ. Un champ absent donne `NON_DISPONIBLE` dans les deux cas — jamais
 * une valeur devinée depuis la conversation.
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
    modele: modele || NON_DISPONIBLE,
    libelleModele: estUtilisateur ? 'envoyé à' : 'modèle',
  }
}
