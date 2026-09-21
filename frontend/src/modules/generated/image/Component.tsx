import { useCallback, useEffect, useRef, useState, type ChangeEvent } from 'react'
import { Image as ImageIcon, Sparkles, X } from 'lucide-react'
import type { SharedModuleProps } from '../../registry'
import { usePersistentState } from '../../../usePersistentState'
import { API, apiFetch } from '../../../api'
import { Button, Card, Textarea } from '../../../components/ui'

/**
 * Module Image — génération via ComfyUI (`backend/modules/image`), txt2img
 * et img2img.
 *
 * Portée tenue : prompt (+ image source optionnelle) → bouton → résultat.
 * Toujours pas de masque, pas de bibliothèque/historique.
 *
 * Champ multipart de l'image source : **`image`**, pas `image_source` — lu
 * dans `router.py::image_generate` (`image: Optional[UploadFile] =
 * File(None)`), pas supposé. `denoise` n'a d'effet que si `image` est fourni
 * (branche img2img, cf. docstring backend) ; envoyé seulement dans ce cas.
 *
 * LoRA "Réalisme" (`use_lora`/`lora_strength`) : un seul LoRA fixe côté
 * backend (`super-realism.safetensors`), pas un sélecteur — toggle + slider
 * envoyés seulement si activé, même patron que `denoise` ci-dessus. Défaut
 * désactivé : comportement identique à avant cette option.
 *
 * Le résultat (et la source, pour l'aperçu et la comparaison) sont des
 * chemins/fichiers, jamais des octets bruts embarqués dans le JSON : on
 * récupère le résultat via `GET /image/file/<filename>` — une route
 * authentifiée (§6 de CLAUDE.md), donc un `<img src=...>` nu échouerait en
 * 401. On passe par `apiFetch` + `URL.createObjectURL`, comme
 * `slides/Component.tsx::exportDeck` pour `/slides/decks/:id/export`. La
 * source, elle, n'a jamais besoin du serveur : son URL objet vient
 * directement du `File` choisi par l'utilisateur.
 */
export default function ImageModule(_props: SharedModuleProps) {
  const [prompt, setPrompt] = usePersistentState<string>('epure.image.prompt', '')
  // Persisté comme les autres réglages de génération (cf. `slides.genN`) :
  // survit à un F5, contrairement au fichier lui-même (un `File` ne se
  // sérialise pas dans localStorage, donc `sourceFile` reste un `useState`).
  const [denoise, setDenoise] = usePersistentState<number>('epure.image.denoise', 0.75)
  // LoRA "Réalisme" (super-realism.safetensors, fixe en dur côté backend —
  // pas de sélecteur, décision de scope actée, cf. router.py). Défaut
  // désactivé : comportement identique à avant cette option.
  const [useLora, setUseLora] = usePersistentState<boolean>('epure.image.useLora', false)
  const [loraStrength, setLoraStrength] = usePersistentState<number>('epure.image.loraStrength', 0.6)
  const [sourceFile, setSourceFile] = useState<File | null>(null)
  const [sourcePreviewUrl, setSourcePreviewUrl] = useState<string | null>(null)
  const [generating, setGenerating] = useState(false)
  const [elapsed, setElapsed] = useState(0)
  const [error, setError] = useState<string | null>(null)
  // Éphémère et non persisté : une URL objet ne survit pas à un F5, la
  // restaurer depuis localStorage donnerait une image cassée.
  const [resultUrl, setResultUrl] = useState<string | null>(null)

  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const resultUrlRef = useRef<string | null>(null)
  const sourcePreviewRef = useRef<string | null>(null)
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // Rafraîchies dans un EFFET et non pendant le rendu — même raison que
  // `usePersistentState.ts::latest` : muter un ref pendant le rendu est ce
  // que la règle `react-hooks/refs` signale (« Cannot update ref during
  // render »), un rendu abandonné y laisserait une valeur jamais commitée.
  useEffect(() => {
    resultUrlRef.current = resultUrl
  }, [resultUrl])
  useEffect(() => {
    sourcePreviewRef.current = sourcePreviewUrl
  }, [sourcePreviewUrl])

  // Au démontage : libère les URL objet en cours (résultat + aperçu source)
  // et le minuteur, s'il y en a.
  useEffect(() => {
    return () => {
      if (resultUrlRef.current) URL.revokeObjectURL(resultUrlRef.current)
      if (sourcePreviewRef.current) URL.revokeObjectURL(sourcePreviewRef.current)
      if (timerRef.current) clearInterval(timerRef.current)
    }
  }, [])

  // Point d'écriture unique de `sourceFile`/`sourcePreviewUrl` : révoque
  // l'aperçu précédent avant d'en poser un nouveau (ou aucun, si `file` est
  // `null` — cas du bouton « retirer »), pour ne jamais en faire fuiter un.
  const choisirFichier = useCallback((file: File | null) => {
    setSourcePreviewUrl(prev => {
      if (prev) URL.revokeObjectURL(prev)
      return file ? URL.createObjectURL(file) : null
    })
    setSourceFile(file)
  }, [])

  const onFileChange = useCallback((e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0] ?? null
    // Validation client minimale — évite un aller-retour réseau évident sur
    // un type de fichier qui ne peut pas être une image. Le backend, lui,
    // lit les octets tels quels (`image.read()`) : c'est ComfyUI qui
    // refusera un contenu invalide, avec son propre message d'erreur — pas
    // une seconde règle à maintenir en synchronisation ici.
    if (file && !file.type.startsWith('image/')) {
      setError("Le fichier choisi n'est pas une image")
      e.target.value = ''
      // Retire aussi une sélection valide précédente : sans ça, l'input
      // affiché redevient vide alors que `sourceFile` garderait l'ancien
      // fichier, et `generate()` l'envoie encore — état incohérent entre ce
      // que l'utilisateur VOIT (rien) et ce qui serait réellement transmis.
      choisirFichier(null)
      return
    }
    setError(null)
    choisirFichier(file)
  }, [choisirFichier])

  const retirerImageSource = useCallback(() => {
    choisirFichier(null)
    if (fileInputRef.current) fileInputRef.current.value = ''
  }, [choisirFichier])

  const generate = useCallback(async () => {
    if (!prompt.trim() || generating) return
    setGenerating(true)
    setError(null)
    setElapsed(0)
    const t0 = Date.now()
    timerRef.current = setInterval(() => setElapsed(Math.round((Date.now() - t0) / 1000)), 1000)

    try {
      const body = new FormData()
      body.append('prompt', prompt.trim())
      if (sourceFile) {
        // Nom de champ exact attendu par `image_generate` (router.py) : `image`.
        body.append('image', sourceFile)
        body.append('denoise', String(denoise))
      }
      if (useLora) {
        body.append('use_lora', 'true')
        body.append('lora_strength', String(loraStrength))
      }
      const res = await apiFetch(`${API}/image/generate`, { method: 'POST', body })
      if (!res.ok) {
        // Le backend écrit ses erreurs (ComfyUI injoignable, refusé, OOM…)
        // pour être lues telles quelles — cf. router.py::ComfyUIError.
        let detail = `HTTP ${res.status}`
        try {
          const d: { detail?: unknown } = await res.json()
          if (typeof d.detail === 'string') detail = d.detail
        } catch { /* corps non JSON — le statut HTTP suffit */ }
        throw new Error(detail)
      }
      const data: { filename?: unknown } = await res.json()
      if (typeof data.filename !== 'string') {
        throw new Error('Réponse du serveur sans nom de fichier')
      }

      const imgRes = await apiFetch(`${API}/image/file/${encodeURIComponent(data.filename)}`)
      if (!imgRes.ok) throw new Error(`Image générée mais illisible (HTTP ${imgRes.status})`)
      const blob = await imgRes.blob()
      const url = URL.createObjectURL(blob)
      setResultUrl(prev => {
        if (prev) URL.revokeObjectURL(prev)
        return url
      })
    } catch (err) {
      console.error('POST /image/generate:', err)
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      if (timerRef.current) {
        clearInterval(timerRef.current)
        timerRef.current = null
      }
      setGenerating(false)
    }
  }, [prompt, generating, sourceFile, denoise, useLora, loraStrength])

  const canGenerate = prompt.trim().length > 0 && !generating

  return (
    <main className="flex flex-col flex-1 overflow-y-auto px-8 py-8">
      <div className="max-w-xl space-y-5">
        <h1 className="text-lg font-semibold text-primary flex items-center gap-2">
          <ImageIcon size={18} className="text-accent" /> Image
        </h1>

        <Card className="space-y-4">
          <div>
            <label htmlFor="image-prompt" className="text-xs text-muted uppercase tracking-wide block mb-2">
              Décrivez l'image
            </label>
            <Textarea
              id="image-prompt"
              value={prompt}
              onChange={e => setPrompt(e.target.value)}
              rows={4}
              disabled={generating}
              className="w-full disabled:opacity-40"
              placeholder="Ex. : un renard roux dans une forêt enneigée, lumière du matin"
            />
          </div>

          <div>
            <label htmlFor="image-source" className="text-xs text-muted uppercase tracking-wide block mb-2">
              Image source (optionnel — active le mode img2img)
            </label>
            <div className="flex items-center gap-2">
              <input
                id="image-source"
                ref={fileInputRef}
                type="file"
                accept="image/*"
                disabled={generating}
                onChange={onFileChange}
                className="flex-1 text-xs text-muted file:mr-2 file:px-2.5 file:py-1 file:rounded-md file:border file:border-line file:bg-elevated file:text-secondary file:text-xs disabled:opacity-40"
              />
              {sourceFile && (
                <button
                  type="button"
                  onClick={retirerImageSource}
                  disabled={generating}
                  className="p-1 rounded-sm text-muted hover:text-error transition-colors duration-150 disabled:opacity-40 shrink-0"
                  title="Retirer l'image source"
                >
                  <X size={13} />
                </button>
              )}
            </div>
          </div>

          {sourceFile && (
            <div>
              <label htmlFor="image-denoise" className="text-xs text-muted uppercase tracking-wide block mb-2">
                Fidélité à la source (denoise)
              </label>
              <div className="flex items-center gap-3">
                <input
                  id="image-denoise"
                  type="range"
                  min={0}
                  max={1}
                  step={0.05}
                  value={denoise}
                  onChange={e => setDenoise(Number(e.target.value))}
                  disabled={generating}
                  className="flex-1 accent-[--accent-primary]"
                />
                <span className="text-xs font-mono text-secondary w-10 text-right">{denoise.toFixed(2)}</span>
              </div>
              <p className="text-xs text-muted mt-1">
                Bas = proche de la source, haut = s'en éloigne (défaut backend : 0,75).
              </p>
            </div>
          )}

          <div>
            <label className="flex items-center gap-2 text-xs text-secondary cursor-pointer">
              <input
                type="checkbox"
                checked={useLora}
                onChange={e => setUseLora(e.target.checked)}
                disabled={generating}
                className="accent-[--accent-primary]"
              />
              Réalisme (LoRA)
            </label>
          </div>

          {useLora && (
            <div>
              <label htmlFor="image-lora-strength" className="text-xs text-muted uppercase tracking-wide block mb-2">
                Intensité du LoRA
              </label>
              <div className="flex items-center gap-3">
                <input
                  id="image-lora-strength"
                  type="range"
                  min={0}
                  max={1}
                  step={0.05}
                  value={loraStrength}
                  onChange={e => setLoraStrength(Number(e.target.value))}
                  disabled={generating}
                  className="flex-1 accent-[--accent-primary]"
                />
                <span className="text-xs font-mono text-secondary w-10 text-right">{loraStrength.toFixed(2)}</span>
              </div>
              {/* Compatibilité Dev/Schnell non garantie pour ce LoRA précis, cf. router.py */}
              <p className="text-xs text-muted mt-1">
                Style photo-réaliste (défaut backend : 0,60, non mesuré).
              </p>
            </div>
          )}

          <Button variant="primary" onClick={generate} disabled={!canGenerate} icon={<Sparkles size={14} />}>
            {generating ? `Génération… ${elapsed}s` : sourceFile ? 'Générer (img2img)' : 'Générer'}
          </Button>
        </Card>

        {generating && (
          <Card accent="secondary" className="py-4">
            <p className="text-xs text-muted">
              Génération sur une machine distante par wifi partagé — de quelques secondes à
              plusieurs minutes selon la charge (et le mode : txt2img est plus rapide qu'img2img).{' '}
              <span className="text-accent2">{elapsed}s</span>
            </p>
          </Card>
        )}

        {error && <p className="text-xs text-error whitespace-pre-wrap">{error}</p>}

        {(sourcePreviewUrl || resultUrl) && (
          <div className="flex gap-3">
            {sourcePreviewUrl && (
              <div className="flex-1 min-w-0 space-y-1">
                {resultUrl && <p className="text-xs text-muted uppercase tracking-wide">Source</p>}
                <div className="rounded-md border border-line overflow-hidden bg-elevated">
                  <img src={sourcePreviewUrl} alt="Image source" className="w-full h-auto block" />
                </div>
              </div>
            )}
            {resultUrl && (
              <div className="flex-1 min-w-0 space-y-1">
                {sourcePreviewUrl && <p className="text-xs text-muted uppercase tracking-wide">Résultat</p>}
                <div className="rounded-md border border-line overflow-hidden bg-elevated">
                  <img src={resultUrl} alt="Image générée" className="w-full h-auto block" />
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </main>
  )
}
