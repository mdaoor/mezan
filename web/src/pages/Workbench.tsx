import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import WaveSurfer from 'wavesurfer.js'
import RegionsPlugin from 'wavesurfer.js/dist/plugins/regions.esm.js'
import { audioUrl, del, fmt, get, Json, mmss, post, put, patch, useTopics } from '../api'
import { useStore } from '../store'
import { RichText } from '../components/text'
import { Progress, useToast } from '../components/ui'

const FLAGS = ['no_speech', 'music', 'overlap', 'unintelligible'] as const
const SPEEDS = [0.75, 1, 1.25, 1.5]
const CONTEXT_S = 2
const SHORTCUTS: [string, string][] = [
  ['Tab / Shift+Tab', 'play-pause / restart segment'], ['Ctrl+← / →', 'seek −/+ 2 s'], ['Ctrl+↑ / ↓', 'speed'],
  ['Ctrl+Enter', 'submit and advance'], ['Ctrl+S', 'save draft (autosaves every 2 s)'], ['Ctrl+J / Ctrl+K', 'previous / next segment'],
  ['Ctrl+1..4', 'toggle flags (no speech, music, overlap, unintelligible)'], ['Ctrl+L', 'mark selection English ↔ Arabic'], ['Ctrl+/', 'this overlay'],
]

type Seg = Json

export default function Workbench() {
  const { datasetId = '' } = useParams()
  const qc = useQueryClient()
  const toast = useToast()
  const user = useStore(s => s.user)
  const nav = useNavigate()
  const claims = useQuery({ queryKey: ['claims', datasetId], queryFn: () => get(`/datasets/${datasetId}/claims/mine`), refetchInterval: 30000 })
  const [idx, setIdx] = useState(0)
  const [minutes, setMinutes] = useState(10)
  // submitted segments leave the server-side claim list; keep them here so batch progress stays meaningful
  const [done, setDone] = useState<Record<string, Seg>>({})
  const segs: Seg[] = useMemo(() => {
    const m = new Map<string, Seg>((claims.data?.segments ?? []).map((x: Seg) => [x.id, x]))
    Object.values(done).forEach(d => m.set(d.id, { ...(m.get(d.id) ?? d), status: 'submitted', has_text: true }))
    return [...m.values()].sort((a, b) => (a.audio_file_id === b.audio_file_id ? a.index - b.index : a.audio_file_id < b.audio_file_id ? -1 : 1))
  }, [claims.data, done])
  const cur: Seg | undefined = segs[Math.min(idx, Math.max(0, segs.length - 1))]
  const [presenceOthers, setPresence] = useState<Json[]>([])
  const [now, setNow] = useState(Date.now())
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), 15000); return () => clearInterval(t) }, [])

  useTopics([`dataset:${datasetId}`], e => { if (e.type === 'presence') setPresence(e.users.filter((u: Json) => u.user_id !== user?.id)) },
    cur ? { dataset: datasetId, segment: cur.id } : undefined)

  const claim = async () => {
    try { const r = await post(`/datasets/${datasetId}/claims`, { minutes }); toast.push({ text: `Claimed ${r.segment_ids.length} segments (${(r.seconds / 60).toFixed(1)} min)` }); await qc.invalidateQueries({ queryKey: ['claims', datasetId] }) }
    catch (e) { toast.push({ text: (e as Error).message, tone: 'err' }) }
  }
  const release = async () => {
    const ids = segs.filter(s => s.status === 'draft' || s.status === 'changes_requested').map(s => s.id)
    await Promise.all(ids.map(id => del(`/claims/${id}`)))
    toast.push({ text: `Released ${ids.length} segments` }); qc.invalidateQueries({ queryKey: ['claims', datasetId] })
  }
  const remainingS = segs.filter(s => s.status === 'draft' || s.status === 'changes_requested').reduce((a, s) => a + s.duration_s, 0)
  const doneCount = segs.filter(s => !['draft', 'changes_requested'].includes(s.status)).length
  const soonest = segs.filter(s => s.claim_expires_at).map(s => new Date(s.claim_expires_at).getTime()).sort()[0]
  const minsLeft = soonest ? (soonest - now) / 60000 : null

  return (
    <div className="grid gap-3 h-[calc(100vh-110px)]" style={{ gridTemplateColumns: '20% 55% 25%' }}>
      <aside className="panel p-3 overflow-auto" aria-label="Batch">
        <div className="font-semibold mb-1">My batch</div>
        <div className="text-xs mut mb-1" data-testid="batch-progress">{doneCount}/{segs.length} · {(remainingS / 60).toFixed(1)} min left</div>
        <Progress value={segs.length ? doneCount / segs.length : 0} />
        {minsLeft !== null && minsLeft < 10 && <div className="mt-2 text-xs p-2 rounded" style={{ background: 'color-mix(in srgb, var(--warn) 20%, transparent)' }} role="alert">Claim expires in {Math.max(0, Math.round(minsLeft))} min</div>}
        <ul className="mt-3 space-y-1">
          {segs.map((s, i) => (
            <li key={s.id}>
              <button data-testid={`seg-${i}`} onClick={() => setIdx(i)} className="w-full text-start px-2 py-1 rounded text-sm flex items-center gap-2" style={{ background: i === idx ? 'color-mix(in srgb, var(--accent) 15%, transparent)' : 'transparent' }}>
                <span aria-hidden style={{ width: 8, height: 8, borderRadius: 8, background: s.status === 'submitted' ? 'var(--accent)' : s.status === 'changes_requested' ? 'var(--bad)' : s.has_text ? 'var(--warn)' : 'var(--line)' }} />
                <span>#{s.index}</span><span className="mut">{mmss(s.duration_s)}</span>
                {s.flags.length > 0 && <span className="chip">{s.flags[0]}</span>}
              </button>
            </li>
          ))}
        </ul>
        <div className="mt-3 flex flex-wrap gap-2 items-center">
          <input className="input w-16" type="number" min={1} value={minutes} onChange={e => setMinutes(+e.target.value)} aria-label="Minutes to claim" />
          <button className="btn btn-primary" onClick={claim} data-testid="claim">Claim {minutes} min</button>
          <button className="btn" onClick={release} disabled={!segs.length}>Release batch</button>
        </div>
        {presenceOthers.length > 0 && <div className="mt-3 text-xs mut">Also here: {presenceOthers.map(u => u.name).join(', ')}</div>}
      </aside>
      {cur ? <Editor key={cur.id} seg={cur} total={segs.length} index={idx} onPrev={() => setIdx(i => Math.max(0, i - 1))} onNext={() => setIdx(i => Math.min(segs.length - 1, i + 1))}
        onSubmitted={() => {
          setDone(d => ({ ...d, [cur.id]: cur })); qc.invalidateQueries({ queryKey: ['claims', datasetId] })
          const nextOpen = segs.findIndex((x, k) => k > idx && !['submitted', 'in_review', 'approved'].includes(x.status) && x.id !== cur.id)
          setIdx(nextOpen >= 0 ? nextOpen : Math.min(segs.length - 1, idx + 1))
        }} datasetId={datasetId} />
        : <section className="panel p-6 col-span-2 text-center">
          <h2 className="font-semibold mb-2">No segments in your batch</h2>
          <p className="mut text-sm mb-3">Claim a few minutes of audio, correct the machine draft, submit. A reviewer approves.</p>
          <button className="btn btn-primary" onClick={claim}>Claim {minutes} minutes</button>
          <div className="mt-3"><button className="btn" onClick={() => nav(`/datasets/${datasetId}`)}>Back to dataset</button></div>
        </section>}
    </div>
  )
}

function Editor({ seg, index, total, onPrev, onNext, onSubmitted, datasetId }: { seg: Seg; index: number; total: number; onPrev: () => void; onNext: () => void; onSubmitted: () => void; datasetId: string }) {
  const toast = useToast()
  const wsDiv = useRef<HTMLDivElement>(null)
  const ws = useRef<WaveSurfer | null>(null)
  const ta = useRef<HTMLTextAreaElement>(null)
  const [data, setData] = useState<Json>(null)
  const [text, setText] = useState('')
  const [flags, setFlags] = useState<string[]>(seg.flags)
  const [dialect, setDialect] = useState<string>(seg.strata?.dialect ?? '')
  const [domain, setDomain] = useState<string>(seg.strata?.domain ?? '')
  const [note, setNote] = useState('')
  const [spans, setSpans] = useState<Json[] | null>(null)
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle')
  const [speed, setSpeed] = useState(1)
  const [help, setHelp] = useState(false)
  const [hist, setHist] = useState(false)
  const [comment, setComment] = useState('')
  const stateRef = useRef({ text, flags, dialect, domain, note, spans })
  stateRef.current = { text, flags, dialect, domain, note, spans }
  const isIaa = seg.track === 'iaa'
  const q = isIaa ? '?track=iaa' : ''

  const load = useCallback(async () => {
    const d = await get(`/segments/${seg.id}/annotation${q}`)
    setData(d); setText(d.annotation?.text ?? ''); setFlags(d.segment.flags); setNote(d.annotation?.note ?? ''); setSpans(null)
    setDialect(d.segment.strata?.dialect ?? ''); setDomain(d.segment.strata?.domain ?? '')
  }, [seg.id, q])
  useEffect(() => { load().then(() => setTimeout(() => ta.current?.focus(), 50)) }, [load])
  useTopics([`segment:${seg.id}`], e => { if (e.type === 'comment') load() })

  // ---- audio: only the segment +-2 s is fetched (no whole-file decode), first play is automatic
  useEffect(() => {
    if (!wsDiv.current) return
    const start = Math.max(0, seg.start_s - CONTEXT_S), end = seg.end_s + CONTEXT_S
    const regions = RegionsPlugin.create()
    const w = WaveSurfer.create({ container: wsDiv.current, height: 90, waveColor: '#8b95a5', progressColor: '#0b6b4f', cursorColor: '#b54708', url: audioUrl(seg.audio_file_id, start, end), plugins: [regions] })
    ws.current = w
    w.on('ready', () => {
      regions.addRegion({ start: seg.start_s - start, end: seg.end_s - start, color: 'rgba(11,107,79,.18)', drag: false, resize: false })
      w.setPlaybackRate(speed, true)
      w.setTime(seg.start_s - start); w.play().catch(() => {})
    })
    w.on('timeupdate', t => { if (loopRef.current && t >= seg.end_s - start) { w.setTime(seg.start_s - start) } else if (!loopRef.current && t >= seg.end_s - start) w.pause() })
    return () => { w.destroy(); ws.current = null }
  }, [seg.id])
  const loopRef = useRef(false)
  const base = Math.max(0, seg.start_s - CONTEXT_S)
  const play = () => { const w = ws.current; if (!w) return; if (w.isPlaying()) w.pause(); else { if (w.getCurrentTime() >= seg.end_s - base - 0.05) w.setTime(seg.start_s - base); w.play() } }
  const restart = () => { const w = ws.current; if (w) { w.setTime(seg.start_s - base); w.play() } }
  const seek = (d: number) => { const w = ws.current; if (w) w.setTime(Math.max(0, w.getCurrentTime() + d)) }
  const setRate = (dir: number) => { const i = Math.max(0, Math.min(SPEEDS.length - 1, SPEEDS.indexOf(speed) + dir)); setSpeed(SPEEDS[i]); ws.current?.setPlaybackRate(SPEEDS[i], true) }

  // ---- saving: optimistic (UI never waits), autosave 2 s after the last edit
  const save = useCallback(async (): Promise<boolean> => {
    const s = stateRef.current
    setSaving('saving')
    try {
      const strata: Json = {}
      if (s.dialect) strata.dialect = s.dialect
      if (s.domain) strata.domain = s.domain
      const a = await put(`/segments/${seg.id}/annotation${q}`, { text: s.text, flags: s.flags, note: s.note, strata, ...(s.spans ? { spans: s.spans } : {}) })
      setData((d: Json) => ({ ...d, annotation: a })); setDirty(false); setSaving('saved'); return true
    } catch (e) { setSaving('error'); toast.push({ text: `Save failed: ${(e as Error).message}`, tone: 'err' }); return false }
  }, [seg.id, q])
  useEffect(() => {
    if (!dirty) return
    const t = setTimeout(save, 2000)
    return () => clearTimeout(t)
  }, [text, flags, dialect, domain, note, spans, dirty, save])
  const edit = (fn: () => void) => { fn(); setDirty(true); setSaving('idle') }

  const submit = useCallback(async () => {
    if (!(await save())) return
    try { await post(`/segments/${seg.id}/annotation/submit${q}`); toast.push({ text: 'Submitted' }); onSubmitted() }
    catch (e) { toast.push({ text: (e as Error).message, tone: 'err' }) }
  }, [save, seg.id, q, onSubmitted])

  const toggleFlag = (f: string) => edit(() => setFlags(x => x.includes(f) ? x.filter(y => y !== f) : [...x, f]))
  const markSelection = () => {
    const el = ta.current; if (!el || el.selectionStart === el.selectionEnd) { toast.push({ text: 'Select some text first' }); return }
    const a = el.selectionStart, b = el.selectionEnd
    const sel = text.slice(a, b), isLat = /[A-Za-z]/.test(sel)
    const base = (spans ?? data?.annotation?.spans ?? []).filter((s: Json) => s.end_char <= a || s.start_char >= b)
    edit(() => setSpans([...base, { start_char: a, end_char: b, lang: isLat ? 'ar' : 'en' }].sort((x: Json, y: Json) => x.start_char - y.start_char)))
    toast.push({ text: `Marked ${isLat ? 'Arabic' : 'English'}` })
  }

  // ---- keyboard-only workflow (mandatory)
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      const ctrl = e.ctrlKey || e.metaKey
      if (e.key === 'Tab') { e.preventDefault(); e.shiftKey ? restart() : play(); return }
      if (!ctrl) return
      const k = e.key
      const act = () => e.preventDefault()
      if (k === 'ArrowLeft') { act(); seek(-2) } else if (k === 'ArrowRight') { act(); seek(2) }
      else if (k === 'ArrowUp') { act(); setRate(1) } else if (k === 'ArrowDown') { act(); setRate(-1) }
      else if (k === 'Enter') { act(); submit() } else if (k === 's' || k === 'S') { act(); save() }
      else if (k === 'j' || k === 'J') { act(); onPrev() } else if (k === 'k' || k === 'K') { act(); onNext() }
      else if (['1', '2', '3', '4'].includes(k)) { act(); toggleFlag(FLAGS[+k - 1]) }
      else if (k === 'l' || k === 'L') { act(); markSelection() }
      else if (k === '/') { act(); setHelp(h => !h) }
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  })

  const draft = data?.machine_draft
  const ann = data?.annotation
  const readOnly = ann && ['submitted', 'in_review', 'approved', 'superseded'].includes(ann.status)
  const shownSpans = spans ?? ann?.spans ?? []

  return (
    <>
      <section className="panel p-3 flex flex-col gap-2 overflow-auto" aria-label="Editor">
        <div ref={wsDiv} data-testid="waveform" />
        <div className="flex flex-wrap gap-2 items-center text-sm">
          <button className="btn" onClick={play} aria-label="Play or pause">⏯ Play</button>
          <button className="btn" onClick={restart}>⟲ Restart</button>
          <button className="btn" onClick={() => seek(-3)}>−3 s</button><button className="btn" onClick={() => seek(3)}>+3 s</button>
          <label className="flex items-center gap-1"><input type="checkbox" onChange={e => { loopRef.current = e.target.checked }} /> loop</label>
          <span className="chip" data-testid="speed">{speed}×</span>
          <span className="mut">#{seg.index} · {fmt(seg.start_s, 2)}–{fmt(seg.end_s, 2)} s · {index + 1}/{total}</span>
          <span className="ms-auto mut" data-testid="save-state">{saving === 'saving' ? 'saving…' : saving === 'saved' ? 'saved' : dirty ? 'unsaved' : ''}</span>
        </div>
        {draft && ann?.source?.startsWith('machine:') && <div className="text-xs mut" data-testid="draft-note">Draft by {draft.source.replace('machine:', 'model ').slice(0, 24)} — edit, don't retype</div>}
        {isIaa && <div className="text-xs p-2 rounded" style={{ background: 'color-mix(in srgb, var(--warn) 15%, transparent)' }}>Blind double annotation: transcribe independently.</div>}
        <div className="relative panel" style={{ minHeight: 160 }}>
          <div className="ar-editor absolute inset-0 pointer-events-none overflow-hidden" aria-hidden><RichText text={text} base={ann?.source?.startsWith('machine:') || draft ? (draft?.text ?? null) : null} /></div>
          <textarea ref={ta} data-testid="transcript" dir="auto" className="ar-editor relative w-full bg-transparent outline-none resize-none" style={{ minHeight: 160, color: 'transparent', caretColor: 'var(--fg)' }}
            value={text} readOnly={!!readOnly} onChange={e => edit(() => { setText(e.target.value); setSpans(null) })} spellCheck={false}
            onKeyDown={e => { if (e.key === 'Tab') e.preventDefault() }} aria-label="Transcript" />
        </div>
        {readOnly && <div className="text-sm mut">This annotation is {ann.status}; it is locked.</div>}
        <div className="flex gap-2">
          <button className="btn btn-primary" onClick={submit} disabled={!!readOnly} data-testid="submit">Submit &amp; next (Ctrl+Enter)</button>
          <button className="btn" onClick={save} disabled={!!readOnly}>Save (Ctrl+S)</button>
          <button className="btn" onClick={() => setHelp(true)}>Shortcuts (Ctrl+/)</button>
        </div>
      </section>
      <aside className="panel p-3 overflow-auto text-sm space-y-4" aria-label="Details">
        <div>
          <div className="font-semibold mb-1">Flags</div>
          <div className="flex flex-wrap gap-2">{FLAGS.map((f, i) => <button key={f} className={`chip ${flags.includes(f) ? 'on' : ''}`} onClick={() => toggleFlag(f)} data-testid={`flag-${f}`} aria-pressed={flags.includes(f)}>{i + 1} · {f.replace('_', ' ')}</button>)}</div>
          {flags.includes('unintelligible') && <input className="input w-full mt-2" placeholder="Note: what can't you hear?" value={note} onChange={e => edit(() => setNote(e.target.value))} />}
        </div>
        <div>
          <div className="font-semibold mb-1">Strata</div>
          <div className="flex gap-2"><input className="input w-1/2" placeholder="dialect" value={dialect} onChange={e => edit(() => setDialect(e.target.value))} />
            <input className="input w-1/2" placeholder="domain" value={domain} onChange={e => edit(() => setDomain(e.target.value))} /></div>
        </div>
        <div>
          <div className="font-semibold mb-1">Language spans</div>
          <ul className="space-y-1">{shownSpans.map((s: Json, i: number) => (
            <li key={i} className="flex items-center gap-2"><span className="chip">{s.lang}</span><span className="bidi" dir="auto">{text.slice(s.start_char, s.end_char)}</span></li>))}
            {!shownSpans.length && <li className="mut">auto-detected from script after you type</li>}</ul>
        </div>
        <div>
          <div className="font-semibold mb-1">Comments</div>
          {(data?.comments ?? []).map((c: Json) => (
            <div key={c.id} className="mb-2 p-2 rounded" style={{ background: 'var(--bg)', opacity: c.resolved ? .55 : 1, marginInlineStart: c.parent_id ? 12 : 0 }}>
              <div className="bidi" dir="auto">{c.body}</div>
              <button className="text-xs mut" onClick={() => patch(`/comments/${c.id}`, { resolved: !c.resolved }).then(load)}>{c.resolved ? 'reopen' : 'resolve'}</button>
            </div>))}
          {ann && !isIaa && <div className="flex gap-1"><input className="input flex-1" value={comment} onChange={e => setComment(e.target.value)} placeholder="Add a comment" />
            <button className="btn" onClick={async () => { if (comment.trim()) { await post(`/annotations/${ann.id}/comments`, { body: comment }); setComment(''); load() } }}>Send</button></div>}
        </div>
        <div>
          <button className="font-semibold" onClick={() => setHist(h => !h)}>History {hist ? '▾' : '▸'}</button>
          {hist && <ul className="mt-1 space-y-1">{(data?.history ?? []).map((h: Json) => <li key={h.id} className="text-xs"><b>v{h.version}</b> {h.status} · {h.source.startsWith('machine') ? 'machine' : h.source}<div className="bidi mut" dir="auto">{h.text.slice(0, 80)}</div></li>)}</ul>}
        </div>
      </aside>
      {help && (
        <div className="fixed inset-0 z-50 flex items-center justify-center" style={{ background: 'rgba(0,0,0,.45)' }} onClick={() => setHelp(false)} data-testid="shortcuts">
          <div className="panel p-5 max-w-md" onClick={e => e.stopPropagation()} role="dialog" aria-label="Keyboard shortcuts">
            <h2 className="font-semibold mb-2">Keyboard shortcuts</h2>
            <table className="t"><tbody>{SHORTCUTS.map(([k, d]) => <tr key={k}><td><kbd>{k}</kbd></td><td>{d}</td></tr>)}</tbody></table>
          </div>
        </div>)}
    </>
  )
}
