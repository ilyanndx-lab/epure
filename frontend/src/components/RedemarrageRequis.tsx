/**
 * Bandeau « Redémarrage requis » — affiché dans l'Atelier et les Réglages.
 *
 * Un changement de module ne prend effet qu'au redémarrage du backend (cf.
 * `src/redemarrage.ts`). Ce bandeau relit l'écart calculé par le backend à son
 * montage, après chaque changement signalé (installation, suppression,
 * approbation) et quand `modules_activés` change — il n'est JAMAIS la source
 * de « requis » : il l'affiche.
 *
 * Quatre états, parce que l'utilisateur doit toujours savoir ce qui se passe :
 *  - repos : bouton « Redémarrer maintenant » (ou rien si aucun écart) ;
 *  - attente : le tray redémarre le backend, on sonde `/health` jusqu'à un
 *    autre `boot_id`, puis la page se recharge — ce rechargement relit aussi
 *    `GET /modules` et le glob des composants générés ;
 *  - manuel : pas de tray (backend lancé à la main) — on le dit, et on
 *    continue de sonder pour recharger dès que l'utilisateur a relancé ;
 *  - échec : le backend n'est pas revenu dans le temps imparti.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { RotateCw, AlertTriangle } from 'lucide-react'
import { Button } from './ui'
import { useInstanceConfig } from '../instance'
import {
  attendreRetour, demanderRedemarrage, fluxEnCours, lireEtat, surChangementModules,
  type EtatRedemarrage,
} from '../redemarrage'

type Phase = 'repos' | 'attente' | 'manuel' | 'echec'

const LIBELLES: Record<string, string> = {
  'à charger': 'à charger',
  'à décharger': 'à retirer (répond encore)',
  'modifié': 'modifié',
}

export default function RedemarrageRequis() {
  const [etat, setEtat] = useState<EtatRedemarrage | null>(null)
  const [phase, setPhase] = useState<Phase>('repos')
  const [message, setMessage] = useState('')
  const vivant = useRef(true)
  const { modules_activés } = useInstanceConfig()
  const cleActives = JSON.stringify(modules_activés ?? [])

  const relire = useCallback(() => {
    void lireEtat().then(e => { if (vivant.current) setEtat(e) })
  }, [])

  useEffect(() => {
    vivant.current = true
    const off = surChangementModules(relire)
    return () => { vivant.current = false; off() }
  }, [relire])

  // Activer/désactiver passe par PUT /instance/config : relire à chaque
  // changement de la liste, sans que les Réglages aient à y penser.
  useEffect(() => { relire() }, [relire, cleActives])

  const redemarrer = useCallback(async () => {
    const encours = fluxEnCours()
    if (encours.length && !window.confirm(
      `En cours : ${encours.join(', ')}.\n\n`
      + 'Le redémarrage du backend va l\'interrompre. Redémarrer quand même ?'
    )) return
    const ancien = etat?.boot_id ?? null
    const rep = await demanderRedemarrage('changement de modules (interface)')
    const annule = () => !vivant.current
    if (rep.déclenché) {
      setPhase('attente'); setMessage('')
      const revenu = await attendreRetour(ancien, { annule })
      if (!vivant.current) return
      if (revenu) { window.location.reload(); return }
      setPhase('echec')
      setMessage('Le backend n\'est pas revenu après 60 s. Regardez epure_tray.log, '
        + 'ou utilisez « Redémarrer » dans le menu de l\'icône Épure.')
      return
    }
    setPhase('manuel')
    setMessage(rep.message ?? 'Redémarrage manuel requis : relancez le backend.')
    // Sans tray, c'est l'utilisateur qui relance : on recharge dès qu'un autre
    // processus répond, sans lui demander de revenir cliquer.
    if (await attendreRetour(ancien, { limiteMs: 30 * 60_000, pasMs: 3_000, annule })) {
      window.location.reload()
    }
  }, [etat])

  if (phase === 'repos' && !etat?.requis) return null

  return (
    <div role="status" className="rounded-md border border-warning/40 bg-warning/5 px-4 py-3 space-y-2 max-w-2xl">
      <div className="flex items-center gap-2 text-sm text-warning font-medium">
        {phase === 'attente'
          ? <RotateCw size={14} className="animate-spin" />
          : <AlertTriangle size={14} />}
        {phase === 'attente' ? 'Redémarrage du backend…' : 'Redémarrage requis'}
      </div>
      {phase === 'repos' && etat && (
        <>
          <p className="text-xs text-secondary">
            Les changements de modules prennent effet au redémarrage du backend
            (quelques secondes ; une génération en cours serait interrompue).
          </p>
          {etat.écarts.length > 0 && (
            <ul className="text-xs text-muted list-disc pl-5">
              {etat.écarts.map(e => (
                <li key={e.id}>{e.id} — {LIBELLES[e.changement] ?? e.changement}</li>
              ))}
            </ul>
          )}
          <Button size="sm" variant="primary" icon={<RotateCw size={12} />} onClick={() => void redemarrer()}>
            Redémarrer maintenant
          </Button>
        </>
      )}
      {phase === 'attente' && (
        <p className="text-xs text-secondary">
          Compter 6 à 10 secondes. La page se rechargera d'elle-même.
        </p>
      )}
      {(phase === 'manuel' || phase === 'echec') && (
        <p className="text-xs text-secondary">{message}</p>
      )}
    </div>
  )
}
