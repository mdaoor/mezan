// Workbench keyboard-only flow (spec 9.3) against a running Mizan: MIZAN_URL=http://127.0.0.1:8080 node web/e2e/keyboard.mjs
import { createRequire } from 'node:module'
const require = createRequire(import.meta.url)
const { chromium } = require('playwright')
const BASE = process.env.MIZAN_URL || 'http://127.0.0.1:8080'
const assert = (c, m) => { if (!c) { console.error('FAIL: ' + m); process.exitCode = 1; throw new Error(m) } }

function wav(utts = 4, sr = 16000) {
  const parts = []
  const gap = new Float32Array(Math.floor(1.3 * sr)).map(() => (Math.random() - 0.5) * 0.004)
  for (let u = 0; u < utts; u++) {
    parts.push(gap)
    const n = 4 * sr, s = new Float32Array(n)
    for (let i = 0; i < n; i++) { const t = i / sr; s[i] = (Math.sin(2 * Math.PI * 180 * t) + 0.5 * Math.sin(2 * Math.PI * 720 * t)) * (0.5 + 0.5 * Math.sin(2 * Math.PI * 3 * t)) * 0.3 }
    parts.push(s)
  }
  parts.push(gap)
  const total = parts.reduce((a, p) => a + p.length, 0)
  const buf = Buffer.alloc(44 + total * 2)
  buf.write('RIFF', 0); buf.writeUInt32LE(36 + total * 2, 4); buf.write('WAVEfmt ', 8); buf.writeUInt32LE(16, 16); buf.writeUInt16LE(1, 20); buf.writeUInt16LE(1, 22)
  buf.writeUInt32LE(sr, 24); buf.writeUInt32LE(sr * 2, 28); buf.writeUInt16LE(2, 32); buf.writeUInt16LE(16, 34); buf.write('data', 36); buf.writeUInt32LE(total * 2, 40)
  let o = 44
  for (const p of parts) for (const v of p) { buf.writeInt16LE(Math.max(-32767, Math.min(32767, Math.round(v * 32767))), o); o += 2 }
  return buf
}

const call = async (tok, method, path, body, form) => {
  const r = await fetch(`${BASE}/api/v1${path}`, { method, headers: { ...(tok ? { Authorization: `Bearer ${tok}` } : {}), ...(body ? { 'Content-Type': 'application/json' } : {}) }, body: form ?? (body ? JSON.stringify(body) : undefined) })
  const j = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error(`${method} ${path} ${r.status} ${JSON.stringify(j)}`)
  return j
}
const until = async (fn, ms = 30000) => { const t = Date.now(); while (Date.now() - t < ms) { const v = await fn(); if (v) return v; await new Promise(r => setTimeout(r, 300)) } throw new Error('timeout') }

const admin = (await call(null, 'POST', '/auth/login', { email: process.env.MIZAN_ADMIN_EMAIL || 'admin@mizan.local', password: process.env.MIZAN_ADMIN_PASSWORD || 'admin' })).access_token
const p = await call(admin, 'POST', '/projects', { name: 'E2E' })
const d = await call(admin, 'POST', `/projects/${p.id}/datasets`, { name: 'kbd' })
const form = new FormData(); form.append('files', new Blob([wav(4)], { type: 'audio/wav' }), 'kbd.wav')
const up = await call(admin, 'POST', `/datasets/${d.id}/audio?segmenter=energy_vad`, null, form)
const segs = await until(async () => { const s = (await call(admin, 'GET', `/datasets/${d.id}/segments`)).items; return s.length >= 4 ? s : null })
const email = `kb${Date.now()}@example.com`
await call(admin, 'POST', '/users', { email, name: 'Kb', role: 'annotator', password: 'secret123' })
const ep = await call(admin, 'POST', '/model-endpoints', { engine_kind: 'mock', model_id: 'm', display_name: 'mock', config: { default_text: 'مسودة draft', latency_ms: 1 } })
await call(admin, 'POST', `/datasets/${d.id}/bootstrap`, { model_endpoint_id: ep.id })
await until(async () => (await call(admin, 'GET', `/datasets/${d.id}/segments?status=draft`)).items.length >= 4)

const browser = await chromium.launch({ args: ['--autoplay-policy=no-user-gesture-required'] })
const page = await (await browser.newContext()).newPage()
page.on('pageerror', e => { console.error('PAGE ERROR', e.message) })
await page.goto(`${BASE}/`)
await page.getByTestId('email').fill(email)
await page.getByTestId('password').fill('secret123')
await page.getByTestId('login').click()
await page.goto(`${BASE}/#/datasets/${d.id}/annotate`)
await page.getByTestId('claim').click()
const ta = page.getByTestId('transcript')
await ta.waitFor({ timeout: 15000 })
assert(await ta.evaluate(el => document.activeElement === el), 'transcript is focused on open (keyboard-only start)')
assert((await page.getByTestId('draft-note').textContent()).includes('Draft by'), 'machine draft is marked')
assert((await ta.inputValue()) === 'مسودة draft', 'draft is pre-filled')

await page.keyboard.press('Control+/')
await page.getByTestId('shortcuts').waitFor(); await page.keyboard.press('Control+/')
await page.getByTestId('shortcuts').waitFor({ state: 'detached' })
await page.keyboard.press('Tab')
assert(await ta.evaluate(el => document.activeElement === el), 'Tab is play/pause and must not leave the editor')
await page.keyboard.press('Shift+Tab')
await page.keyboard.press('Control+ArrowUp')
assert((await page.getByTestId('speed').textContent()).includes('1.25'), 'Ctrl+Up raises speed')
await page.keyboard.press('Control+ArrowDown')

// N3: interaction latency < 100 ms (flag toggle, measured in-page to exclude automation overhead)
const dt = await page.evaluate(async () => {
  const t0 = performance.now()
  window.dispatchEvent(new KeyboardEvent('keydown', { key: '3', ctrlKey: true, bubbles: true }))
  await new Promise(r => requestAnimationFrame(() => r(null)))
  return { ms: performance.now() - t0, on: document.querySelector('[data-testid="flag-overlap"]').getAttribute('aria-pressed') }
})
assert(dt.on === 'true' && dt.ms < 100, `Ctrl+3 toggles overlap in <100 ms (took ${dt.ms.toFixed(1)} ms)`)
await page.keyboard.press('Control+3')
await page.keyboard.press('Control+1')
assert((await page.getByTestId('flag-no_speech').getAttribute('aria-pressed')) === 'true', 'Ctrl+1 toggles no_speech')
await page.keyboard.press('Control+1')

await page.keyboard.press('Control+A')
await page.keyboard.type('مرحبا بكم meeting اليوم')
await until(async () => (await page.getByTestId('save-state').textContent()) === 'saved', 6000)   // autosave after 2 s idle
const first = segs[0].id
let w = await call(admin, 'GET', `/segments/${first}/annotation`)
assert(w.annotation.text === 'مرحبا بكم meeting اليوم' && w.annotation.source === 'human', 'autosave stored the edit as a human version')

if (process.env.SHOT) await page.screenshot({ path: process.env.SHOT })
await page.keyboard.press('Control+Enter')                                                        // submit and advance
await until(async () => (await page.getByTestId('batch-progress').textContent()).startsWith('1/'), 8000)
w = await call(admin, 'GET', `/segments/${first}/annotation`)
assert(w.annotation.status === 'submitted', 'Ctrl+Enter submits')
assert((await page.getByTestId('seg-1').count()) === 1, 'batch list shows next segment')
await page.keyboard.press('Control+j')                                                            // previous
await page.keyboard.press('Control+k')                                                            // next
console.log('OK keyboard flow')
await browser.close()
