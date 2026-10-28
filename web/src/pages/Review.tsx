import { useCallback, useEffect, useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { audioUrl, get, Json, pct, post } from '../api'
import { RichText } from '../components/text'
import { Empty, useToast } from '../components/ui'

export default function Review() {
  const { datasetId } = useParams()
  const qc = useQueryClient()
  const toast = useToast()
  const [sel, setSel] = useState(0)
  const [note, setNote] = useState('')
  const [edit, setEdit] = useState<string | null>(null)
  const [requesting, setRequesting] = useState(false)
  const [listened, setListened] = useState<Set<string>>(new Set())
  const [batch, setBatch] = useState<Set<string>>(new Set())
  const qs = datasetId ? `?dataset=${datasetId}` : ''
  const q = useQuery({ queryKey: ['review', datasetId], queryFn: () => get(`/review/queue${qs}`) })
  const items: Json[] = useMemo(() => q.data?.items ?? [], [q.data])
  const cur = items[Math.min(sel, items.length - 1)]
  const stats = useQuery({ queryKey: ['author', cur?.annotation.author_id], queryFn: () => get(`/review/author-stats/${cur.annotation.author_id}`), enabled: !!cur?.annotation.author_id })

  const act = useCallback(async (action: 'approve' | 'request_changes', text?: string) => {
    if (!cur) return
    if (action === 'request_changes' && !note.trim()) { setRequesting(true); toast.push({ text: 'A comment is required to request changes', tone: 'err' }); return }
    try {
      await post(`/segments/${cur.segment.id}/annotation/review`, { action, note: note || undefined, text })
      toast.push({ text: action === 'approve' ? 'Approved' : 'Changes requested' }); setNote(''); setEdit(null); setRequesting(false)
      await qc.invalidateQueries({ queryKey: ['review'] }); setSel(0)
    } catch (e) { toast.push({ text: (e as Error).message, tone: 'err' }) }
  }, [cur, note])
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement
      if (t.tagName === 'TEXTAREA' || t.tagName === 'INPUT') return
      if (e.key.toLowerCase() === 'a') act('approve'); else if (e.key.toLowerCase() === 'r') { e.preventDefault(); if (note.trim()) act('request_changes'); else setRequesting(true) }
      else if (e.key === 'j') setSel(s => Math.min(items.length - 1, s + 1)); else if (e.key === 'k') setSel(s => Math.max(0, s - 1))
    }
    window.addEventListener('keydown', h); return () => window.removeEventListener('keydown', h)
  })
  const batchApprove = async () => {
    const ids = [...batch]
    const plan = await post('/review/batch-approve/plan', { segment_ids: ids })
    const missing = plan.spot_check_ids.filter((i: string) => !listened.has(i))
    if (missing.length) { toast.push({ text: `Spot-check required: listen to ${missing.length} sampled segment(s) (marked ★) first`, tone: 'err' }); return }
    const r = await post('/review/batch-approve', { segment_ids: ids, listened_ids: [...listened] })
    toast.push({ text: `Approved ${r.approved.length}${r.failed.length ? `, ${r.failed.length} failed` : ''}` }); setBatch(new Set()); qc.invalidateQueries({ queryKey: ['review'] })
  }
  const [plan, setPlan] = useState<string[]>([])
  useEffect(() => { if (batch.size) post('/review/batch-approve/plan', { segment_ids: [...batch] }).then(p => setPlan(p.spot_check_ids)); else setPlan([]) }, [batch])
  const play = (s: Json) => { setListened(l => new Set(l).add(s.segment.id)); window.dispatchEvent(new CustomEvent('mizan:play', { detail: audioUrl(s.segment.audio_file_id, s.segment.start_s, s.segment.end_s) })) }

  if (q.isLoading) return null
  if (!items.length) return <Empty title="Review queue is empty">Nothing submitted by others is waiting. 🎉</Empty>
  return (
    <div className="grid gap-3" style={{ gridTemplateColumns: '38% 62%' }}>
      <aside className="panel p-2 overflow-auto max-h-[80vh]">
        <div className="flex justify-between items-center p-1 text-sm"><b>{q.data.total} waiting</b><button className="btn" disabled={!batch.size} onClick={batchApprove}>Batch approve ({batch.size})</button></div>
        {items.map((s, i) => (
          <div key={s.segment.id} className="flex gap-2 items-start p-2 rounded" style={{ background: i === sel ? 'color-mix(in srgb, var(--accent) 12%, transparent)' : undefined }}>
            <input type="checkbox" aria-label="Select for batch approve" checked={batch.has(s.segment.id)} onChange={e => setBatch(b => { const n = new Set(b); e.target.checked ? n.add(s.segment.id) : n.delete(s.segment.id); return n })} />
            <button className="text-start flex-1" onClick={() => setSel(i)}><div className="bidi truncate text-sm" dir="auto">{plan.includes(s.segment.id) && <span title="must listen" style={{ color: listened.has(s.segment.id) ? 'var(--accent)' : 'var(--warn)' }}>★ </span>}{s.annotation.text || <i className="mut">(empty)</i>}</div>
              <div className="text-xs mut">{s.segment.flags.join(', ')} {s.open_comments && '💬'}</div></button>
          </div>))}
      </aside>
      {cur && <section className="panel p-4 space-y-3" data-testid="review-card">
        <div className="flex gap-2 items-center"><button className="btn" onClick={() => play(cur)} autoFocus>▶ Listen</button>
          <span className="mut text-sm" title="Quality signals are visible to reviewers only">annotator approval rate: {stats.data?.approval_rate == null ? 'n/a' : pct(stats.data.approval_rate, 0)} ({stats.data?.reviewed ?? 0} reviewed)</span></div>
        <div><div className="text-xs mut">Submitted transcript {cur.machine_draft != null && '(underlined = changed from machine draft)'}</div>
          {edit === null ? <div className="ar-editor panel"><RichText text={cur.annotation.text} base={cur.machine_draft} /></div>
            : <textarea className="ar-editor panel w-full" dir="auto" value={edit} onChange={e => setEdit(e.target.value)} aria-label="Edit transcript" />}</div>
        {cur.machine_draft != null && edit === null && <details><summary className="text-xs mut cursor-pointer">Machine draft</summary><div className="ar-editor"><RichText text={cur.machine_draft} /></div></details>}
        {cur.annotation.note && <div className="text-sm">Annotator note: {cur.annotation.note}</div>}
        {requesting && <textarea className="input w-full" placeholder="What needs to change? (required)" value={note} onChange={e => setNote(e.target.value)} autoFocus aria-label="Review comment" />}
        <div className="flex gap-2">
          <button className="btn btn-primary" onClick={() => act('approve', edit ?? undefined)} data-testid="approve">{edit !== null ? 'Edit & approve' : 'Approve (A)'}</button>
          <button className="btn btn-danger" onClick={() => (requesting && note.trim() ? act('request_changes') : setRequesting(true))} data-testid="request-changes">Request changes (R)</button>
          {edit === null ? <button className="btn" onClick={() => setEdit(cur.annotation.text)}>Edit</button> : <button className="btn" onClick={() => setEdit(null)}>Cancel edit</button>}
        </div>
        <div className="text-xs mut">Keys: A approve · R request changes · J/K next/previous</div>
      </section>}
    </div>
  )
}
