import { useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { audioUrl, api, fmt, get, Json, mmss, patch, post, pct, useTopics } from '../api'
import { useStore } from '../store'
import { Empty, ErrorBox, Progress, Ring, SchemaForm, StatusBadge, Tabs, useToast } from '../components/ui'

export function DatasetDetail() {
  const { datasetId = '' } = useParams()
  const qc = useQueryClient()
  const toast = useToast()
  const role = useStore(s => s.user?.role)
  const manage = role === 'admin' || role === 'reviewer'
  const [tab, setTab] = useState('Overview')
  const [job, setJob] = useState<string | null>(null)
  const [segKind, setSegKind] = useState('energy_vad')
  const [segCfg, setSegCfg] = useState<Json>({})
  const [bootEp, setBootEp] = useState('')
  const [goldName, setGoldName] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const file = useRef<HTMLInputElement>(null)
  const ds = useQuery({ queryKey: ['dataset', datasetId], queryFn: () => get(`/datasets/${datasetId}`), refetchInterval: job ? 2000 : false })
  const audio = useQuery({ queryKey: ['audio', datasetId], queryFn: () => get(`/datasets/${datasetId}/audio`), refetchInterval: job ? 2000 : false })
  const segs = useQuery({ queryKey: ['segments', datasetId, statusFilter], queryFn: () => get(`/datasets/${datasetId}/segments?limit=200${statusFilter ? `&status=${statusFilter}` : ''}`), enabled: tab === 'Segments' })
  const strat = useQuery({ queryKey: ['strategies'], queryFn: () => get('/strategies') })
  const eps = useQuery({ queryKey: ['endpoints'], queryFn: () => get('/model-endpoints') })
  const jobq = useQuery({ queryKey: ['job', job], queryFn: () => get(`/jobs/${job}`), enabled: !!job, refetchInterval: 1500 })
  if (jobq.data && ['succeeded', 'failed'].includes(jobq.data.status) && job) { setTimeout(() => { setJob(null); qc.invalidateQueries() }, 0); if (jobq.data.status === 'failed') toast.push({ text: `Job failed: ${jobq.data.error?.slice(0, 120)}`, tone: 'err' }) }

  const upload = async (files: FileList | null) => {
    if (!files?.length) return
    const form = new FormData(); Array.from(files).forEach(f => form.append('files', f))
    try { const r = await api('POST', `/datasets/${datasetId}/audio`, undefined, { form }); if (r.job_id) setJob(r.job_id); toast.push({ text: `${r.files.length} file(s) uploaded` }) } catch (e) { toast.push({ text: (e as Error).message, tone: 'err' }) }
  }
  const run = async (fn: () => Promise<Json>, ok: string) => { try { const r = await fn(); if (r.job_id) setJob(r.job_id); toast.push({ text: ok }); qc.invalidateQueries() } catch (e) { toast.push({ text: (e as Error).message, tone: 'err' }) } }
  const d = ds.data
  const st = d?.stats
  const segSchema = strat.data?.segmenters.find((s: Json) => s.kind === segKind)?.config_schema

  if (ds.error) return <ErrorBox error={ds.error} />
  if (!d) return null
  const steps = [{ label: 'Upload', done: st.files > 0 }, { label: 'Segment', done: st.segments > 0 }, { label: 'Bootstrap drafts', done: (st.by_status?.draft?.segments ?? 0) > 0 || (st.by_status?.approved?.segments ?? 0) > 0 }, { label: 'Claim & annotate', done: (st.by_status?.submitted?.segments ?? 0) + (st.by_status?.approved?.segments ?? 0) > 0 }, { label: 'Freeze gold', done: st.gold_sets > 0 }]
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-4"><Ring value={st.progress} size={64} /><div><h1 className="text-xl font-semibold">{d.name}</h1>
        <div className="mut text-sm">{st.files} files · {st.audio_minutes.toFixed(1)} min · {st.segments} segments · {st.gold_sets} gold sets</div></div>
        <div className="ms-auto flex gap-2"><Link className="btn btn-primary" to={`/datasets/${datasetId}/annotate`}>Annotate</Link>{manage && <Link className="btn" to={`/datasets/${datasetId}/review`}>Review</Link>}</div></div>
      {job && <div className="panel p-3 text-sm">Working… {jobq.data ? `${jobq.data.status} ${jobq.data.progress.done}/${jobq.data.progress.total}` : ''}<Progress value={jobq.data?.progress.total ? jobq.data.progress.done / jobq.data.progress.total : 0.05} /></div>}
      <Tabs tabs={['Overview', 'Files', 'Segments', 'Gold sets']} value={tab} onChange={setTab} />
      {tab === 'Overview' && <div className="grid md:grid-cols-2 gap-3">
        <Empty title="Pipeline" steps={steps} />
        {manage && <div className="panel p-4 space-y-3">
          <div><b>1 · Upload</b><div className="mt-1"><input ref={file} type="file" multiple accept=".wav,.mp3,.m4a,.mp4,.ogg,.flac" hidden onChange={e => upload(e.target.files)} data-testid="file-input" />
            <button className="btn" onClick={() => file.current?.click()}>Choose audio files…</button> <span className="mut text-xs">wav, mp3, m4a, mp4, ogg, flac → 16 kHz mono</span></div></div>
          <div><b>2 · Segment</b>
            <div className="flex gap-2 mt-1"><select className="input" value={segKind} onChange={e => { setSegKind(e.target.value); setSegCfg({}) }}>{strat.data?.segmenters.map((s: Json) => <option key={s.kind}>{s.kind}</option>)}</select>
              <button className="btn" onClick={() => run(() => post(`/datasets/${datasetId}/segment`, { segmenter: { kind: segKind, config: segCfg } }), 'Segmenting…')}>Segment</button></div>
            {segSchema && <details className="mt-1"><summary className="text-xs mut cursor-pointer">Segmenter settings</summary><SchemaForm schema={segSchema} value={segCfg} onChange={setSegCfg} /></details>}</div>
          <div><b>3 · Bootstrap drafts</b><div className="flex gap-2 mt-1"><select className="input" value={bootEp} onChange={e => setBootEp(e.target.value)} aria-label="Bootstrap endpoint"><option value="">choose endpoint…</option>{eps.data?.items.map((e: Json) => <option key={e.id} value={e.id}>{e.display_name}</option>)}</select>
            <button className="btn" disabled={!bootEp} onClick={() => run(() => post(`/datasets/${datasetId}/bootstrap`, { model_endpoint_id: bootEp }), 'Bootstrapping…')}>Bootstrap</button></div></div>
          <div><b>5 · Freeze gold</b><div className="flex gap-2 mt-1"><input className="input" placeholder="Gold set name" value={goldName} onChange={e => setGoldName(e.target.value)} />
            <button className="btn" disabled={!goldName} onClick={async () => { if (confirm('Freezing creates an immutable gold set from all approved segments. Continue?')) run(() => post(`/datasets/${datasetId}/gold-sets`, { name: goldName }), 'Gold set frozen') }}>Freeze</button></div></div>
          <div className="text-xs mut">Transfer: <a href={`/api/v1/datasets/${datasetId}/export?token=${useStore.getState().token}`}>export zip</a></div>
        </div>}
        <div className="panel p-4 md:col-span-2"><b>Status</b><table className="t"><thead><tr><th>Annotation status</th><th>Segments</th><th>Minutes</th></tr></thead><tbody>
          {Object.entries(st.by_status).map(([k, v]: [string, Json]) => <tr key={k}><td><StatusBadge status={k} /></td><td>{v.segments}</td><td>{fmt(v.minutes, 1)}</td></tr>)}</tbody></table></div>
      </div>}
      {tab === 'Files' && <table className="t panel"><thead><tr><th>File</th><th>Status</th><th>Duration</th><th>Segments</th><th /></tr></thead><tbody>
        {audio.data?.items.map((a: Json) => <tr key={a.id}><td>{a.name}</td><td><StatusBadge status={a.status} /> {a.error && <span className="text-xs" style={{ color: 'var(--bad)' }}>{a.error.slice(0, 80)}</span>}</td><td>{a.duration_s ? mmss(a.duration_s) : '—'}</td><td>{a.segments}</td>
          <td>{a.status === 'ready' && <button className="btn" onClick={() => window.dispatchEvent(new CustomEvent('mizan:play', { detail: audioUrl(a.id) }))}>▶</button>}</td></tr>)}</tbody></table>}
      {tab === 'Segments' && <div className="space-y-2">
        <select className="input" value={statusFilter} onChange={e => setStatusFilter(e.target.value)} aria-label="Status filter"><option value="">all</option>{['unannotated', 'draft', 'submitted', 'in_review', 'changes_requested', 'approved'].map(s => <option key={s}>{s}</option>)}</select>
        <table className="t panel"><thead><tr><th>#</th><th>Time</th><th>Text</th><th>Status</th><th>Strata / flags</th><th /></tr></thead><tbody>
          {segs.data?.items.map((s: Json) => <tr key={s.id}><td>{s.index}</td><td>{fmt(s.start_s, 1)}–{fmt(s.end_s, 1)}</td><td className="bidi max-w-md truncate" dir="auto">{s.annotation?.text}</td>
            <td>{s.annotation ? <StatusBadge status={s.annotation.status} /> : <span className="mut">—</span>}</td>
            <td className="text-xs">{Object.entries(s.strata).filter(([k]) => k !== 'priority').map(([k, v]) => <span key={k} className="chip me-1">{k}:{String(v)}</span>)}{s.flags.map((f: string) => <span key={f} className="chip me-1">{f}</span>)}</td>
            <td><button className="btn" onClick={() => window.dispatchEvent(new CustomEvent('mizan:play', { detail: audioUrl(s.audio_file_id, s.start_s, s.end_s) }))}>▶</button></td></tr>)}</tbody></table></div>}
      {tab === 'Gold sets' && <div className="space-y-2">{d.gold_sets.map((g: Json) => <GoldCard key={g.id} id={g.id} />)}{!d.gold_sets.length && <Empty title="No gold sets yet — approve segments, then freeze." />}</div>}
    </div>
  )
}

function GoldCard({ id }: { id: string }) {
  const g = useQuery({ queryKey: ['gold', id], queryFn: () => get(`/gold-sets/${id}`) })
  if (!g.data) return null
  const x = g.data
  return <div className="panel p-3"><b>{x.name} · v{x.version}</b> <span className="mut text-sm">{x.segment_count} segments · {x.minutes} min · frozen {new Date(x.frozen_at).toLocaleString()}</span>
    <div className="mt-2 flex gap-4 flex-wrap text-sm">{Object.entries(x.strata_distribution).filter(([k]) => ['lang_mix', 'duration_bucket', 'dialect', 'domain'].includes(k)).map(([k, v]: [string, Json]) => <div key={k}><div className="mut text-xs">{k}</div>{Object.entries(v).map(([a, b]) => <span key={a} className="chip me-1">{a}: {String(b)}</span>)}</div>)}</div>
    <div className="text-xs mut mt-1">policy: {JSON.stringify(x.policy)}</div></div>
}
