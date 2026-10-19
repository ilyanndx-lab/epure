// Sonde CSP par CDP : ouvre une page, provoque deux violations, relève les
// rapports envoyés par le navigateur (équivalent de l'onglet Network).
//
//   node sonde_csp.mjs <url de la page> <port CDP>
//
// Violation 1 : <img src="https://exemple.invalid/…"> → img-src (BLOQUANT).
// Violation 2 : fetch vers 203.0.113.7 (TEST-NET, non routable) → connect-src
//               (Report-Only : non bloquée, la requête échoue d'elle-même).
const [, , urlPage, portCdp] = process.argv

const attendre = ms => new Promise(r => setTimeout(r, ms))

async function cible() {
  for (let i = 0; i < 50; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${portCdp}/json/list`)
      const pages = (await r.json()).filter(t => t.type === 'page')
      if (pages.length) return pages[0]
    } catch { /* navigateur pas encore prêt */ }
    await attendre(200)
  }
  throw new Error('CDP injoignable')
}

const page = await cible()
const ws = new WebSocket(page.webSocketDebuggerUrl)
await new Promise((ok, ko) => { ws.onopen = ok; ws.onerror = ko })

let id = 0
const enAttente = new Map()
const requetes = new Map()
const evenements = []
ws.onmessage = ev => {
  const m = JSON.parse(ev.data)
  if (m.id && enAttente.has(m.id)) { enAttente.get(m.id)(m); enAttente.delete(m.id); return }
  evenements.push(m)
  if (m.method === 'Network.requestWillBeSent' && m.params.request.url.includes('/csp/report')) {
    requetes.set(m.params.requestId, {
      url: m.params.request.url,
      methode: m.params.request.method,
      type: m.params.type,
      contentType: Object.entries(m.params.request.headers)
        .find(([k]) => k.toLowerCase() === 'content-type')?.[1] ?? '(absent)',
      corps: (m.params.request.postData ?? '').slice(0, 400),
    })
  }
  if (m.method === 'Network.responseReceived' && requetes.has(m.params.requestId)) {
    requetes.get(m.params.requestId).statut = m.params.response.status
  }
  if (m.method === 'Network.loadingFailed' && requetes.has(m.params.requestId)) {
    requetes.get(m.params.requestId).echec = m.params.errorText
  }
}
const cdp = (method, params = {}) => new Promise(ok => {
  const n = ++id
  enAttente.set(n, ok)
  ws.send(JSON.stringify({ id: n, method, params }))
})

await cdp('Network.enable')
await cdp('Page.enable')
await cdp('Page.navigate', { url: urlPage })
await attendre(4000)

const entetes = evenements.find(e => e.method === 'Network.responseReceived' && e.params.response.url.startsWith(urlPage))
const h = entetes ? entetes.params.response.headers : {}
const lire = nom => Object.entries(h).find(([k]) => k.toLowerCase() === nom)?.[1] ?? '(absent)'
console.log('EN-TÊTE Content-Security-Policy             :', lire('content-security-policy'))
console.log('EN-TÊTE Content-Security-Policy-Report-Only :', lire('content-security-policy-report-only'))

const r = await cdp('Runtime.evaluate', {
  awaitPromise: true,
  expression: `(async () => {
    const img = new Image();
    const etatImg = new Promise(ok => { img.onload = () => ok('chargée'); img.onerror = () => ok('non chargée') });
    img.src = 'https://exemple.invalid/sonde-img.png?q=secret';
    document.body.appendChild(img);
    let etatFetch;
    try { await fetch('http://203.0.113.7/sonde-connect', { mode: 'no-cors', signal: AbortSignal.timeout(1500) }); etatFetch = 'partie' }
    catch (e) { etatFetch = 'partie puis échec réseau (' + e.name + ')' }
    return JSON.stringify({ img: await etatImg, fetch: etatFetch });
  })()`,
})
console.log('PAGE :', r.result?.result?.value ?? JSON.stringify(r.result))
await attendre(3000)

console.log(`RAPPORTS ENVOYÉS : ${requetes.size}`)
for (const q of requetes.values()) console.log(JSON.stringify(q, null, 1))
ws.close()
process.exit(0)
