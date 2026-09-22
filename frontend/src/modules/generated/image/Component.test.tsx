import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

import ImageModule from './Component'

/**
 * Module Image — formulaire, état de chargement, affichage du résultat.
 *
 * `POST /image/generate` renvoie un `filename`, jamais des octets (cf.
 * docstring de `router.py`) : le composant fait un second appel authentifié
 * (`GET /image/file/<filename>`) pour récupérer l'image — ce fichier
 * bouchonne donc DEUX routes, pas une.
 */

type ReponseJson = { status?: number; corps: unknown }

/** `/pair` + `/image/generate` bouchonnés ; `/image/file/*` sert un PNG factice. */
function poserFetch(opts: { generate?: ReponseJson; fichier?: { status?: number }; calibrate?: ReponseJson } = {}) {
  // Second paramètre déclaré (même inutilisé) : sans lui, le type inféré des
  // appels de `impl.mock.calls` est un tuple à un seul élément, et
  // `appel[1]` (le `RequestInit` — corps de la requête inspecté plus bas)
  // ne type-checke pas.
  const impl = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
    const url = typeof input === 'string' ? input : String(input)
    if (url.includes('/pair')) {
      return new Response(JSON.stringify({ token: 'jeton-de-test' }), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      })
    }
    if (url.includes('/image/file/')) {
      const { status = 200 } = opts.fichier ?? {}
      return new Response(new Blob(['PNGDATA'], { type: 'image/png' }), { status })
    }
    if (url.includes('/image/calibrate-prompt')) {
      const { status = 200, corps = { prompt: 'a cat, watercolor style' } } = opts.calibrate ?? {}
      return new Response(JSON.stringify(corps), {
        status, headers: { 'Content-Type': 'application/json' },
      })
    }
    if (url.includes('/image/generate')) {
      const { status = 200, corps = { filename: 'sortie.png' } } = opts.generate ?? {}
      return new Response(JSON.stringify(corps), {
        status, headers: { 'Content-Type': 'application/json' },
      })
    }
    return new Response(JSON.stringify({ detail: 'route inattendue' }), { status: 500 })
  })
  vi.stubGlobal('fetch', impl)
  return impl
}

// jsdom n'implémente pas `URL.createObjectURL` — bouchon minimal : le
// composant n'a besoin que d'une chaîne stable à poser en `src`.
beforeEach(() => {
  localStorage.clear()
  vi.stubGlobal('URL', Object.assign(URL, {
    createObjectURL: vi.fn(() => 'blob:mock'),
    revokeObjectURL: vi.fn(),
  }))
})

afterEach(cleanup)

function saisirPrompt(texte: string) {
  fireEvent.change(screen.getByPlaceholderText(/renard roux/), { target: { value: texte } })
}

describe('module image', () => {
  it('désactive Générer sans prompt, l\'active une fois saisi', () => {
    poserFetch()
    render(<ImageModule />)
    const bouton = screen.getByText('Générer').closest('button')!
    expect(bouton.disabled).toBe(true)

    saisirPrompt('un chat en aquarelle')
    expect(bouton.disabled).toBe(false)
  })

  it('affiche un état de chargement pendant la génération', async () => {
    let resoudre!: (r: Response) => void
    const attente = new Promise<Response>(resolve => { resoudre = resolve })
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : String(input)
      if (url.includes('/pair')) return new Response(JSON.stringify({ token: 't' }))
      if (url.includes('/image/generate')) return attente
      if (url.includes('/image/file/')) return new Response(new Blob(['PNGDATA']), { status: 200 })
      return new Response('', { status: 500 })
    }))

    render(<ImageModule />)
    saisirPrompt('un chat')
    fireEvent.click(screen.getByText('Générer'))

    expect(await screen.findByText(/Génération…/)).toBeTruthy()

    resoudre(new Response(JSON.stringify({ filename: 'x.png' }), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }))

    // Laisse la génération se terminer avant la fin du test — sinon la
    // continuation (fetch du fichier, mise à jour d'état) tourne après le
    // démontage du composant par `afterEach(cleanup)`.
    await screen.findByRole('img')
  })

  it('affiche l\'image reçue après une génération réussie', async () => {
    poserFetch({ generate: { corps: { filename: 'sortie.png' } } })
    render(<ImageModule />)

    saisirPrompt('un chat')
    fireEvent.click(screen.getByText('Générer'))

    const img = await screen.findByRole('img')
    expect(img.getAttribute('src')).toBe('blob:mock')
  })

  it('affiche le message d\'erreur renvoyé par le backend (502 ComfyUI)', async () => {
    poserFetch({
      generate: { status: 502, corps: { detail: 'ComfyUI injoignable — génération jamais lancée' } },
    })
    render(<ImageModule />)

    saisirPrompt('un chat')
    fireEvent.click(screen.getByText('Générer'))

    expect(await screen.findByText(/ComfyUI injoignable/)).toBeTruthy()
  })

  // ── img2img : image source + denoise ──────────────────────────────────────

  it('n\'envoie ni image ni denoise sans fichier choisi (non-régression txt2img)', async () => {
    const impl = poserFetch({ generate: { corps: { filename: 'sortie.png' } } })
    render(<ImageModule />)

    saisirPrompt('un chat')
    fireEvent.click(screen.getByText('Générer'))
    await screen.findByRole('img')

    const appel = impl.mock.calls.find(([input]) => String(input).includes('/image/generate'))!
    const body = appel[1]!.body as FormData
    expect(body.has('image')).toBe(false)
    expect(body.has('denoise')).toBe(false)
  })

  it('n\'affiche pas le réglage denoise sans image source', () => {
    poserFetch()
    render(<ImageModule />)
    expect(screen.queryByLabelText(/Fidélité à la source/i)).toBeNull()
  })

  it('envoie le champ multipart `image` (pas `image_source`) et le denoise choisi', async () => {
    const impl = poserFetch({ generate: { corps: { filename: 'sortie.png' } } })
    render(<ImageModule />)
    saisirPrompt('un chat')

    const fichier = new File(['contenu'], 'source.png', { type: 'image/png' })
    fireEvent.change(screen.getByLabelText(/Image source/i), { target: { files: [fichier] } })
    fireEvent.change(screen.getByLabelText(/Fidélité à la source/i), { target: { value: '0.4' } })

    fireEvent.click(screen.getByText('Générer (img2img)'))
    await screen.findByAltText('Image générée')

    const appel = impl.mock.calls.find(([input]) => String(input).includes('/image/generate'))!
    const body = appel[1]!.body as FormData
    expect(body.get('image')).toBe(fichier)
    expect(body.has('image_source')).toBe(false)
    expect(body.get('denoise')).toBe('0.4')
  })

  it('rejette côté client un fichier non-image, sans appeler /image/generate', () => {
    const impl = poserFetch()
    render(<ImageModule />)
    saisirPrompt('un chat')

    const fichier = new File(['contenu'], 'notes.txt', { type: 'text/plain' })
    fireEvent.change(screen.getByLabelText(/Image source/i), { target: { files: [fichier] } })

    expect(screen.getByText(/n'est pas une image/)).toBeTruthy()
    expect(impl.mock.calls.some(([input]) => String(input).includes('/image/generate'))).toBe(false)
  })

  it('un fichier non-image retire aussi une image valide déjà choisie', async () => {
    const impl = poserFetch({ generate: { corps: { filename: 'sortie.png' } } })
    render(<ImageModule />)
    saisirPrompt('un chat')

    const image = new File(['contenu'], 'source.png', { type: 'image/png' })
    fireEvent.change(screen.getByLabelText(/Image source/i), { target: { files: [image] } })
    expect(screen.getByText('Générer (img2img)')).toBeTruthy()

    const texte = new File(['contenu'], 'notes.txt', { type: 'text/plain' })
    fireEvent.change(screen.getByLabelText(/Image source/i), { target: { files: [texte] } })

    // Le bouton redevient "Générer" (txt2img) : l'ancienne sélection valide
    // n'est plus là, pas seulement l'input visuellement vidé.
    expect(screen.getByText('Générer')).toBeTruthy()
    expect(screen.queryByText('Générer (img2img)')).toBeNull()

    fireEvent.click(screen.getByText('Générer'))
    await screen.findByRole('img')

    const appel = impl.mock.calls.find(([input]) => String(input).includes('/image/generate'))!
    const body = appel[1]!.body as FormData
    expect(body.has('image')).toBe(false)
  })

  it('affiche la source et le résultat côte à côte après une génération img2img', async () => {
    poserFetch({ generate: { corps: { filename: 'sortie.png' } } })
    render(<ImageModule />)
    saisirPrompt('un chat')

    const fichier = new File(['contenu'], 'source.png', { type: 'image/png' })
    fireEvent.change(screen.getByLabelText(/Image source/i), { target: { files: [fichier] } })
    fireEvent.click(screen.getByText('Générer (img2img)'))

    await screen.findByAltText('Image générée')
    expect(screen.getByAltText('Image source')).toBeTruthy()
  })

  // ── LoRA "Réalisme" : toggle + intensité ───────────────────────────────────

  it('n\'envoie ni use_lora ni lora_strength toggle désactivé (non-régression)', async () => {
    const impl = poserFetch({ generate: { corps: { filename: 'sortie.png' } } })
    render(<ImageModule />)

    saisirPrompt('un chat')
    fireEvent.click(screen.getByText('Générer'))
    await screen.findByRole('img')

    const appel = impl.mock.calls.find(([input]) => String(input).includes('/image/generate'))!
    const body = appel[1]!.body as FormData
    expect(body.has('use_lora')).toBe(false)
    expect(body.has('lora_strength')).toBe(false)
  })

  it('n\'affiche pas le réglage d\'intensité sans le toggle LoRA activé', () => {
    poserFetch()
    render(<ImageModule />)
    expect(screen.queryByLabelText(/Intensité du LoRA/i)).toBeNull()
  })

  it('envoie use_lora et lora_strength une fois le toggle activé', async () => {
    const impl = poserFetch({ generate: { corps: { filename: 'sortie.png' } } })
    render(<ImageModule />)
    saisirPrompt('un chat')

    fireEvent.click(screen.getByLabelText(/Réalisme \(LoRA\)/i))
    fireEvent.change(screen.getByLabelText(/Intensité du LoRA/i), { target: { value: '0.8' } })
    fireEvent.click(screen.getByText('Générer'))
    await screen.findByRole('img')

    const appel = impl.mock.calls.find(([input]) => String(input).includes('/image/generate'))!
    const body = appel[1]!.body as FormData
    expect(body.get('use_lora')).toBe('true')
    expect(body.get('lora_strength')).toBe('0.8')
  })

  // ── Calibration de prompt (bouton "Calibrer") ──────────────────────────────

  it('affiche le bouton Calibrer, désactivé sans prompt', () => {
    poserFetch()
    render(<ImageModule />)
    const bouton = screen.getByText('Calibrer').closest('button')!
    expect(bouton.disabled).toBe(true)

    saisirPrompt('un chat')
    expect(bouton.disabled).toBe(false)
  })

  it('remplace le contenu du textarea par le résultat calibré, sans appeler /image/generate', async () => {
    const impl = poserFetch({ calibrate: { corps: { prompt: 'a cat, watercolor style' } } })
    render(<ImageModule />)
    saisirPrompt('un chat en aquarelle')

    fireEvent.click(screen.getByText('Calibrer'))
    await screen.findByDisplayValue('a cat, watercolor style')

    expect(impl.mock.calls.some(([input]) => String(input).includes('/image/generate'))).toBe(false)
  })

  it('le résultat calibré reste éditable — le textarea accepte une saisie après calibration', async () => {
    poserFetch({ calibrate: { corps: { prompt: 'a cat, watercolor style' } } })
    render(<ImageModule />)
    saisirPrompt('un chat en aquarelle')

    fireEvent.click(screen.getByText('Calibrer'))
    await screen.findByDisplayValue('a cat, watercolor style')

    saisirPrompt('a cat, watercolor style, dramatic lighting')
    expect(screen.getByDisplayValue('a cat, watercolor style, dramatic lighting')).toBeTruthy()
  })

  it('envoie le prompt tel qu\'édité après calibration, pas le texte calibré figé', async () => {
    const impl = poserFetch({
      calibrate: { corps: { prompt: 'a cat, watercolor style' } },
      generate: { corps: { filename: 'sortie.png' } },
    })
    render(<ImageModule />)
    saisirPrompt('un chat en aquarelle')

    fireEvent.click(screen.getByText('Calibrer'))
    await screen.findByDisplayValue('a cat, watercolor style')

    // Édition APRÈS calibration, avant de générer.
    saisirPrompt('a cat, watercolor style, dramatic lighting')
    fireEvent.click(screen.getByText('Générer'))
    await screen.findByRole('img')

    const appel = impl.mock.calls.find(([input]) => String(input).includes('/image/generate'))!
    const body = appel[1]!.body as FormData
    expect(body.get('prompt')).toBe('a cat, watercolor style, dramatic lighting')
  })

  it('affiche le message d\'erreur renvoyé par le backend en cas d\'échec de calibration', async () => {
    poserFetch({ calibrate: { status: 500, corps: { detail: 'calibration indisponible' } } })
    render(<ImageModule />)
    saisirPrompt('un chat')

    fireEvent.click(screen.getByText('Calibrer'))

    expect(await screen.findByText(/calibration indisponible/)).toBeTruthy()
  })
})
