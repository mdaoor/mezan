import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { audioUrl, fmt, get, Json, patch, pct, post, useTopics } from '../api'
import { useStore } from '../store'
import { Bars, Spark } from '../components/charts'
import { DiffTokens, RichText } from '../components/text'
import { Empty, ErrorBox, Progress, Ring, StatusBadge, Tabs, useToast } from '../components/ui'

const KEY_METRICS = ['wer', 'cer', 'mer', 'wer_ar', 'wer_en', 'en_preservation', 'hallucination_rate', 'latency_ms_p95']

export function RunsList() {
  const projectId = useStore(s => s.projectId)
  const nav = useNavigate()
  const [sel, setSel] = useState<string[]>([])
  const runs = useQuery({ queryKey: ['runs', projectId, 'all'], queryFn: () => get(`/runs?project_id=${projectId}&limit=100`), enabled: !!projectId, refetchInterval: 4000 })
  const toggle = (id: string) => setSel(s => (s.includes(id) ? s.filter(x => x !== id) : [...s, id]))
  return (
    <div className="space-y-3">
      <div className="flex gap-2 items-center"><h1 className="text-xl font-semibold">Evaluation runs</h1>
        <button className="btn ms-auto" disabled={sel.length < 2} onClick={() => nav(`/runs/compare?ids=${sel.join(',')}`)}>Compare ({sel.length})</button>
        <button className="btn" disabled={sel.length < 2} onClick={() => nav(`/runs/disagreements?ids=${sel.join(',')}`)}>Disagreements</button>
        <Link className="btn btn-primary" to="/runs/new">New run</Link></div>
      <ErrorBox error={runs.error} />
      <table className="t panel"><thead><tr><th /><th>Name</th><th>Type</th><th>Status</th><th>Progress</th><th>WER</th><th>CER</th><th>Started</th></tr></thead><tbody>
        {runs.data?.items.map((r: Json) => <tr key={r.id}><td><input type="checkbox" aria-label={`select ${r.name}`} checked={sel.includes(r.id)} onChange={() => toggle(r.id)} /></td>
          <td><Link to={`/runs/${r.id}`}>{r.name}</Link></td><td>{r.type}</td><td><StatusBadge status={r.status} /></td><td>{r.progress.done}/{r.progress.total}</td>
          <td>{r.summary?.wer !== undefined ? pct(r.summary.wer) : '—'}</td><td>{r.summary?.cer !== undefined ? pct(r.summary.cer) : '—'}</td><td className="mut">{r.started_at ? new Date(r.started_at).toLocaleString() : '—'}</td></tr>)}</tbody></table>
    </div>
  )
}

export function NewRun() {
  const projectId = useStore(s => s.projectId)
  const nav = useNavigate()
  const toast = useToast()
  const [step, setStep] = useState(0)
  const [eps, setEps] = useState<string[]>([])
  const [refType, setRefType] = useState<'gold_set' | 'run'>('gold_set')
  const [refId, setRefId] = useState('')
  const [dataset, setDataset] = useState('')
  const [norm, setNorm] = useState('arabic_v1')
  const [preset, setPreset] = useState('arabic_codeswitch_default')
  const [conc, setConc] = useState<number | ''>('')
  const [lang, setLang] = useState('')
  const [filterKey, setFilterKey] = useState('')
  const [filterVal, setFilterVal] = useState('')
  const endpoints = useQuery({ queryKey: ['endpoints'], queryFn: () => get('/model-endpoints') })
  const datasets = useQuery({ queryKey: ['datasets', projectId], queryFn: () => get(`/projects/${projectId}/datasets`), enabled: !!projectId })
  const golds = useQuery({ queryKey: ['golds', datasets.data], queryFn: async () => (await Promise.all((datasets.data?.items ?? []).map((d: Json) => get(`/datasets/${d.id}/gold-sets`).then(r => r.items.map((g: Json) => ({ ...g, dataset: d.name })))))).flat(), enabled: !!datasets.data })
  const runs = useQuery({ queryKey: ['runs', projectId, 'ok'], queryFn: () => get(`/runs?project_id=${projectId}&status=succeeded&limit=100`), enabled: !!projectId && refType === 'run' })
  const strat = useQuery({ queryKey: ['strategies'], queryFn: () => get('/strategies') })
  const est = useMemo(() => endpoints.data?.items.filter((e: Json) => eps.includes(e.id)), [eps, endpoints.data])
  const go = async () => {
    try {
      const options: Json = { ...(conc ? { concurrency: conc } : {}), ...(lang ? { language_hint: lang } : {}), ...(filterKey && filterVal ? { segment_filter: { [filterKey]: filterVal.split(',') } } : {}) }
      const run: Json = { type: 'evaluation', model_endpoint_id: eps[0], reference: { type: refType, id: refId }, normalizer: { kind: norm }, metric_set: preset, options, ...(refType === 'run' ? { dataset_id: dataset } : {}) }
      const r = await post('/runs/batch', { model_endpoint_ids: eps, run })
      toast.push({ text: `Started ${r.runs.length} run(s)` }); nav(r.runs.length === 1 ? `/runs/${r.runs[0].run_id}` : '/runs')
    } catch (e) { toast.push({ text: (e as Error).message, tone: 'err' }) }
  }
  const ok = [eps.length > 0, !!refId && (refType === 'gold_set' || !!dataset), true][step]
  return (
    <div className="space-y-3 max-w-3xl">
      <h1 className="text-xl font-semibold">New evaluation run</h1>
      <Tabs tabs={['1 · Endpoints', '2 · Reference', '3 · Options']} value={['1 · Endpoints', '2 · Reference', '3 · Options'][step]} onChange={t => setStep(+t[0] - 1)} />
      <div className="panel p-4 space-y-3">
        {step === 0 && <><p className="text-sm mut">One run is created per selected endpoint.</p>{endpoints.data?.items.map((e: Json) => (
          <label key={e.id} className="flex items-center gap-2"><input type="checkbox" checked={eps.includes(e.id)} onChange={() => setEps(x => (x.includes(e.id) ? x.filter(y => y !== e.id) : [...x, e.id]))} />{e.display_name} <span className="chip">{e.engine_kind}</span>{e.external && <span className="chip" style={{ color: 'var(--warn)' }}>⚠ external</span>}</label>))}</>}
        {step === 1 && <>
          <div className="flex gap-2"><button className={`chip ${refType === 'gold_set' ? 'on' : ''}`} onClick={() => { setRefType('gold_set'); setRefId('') }}>Gold set</button><button className={`chip ${refType === 'run' ? 'on' : ''}`} onClick={() => { setRefType('run'); setRefId('') }}>Another run (agreement mode)</button></div>
          {refType === 'gold_set' ? <select className="input" value={refId} onChange={e => setRefId(e.target.value)} aria-label="Gold set"><option value="">choose gold set…</option>{golds.data?.map((g: Json) => <option key={g.id} value={g.id}>{g.dataset} · {g.name} v{g.version} ({g.segment_count} seg)</option>)}</select>
            : <><select className="input" value={refId} onChange={e => setRefId(e.target.value)} aria-label="Reference run"><option value="">choose run…</option>{runs.data?.items.map((r: Json) => <option key={r.id} value={r.id}>{r.name}</option>)}</select>
              <select className="input" value={dataset} onChange={e => setDataset(e.target.value)} aria-label="Dataset"><option value="">dataset…</option>{datasets.data?.items.map((d: Json) => <option key={d.id} value={d.id}>{d.name}</option>)}</select>
              <p className="text-xs mut">Agreement results are labelled “disagreement vs {'{run}'}” and never reported as accuracy.</p></>}
        </>}
        {step === 2 && <div className="grid grid-cols-2 gap-3 text-sm">
          <label>Normalizer<select className="input w-full" value={norm} onChange={e => setNorm(e.target.value)}>{strat.data?.normalizers.map((n: Json) => <option key={n.kind}>{n.kind}</option>)}</select></label>
          <label>Metric set<select className="input w-full" value={preset} onChange={e => setPreset(e.target.value)}>{Object.keys(strat.data?.metric_presets ?? {}).map(k => <option key={k}>{k}</option>)}</select></label>
          <label>Concurrency<input className="input w-full" type="number" value={conc} placeholder="endpoint default" onChange={e => setConc(e.target.value ? +e.target.value : '')} /></label>
          <label>Language hint<input className="input w-full" value={lang} placeholder="ar, en, … (optional)" onChange={e => setLang(e.target.value)} /></label>
          <label>Segment filter (stratum)<input className="input w-full" value={filterKey} placeholder="lang_mix" onChange={e => setFilterKey(e.target.value)} /></label>
          <label>Values<input className="input w-full" value={filterVal} placeholder="mixed,ar" onChange={e => setFilterVal(e.target.value)} /></label>
          <div className="col-span-2 mut text-xs">{est?.length} endpoint(s) selected. Estimated time uses each endpoint's last observed RTFx once a run has completed.</div>
        </div>}
      </div>
      <div className="flex gap-2"><button className="btn" disabled={step === 0} onClick={() => setStep(step - 1)}>Back</button>
        {step < 2 ? <button className="btn btn-primary" disabled={!ok} onClick={() => setStep(step + 1)}>Next</button> : <button className="btn btn-primary" onClick={go} data-testid="start-runs">Start {eps.length} run(s)</button>}</div>
    </div>
  )
}

export function RunDetail() {
  const { runId = '' } = useParams()
  const qc = useQueryClient()
  const toast = useToast()
  const [tab, setTab] = useState('Summary')
  const [stratKey, setStratKey] = useState('lang_mix')
  const [p95s, setP95s] = useState<number[]>([])
  const [live, setLive] = useState<Json>(null)
  const [search, setSearch] = useState('')
  const run = useQuery({ queryKey: ['run', runId], queryFn: () => get(`/runs/${runId}`), refetchInterval: q => (['running', 'pending', 'paused'].includes(q.state.data?.status) ? 3000 : false) })
  const cfg = useQuery({ queryKey: ['rcfg', runId], queryFn: () => get(`/runs/${runId}/resolved-config`), enabled: tab === 'Resolved config' || tab === 'Reproduce' })
  const hyps = useQuery({ queryKey: ['hyps', runId, search], queryFn: () => get(`/runs/${runId}/hypotheses?limit=100${search ? `&q=${encodeURIComponent(search)}` : ''}`), enabled: tab === 'Hypotheses' })
  const scores = useQuery({ queryKey: ['scores', runId, stratKey], queryFn: () => get(`/runs/${runId}/scores?stratum=${stratKey}`), enabled: tab === 'Summary' })
  useTopics([`run:${runId}`], e => {
    if (e.type === 'progress') { setLive(e); if (e.p95_ms != null) setP95s(x => [...x.slice(-60), e.p95_ms]) }
    if (e.type === 'finished') qc.invalidateQueries({ queryKey: ['run', runId] })
  })
  const ctl = async (a: string) => { try { await post(`/runs/${runId}/${a}`); qc.invalidateQueries({ queryKey: ['run', runId] }) } catch (e) { toast.push({ text: (e as Error).message, tone: 'err' }) } }
  const r = run.data
  if (!r) return <ErrorBox error={run.error} />
  const total = r.progress.total || 1
  const s = r.summary
  const strata = useMemo(() => { const g: Record<string, Json[]> = {}; (scores.data?.rows ?? []).forEach((x: Json) => (g[x.stratum_value] ??= []).push(x)); return g }, [scores.data])
  const cli = cfg.data && `mizan run eval --model ${cfg.data.model.endpoint_id} ${cfg.data.reference.type === 'gold_set' ? `--gold ${r.reference.id}` : `--ref-run ${r.reference.id}`} --normalizer ${cfg.data.normalizer.kind}`
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-4 flex-wrap"><Ring value={r.progress.done / total} size={64} />
        <div><h1 className="text-xl font-semibold">{r.name}</h1><div className="text-sm mut"><StatusBadge status={r.status} /> {r.type} · {r.progress.done}/{r.progress.total}{live?.eta_s ? ` · ETA ${Math.round(live.eta_s)} s` : ''}</div></div>
        {p95s.length > 1 && <div><div className="text-xs mut">p95 latency (ms)</div><Spark values={p95s} /></div>}
        <div className="ms-auto flex gap-2">{r.status === 'running' && <button className="btn" onClick={() => ctl('pause')}>Pause</button>}{r.status === 'paused' && <button className="btn" onClick={() => ctl('resume')}>Resume</button>}
          {['running', 'paused', 'pending'].includes(r.status) && <button className="btn btn-danger" onClick={() => ctl('cancel')}>Cancel</button>}{['failed', 'cancelled'].includes(r.status) && <button className="btn" onClick={() => ctl('rerun')}>Resume missing</button>}</div></div>
      {r.error && <ErrorBox error={r.error} />}
      {r.type === 'agreement' && <div className="panel p-2 text-sm">Agreement mode: these numbers are <b>disagreement vs the reference run</b>, not accuracy.</div>}
      <Tabs tabs={['Summary', 'Hypotheses', 'Resolved config', 'Reproduce']} value={tab} onChange={setTab} />
      {tab === 'Summary' && <>
        <div className="grid gap-3" style={{ gridTemplateColumns: 'repeat(auto-fill,minmax(150px,1fr))' }}>
          {KEY_METRICS.filter(k => s[k] !== undefined).map(k => <div key={k} className="panel p-3"><div className="text-xs mut uppercase">{k.replace('_', ' ')}</div><div className="text-2xl font-semibold">{k.startsWith('latency') ? `${fmt(s[k], 0)} ms` : pct(s[k])}</div></div>)}
          {s.rtfx_observed !== undefined && <div className="panel p-3"><div className="text-xs mut uppercase">RTFx observed</div><div className="text-2xl font-semibold">{fmt(s.rtfx_observed, 1)}×</div></div>}
        </div>
        <div className="flex gap-2 items-center"><span className="text-sm mut">Per stratum</span>{['lang_mix', 'duration_bucket', 'dialect', 'domain'].map(k => <button key={k} className={`chip ${stratKey === k ? 'on' : ''}`} onClick={() => setStratKey(k)}>{k}</button>)}</div>
        <table className="t panel"><thead><tr><th>{stratKey}</th><th>n</th><th>WER</th><th>CER</th><th>MER</th><th>WER-AR</th><th>WER-EN</th></tr></thead><tbody>
          {Object.entries(strata).map(([v, rows]) => { const m = Object.fromEntries(rows.map(x => [x.metric_id, x])); return <tr key={v}><td>{v}</td><td>{m.wer?.n ?? '—'}</td>{['wer', 'cer', 'mer', 'wer_ar', 'wer_en'].map(k => <td key={k}>{m[k] ? pct(m[k].value) : '—'}</td>)}</tr> })}</tbody></table>
      </>}
      {tab === 'Hypotheses' && <div className="space-y-2"><input className="input" placeholder="Search hypotheses…" value={search} onChange={e => setSearch(e.target.value)} />
        <table className="t panel"><thead><tr><th /><th>Hypothesis</th><th>WER</th><th>Latency</th></tr></thead><tbody>{hyps.data?.items.map((h: Json) => (
          <tr key={h.id}><td><button className="btn" onClick={() => window.dispatchEvent(new CustomEvent('mizan:play', { detail: audioUrl(h.audio_file_id, h.start_s, h.end_s) }))}>▶</button></td>
            <td><RichText text={h.text} />{h.error && <span style={{ color: 'var(--bad)' }}> {h.error}</span>}</td><td>{h.scores.wer !== undefined ? pct(h.scores.wer) : '—'}</td><td>{h.latency_ms ? `${h.latency_ms.toFixed(0)} ms` : ''}</td></tr>))}</tbody></table></div>}
      {tab === 'Resolved config' && <pre className="panel p-3 text-xs overflow-auto" dir="ltr">{JSON.stringify(cfg.data, null, 2)}</pre>}
      {tab === 'Reproduce' && <div className="panel p-3 space-y-2"><pre className="text-xs overflow-auto" dir="ltr">{cli}</pre><button className="btn" onClick={() => { navigator.clipboard?.writeText(cli ?? ''); toast.push({ text: 'Copied' }) }}>Copy CLI</button>
        <p className="text-xs mut">Same resolved config ⇒ same metrics. Normalizer {cfg.data?.normalizer.kind} v{cfg.data?.normalizer.version}; adapter v{cfg.data?.engine.adapter_version}.</p></div>}
    </div>
  )
}

export function Compare() {
  const [sp] = useSearchParams()
  const ids = sp.get('ids') ?? ''
  const [stratum, setStratum] = useState('')
  const [chart, setChart] = useState(false)
  const c = useQuery({ queryKey: ['compare', ids, stratum], queryFn: () => get(`/runs/compare?ids=${ids}${stratum ? `&stratum=${stratum}` : ''}`) })
  if (!c.data) return <ErrorBox error={c.error} />
  const d = c.data
  const metrics = KEY_METRICS.filter(k => d.metrics.includes(k))
  const Table = ({ t, title }: { t: Json; title: string }) => (
    <div className="space-y-1"><h3 className="font-semibold">{title}</h3>
      <table className="t panel"><thead><tr><th>Run</th>{metrics.map(m => <th key={m}>{m}</th>)}</tr></thead><tbody>
        {d.runs.map((r: Json) => <tr key={r.id}><td>{r.name} {r.id === d.baseline && <span className="chip">baseline</span>}<div className="text-xs mut">{d.labels[r.id]}</div></td>
          {metrics.map(m => { const v = t.runs[r.id]?.[m]?.value; const best = t.best[m] === r.id; const dl = t.delta_vs_baseline[m]?.[r.id]
            return <td key={m} style={best ? { fontWeight: 700, color: 'var(--accent)' } : undefined}>{v === undefined ? '—' : m.startsWith('latency') ? fmt(v, 0) : pct(v)}{dl !== undefined && <div className="text-xs mut">{dl > 0 ? '+' : ''}{m.startsWith('latency') ? fmt(dl, 0) : (dl * 100).toFixed(1) + ' pp'}</div>}</td> })}</tr>)}</tbody></table></div>)
  const data = metrics.filter(m => !m.startsWith('latency')).map(m => ({ metric: m, ...Object.fromEntries(d.runs.map((r: Json) => [r.name, (d.overall.runs[r.id]?.[m]?.value ?? 0) * 100])) }))
  return (
    <div className="space-y-3">
      <div className="flex gap-2 items-center"><h1 className="text-xl font-semibold">Compare runs</h1>
        <select className="input ms-auto" value={stratum} onChange={e => setStratum(e.target.value)} aria-label="Stratum"><option value="">overall only</option>{['lang_mix', 'duration_bucket', 'dialect', 'domain'].map(k => <option key={k}>{k}</option>)}</select>
        <button className="btn" onClick={() => setChart(x => !x)}>{chart ? 'Table' : 'Chart'}</button><Link className="btn" to={`/runs/disagreements?ids=${ids}`}>Disagreements</Link></div>
      {chart ? <Bars data={data} xKey="metric" bars={d.runs.map((r: Json) => ({ key: r.name }))} /> : <Table t={d.overall} title="Overall" />}
      {d.stratum && Object.entries(d.stratum.values).map(([v, t]: [string, Json]) => <Table key={v} t={t} title={`${d.stratum.key} = ${v}`} />)}
    </div>
  )
}

export function Disagreements() {
  const [sp] = useSearchParams()
  const ids = sp.get('ids') ?? ''
  const toast = useToast()
  const [metric, setMetric] = useState('wer')
  const d = useQuery({ queryKey: ['dis', ids, metric], queryFn: () => get(`/runs/disagreements?ids=${ids}&metric=${metric}&top=50`) })
  const runs = useQuery({ queryKey: ['compare-names', ids], queryFn: () => get(`/runs/compare?ids=${ids}`) })
  const names: Record<string, string> = Object.fromEntries((runs.data?.runs ?? []).map((r: Json) => [r.id, r.name]))
  const send = async (id: string) => { try { await post(`/segments/${id}/queue`, { priority: 5 }); toast.push({ text: 'Sent to annotation queue (priority)' }) } catch (e) { toast.push({ text: (e as Error).message, tone: 'err' }) } }
  if (!d.data) return <ErrorBox error={d.error} />
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2"><h1 className="text-xl font-semibold">Disagreement explorer</h1><span className="mut text-sm">{d.data.total_segments} segments · diff vs {d.data.diff_base}</span>
        <select className="input ms-auto" value={metric} onChange={e => setMetric(e.target.value)}><option>wer</option><option>cer</option></select></div>
      {d.data.items.map((it: Json) => (
        <div key={it.segment_id} className="panel p-3 space-y-2">
          <div className="flex items-center gap-2"><button className="btn" onClick={() => window.dispatchEvent(new CustomEvent('mizan:play', { detail: audioUrl(it.audio_file_id, it.start_s, it.end_s) }))}>▶</button>
            <span className="chip">distance {it.distance.toFixed(2)}</span><button className="btn ms-auto" onClick={() => send(it.segment_id)}>Send to annotation</button></div>
          {it.reference != null && <div><span className="text-xs mut">reference </span><RichText text={it.reference} /></div>}
          {it.hypotheses.map((h: Json) => <div key={h.run_id}><span className="text-xs mut">{names[h.run_id] ?? h.run_id.slice(0, 6)} </span><DiffTokens diff={h.diff} /></div>)}
        </div>))}
    </div>
  )
}
