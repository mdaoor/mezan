import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { fmt, get, Json, pct, post, useTopics } from '../api'
import { useStore } from '../store'
import { LineSeries } from '../components/charts'
import { ErrorBox, Progress, SchemaForm, StatusBadge, useToast } from '../components/ui'

export function LoadList() {
  const projectId = useStore(s => s.projectId)
  const eps = useQuery({ queryKey: ['endpoints'], queryFn: () => get('/model-endpoints') })
  const name = (id: string) => eps.data?.items.find((e: Json) => e.id === id)?.display_name ?? id.slice(0, 6)
  const l = useQuery({ queryKey: ['loadruns', projectId], queryFn: () => get(`/load-runs?project_id=${projectId}`), enabled: !!projectId, refetchInterval: 4000 })
  return (
    <div className="space-y-3"><div className="flex"><h1 className="text-xl font-semibold">Load runs</h1><Link className="btn btn-primary ms-auto" to="/load/new">New load run</Link></div>
      <table className="t panel"><thead><tr><th>Endpoint</th><th>Status</th><th>Best RTFx</th><th>Saturation</th><th>Started</th></tr></thead><tbody>
        {l.data?.items.map((r: Json) => <tr key={r.id}><td><Link to={`/load/${r.id}`}>{name(r.model_endpoint_id)}</Link></td><td><StatusBadge status={r.status} /></td><td>{r.best_rtfx ? `${fmt(r.best_rtfx, 1)}×` : '—'}</td>
          <td>{r.saturation ? `c=${r.saturation.concurrency} (${r.saturation.reason.replace('_', ' ')})` : r.status === 'succeeded' ? 'not reached' : '—'}</td><td className="mut">{r.started_at ? new Date(r.started_at).toLocaleString() : '—'}</td></tr>)}</tbody></table></div>
  )
}

export function NewLoad() {
  const projectId = useStore(s => s.projectId)
  const nav = useNavigate()
  const toast = useToast()
  const [ep, setEp] = useState(''); const [sc, setSc] = useState(''); const [ds, setDs] = useState(''); const [tele, setTele] = useState('')
  const [custom, setCustom] = useState(false)
  const [form, setForm] = useState<Json>({ name: 'my-scenario', concurrency_steps: [1, 4, 16, 32, 64], step_duration_s: 120, length_buckets: { '5-15s': 0.6, '15-30s': 0.4 }, warmup_s: 30, timeout_s: 120 })
  const eps = useQuery({ queryKey: ['endpoints'], queryFn: () => get('/model-endpoints') })
  const scs = useQuery({ queryKey: ['scenarios'], queryFn: () => get('/load-scenarios') })
  const dss = useQuery({ queryKey: ['datasets', projectId], queryFn: () => get(`/projects/${projectId}/datasets`), enabled: !!projectId })
  const strat = useQuery({ queryKey: ['strategies'], queryFn: () => get('/strategies') })
  const schema = { type: 'object', properties: { name: { type: 'string' }, concurrency_steps: { type: 'array', items: { type: 'integer', minimum: 1 } }, step_duration_s: { type: 'number' }, warmup_s: { type: 'number' }, timeout_s: { type: 'number' } } }
  const go = async () => {
    try {
      let scenario = sc
      if (custom) scenario = (await post('/load-scenarios', form)).id
      const r = await post('/load-runs', { model_endpoint_id: ep, scenario_id: scenario, dataset_id: ds, telemetry_source: tele || null })
      nav(`/load/${r.load_run_id}`)
    } catch (e) { toast.push({ text: (e as Error).message, tone: 'err' }) }
  }
  return (
    <div className="space-y-3 max-w-2xl"><h1 className="text-xl font-semibold">New load run</h1>
      <div className="panel p-4 space-y-3 text-sm">
        <label className="block">Endpoint<select className="input w-full" value={ep} onChange={e => setEp(e.target.value)} aria-label="Endpoint"><option value="">choose…</option>{eps.data?.items.map((e: Json) => <option key={e.id} value={e.id}>{e.display_name}</option>)}</select></label>
        <label className="block">Dataset (audio is sampled from its segments)<select className="input w-full" value={ds} onChange={e => setDs(e.target.value)} aria-label="Dataset"><option value="">choose…</option>{dss.data?.items.map((d: Json) => <option key={d.id} value={d.id}>{d.name}</option>)}</select></label>
        <div><label><input type="checkbox" checked={custom} onChange={e => setCustom(e.target.checked)} /> Custom scenario</label></div>
        {custom ? <><SchemaForm schema={schema} value={form} onChange={setForm} /><label className="block">Length buckets (JSON)<input className="input w-full" value={JSON.stringify(form.length_buckets)} onChange={e => { try { setForm({ ...form, length_buckets: JSON.parse(e.target.value) }) } catch { /* typing */ } }} /></label></>
          : <label className="block">Scenario<select className="input w-full" value={sc} onChange={e => setSc(e.target.value)} aria-label="Scenario"><option value="">choose…</option>{scs.data?.items.map((s: Json) => <option key={s.id} value={s.id}>{s.name} — {s.concurrency_steps.join('→')} × {s.step_duration_s}s</option>)}</select></label>}
        <label className="block">GPU telemetry<select className="input w-full" value={tele} onChange={e => setTele(e.target.value)}><option value="">none</option>{strat.data?.telemetry_sources.filter((t: Json) => t.kind !== 'none').map((t: Json) => <option key={t.kind}>{t.kind}</option>)}</select></label>
        <p className="mut text-xs">Load tests disturb shared GPUs. Concurrency never exceeds the endpoint's max_concurrency; a step aborts above 20 % errors.</p>
        <button className="btn btn-primary" disabled={!ep || !ds || (!custom && !sc)} onClick={go}>Start load run</button></div></div>
  )
}

export function LoadDetail() {
  const { loadId = '' } = useParams()
  const qc = useQueryClient()
  const [liveSteps, setLive] = useState<Json[]>([])
  const l = useQuery({ queryKey: ['load', loadId], queryFn: () => get(`/load-runs/${loadId}`), refetchInterval: q => (['running', 'pending'].includes(q.state.data?.status) ? 2000 : false) })
  const tele = useQuery({ queryKey: ['tele', loadId, l.data?.status], queryFn: () => get(`/load-runs/${loadId}/telemetry`) })
  useTopics([`load-run:${loadId}`], e => { if (e.type === 'step') setLive(x => [...x, e]); if (e.type === 'finished') qc.invalidateQueries({ queryKey: ['load', loadId] }) })
  const d = l.data
  if (!d) return <ErrorBox error={l.error} />
  const rows = d.steps.map((s: Json) => ({ concurrency: s.concurrency, p50: s.p50, p95: s.p95, p99: s.p99, rtfx: s.rtfx, rps: s.req_per_s,
    gpu: Math.max(...Object.values(s.gpu ?? {}).map((g: Json) => g.util_max ?? 0), 0) || null }))
  const total = d.info?.scenario?.effective_steps?.length ?? 1
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3"><h1 className="text-xl font-semibold">Load run</h1><StatusBadge status={d.status} /><span className="mut text-sm">{d.live.requests} requests · {d.live.errors} errors</span>
        {d.status === 'running' && <><div className="w-48"><Progress value={d.steps.length / total} /></div><button className="btn btn-danger" onClick={() => post(`/load-runs/${loadId}/cancel`).then(() => qc.invalidateQueries({ queryKey: ['load', loadId] }))}>Cancel</button></>}</div>
      {d.info?.aborted_reason && <ErrorBox error={d.info.aborted_reason} />}
      {d.saturation && <div className="panel p-3 text-sm">Saturation at concurrency <b>{d.saturation.concurrency}</b> ({d.saturation.reason.replace('_', ' ')})</div>}
      <div className="grid md:grid-cols-2 gap-3">
        <div className="panel p-3"><div className="font-semibold text-sm mb-1">Latency vs concurrency (ms) with GPU util overlay</div>
          <LineSeries data={rows} xKey="concurrency" lines={[{ key: 'p50' }, { key: 'p95' }, { key: 'p99' }, ...(rows.some((r: Json) => r.gpu) ? [{ key: 'gpu', name: 'GPU util %', yAxisId: 'r' }] : [])]} /></div>
        <div className="panel p-3"><div className="font-semibold text-sm mb-1">Throughput</div><LineSeries data={rows} xKey="concurrency" lines={[{ key: 'rtfx', name: 'RTFx' }, { key: 'rps', name: 'req/s' }]} /></div>
      </div>
      <table className="t panel"><thead><tr><th>Concurrency</th><th>p50</th><th>p95</th><th>p99</th><th>RTFx</th><th>req/s</th><th>Errors</th><th>Timeouts</th><th>GPU util / VRAM</th></tr></thead><tbody>
        {d.steps.map((s: Json) => { const g = Object.values(s.gpu ?? {}) as Json[]; return <tr key={s.step}><td>{s.concurrency}</td><td>{fmt(s.p50, 0)}</td><td>{fmt(s.p95, 0)}</td><td>{fmt(s.p99, 0)}</td><td>{fmt(s.rtfx, 1)}</td><td>{fmt(s.req_per_s, 1)}</td><td>{pct(s.error_rate)}</td><td>{s.timeouts}</td>
          <td>{g.length ? `${fmt(Math.max(...g.map(x => x.util_max ?? 0)), 0)}% / ${fmt(Math.max(...g.map(x => x.mem_used_mb_max ?? 0)), 0)} MB` : '—'}</td></tr> })}</tbody></table>
      {tele.data?.samples.length > 0 && <div className="text-xs mut">{tele.data.samples.length} telemetry samples collected at 1 Hz</div>}
    </div>
  )
}
