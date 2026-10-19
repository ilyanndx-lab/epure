// Banc CDP (même mécanique que sonde_csp.mjs, #72) : ouvre l'interface
// construite servie par le backend et relève les violations connect-src.
//
//   node sonde_connect.mjs <url de la page> <port CDP>
//
// Deux relevés indépendants : l'événement `securitypolicyviolation` dans la
// page (posé avant tout script) et les POST vers /csp/report vus au réseau.
const [, , urlPage, portCdp] = process.argv
const attendre = ms => new Promise(r => setTimeout(r, ms))

async function cible() {
  for (let i = 0; i < 50; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${portCdp}/json/list`)
      const pages = (await r.json()).filter(t => t.type === 'page')
      if (pages.length) return pages[0]
    } catch { /* pas prêt */ }
    await attendre(200)
  }
  throw new Error('CDP injoignable')
}

const page = await cible()
const ws = new WebSocket(page.webSocketDebuggerUrl)
await new Promise((ok, ko) => { ws.onopen = ok; ws.onerror = ko })
let id = 0
const enAttente = new Map()
const rapports = new Map()
const origines = new Map()
ws.onmessage = ev => {
  const m = JSON.parse(ev.data)
  if (m.id && enAttente.has(m.id)) { enAttente.get(m.id)(m); enAttente.delete(m.id); return }
  if (m.method === 'Network.requestWillBeSent') {
    const u = m.params.request.url
    if (u.includes('/csp/report')) {
      rapports.set(m.params.requestId, { corps: m.params.request.postData ?? '' })
    } else if (/^(https?|wss?):/.test(u)) {
      const o = new URL(u).origin
      origines.set(o, (origines.get(o) ?? 0) + 1)
    }
  }
  if (m.method === 'Network.responseReceived' && rapports.has(m.params.requestId)) {
    rapports.get(m.params.requestId).statut = m.params.response.status
  }
}
const cdp = (method, params = {}) => new Promise(ok => {
  const n = ++id
  enAttente.set(n, ok)
  ws.send(JSON.stringify({ id: n, method, params }))
})

await cdp('Network.enable')
await cdp('Page.enable')
await cdp('Network.clearBrowserCache')
await cdp('Page.addScriptToEvaluateOnNewDocument', { source: `
  window.__violations = [];
  document.addEventListener('securitypolicyviolation', e => window.__violations.push(
    { directive: e.effectiveDirective, bloque: e.blockedURI, disposition: e.disposition }));
` })
await cdp('Page.navigate', { url: urlPage })
await attendre(9000)

const r = await cdp('Runtime.evaluate', { expression: 'JSON.stringify(window.__violations)' })
const violations = JSON.parse(r.result?.result?.value ?? '[]')
const connect = violations.filter(v => v.directive === 'connect-src')
console.log(`PAGE            : ${urlPage}`)
console.log(`ORIGINES APPELÉES : ${JSON.stringify(Object.fromEntries(origines))}`)
console.log(`VIOLATIONS (page) : ${violations.length} dont connect-src : ${connect.length}`)
for (const v of connect.slice(0, 5)) console.log('  ', JSON.stringify(v))
const rapportsConnect = [...rapports.values()].filter(q => q.corps.includes('connect-src'))
console.log(`RAPPORTS /csp/report : ${rapports.size} dont connect-src : ${rapportsConnect.length}`)
console.log(`VERDICT connect-src : ${connect.length === 0 && rapportsConnect.length === 0 ? 'AUCUNE' : 'VIOLATIONS'}`)
ws.close()
process.exit(0)
