import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { fmt, get, Json, patch, post } from '../api'
import { useStore } from '../store'
import { LineSeries, Bars } from '../components/charts'
import { Empty, ErrorBox, StatusBadge, useToast } from '../components/ui'

export function Campaigns() {
  const projectId = useStore(s => s.projectId)
  const qc = useQueryClient()
  const nav = useNavigate()
  const [name, setName] = useState(''); const [question, setQuestion] = useState('')
  const list = useQuery({ queryKey: ['campaigns', projectId], queryFn: () => get(`/campaigns?project_id=${projectId}`), enabled: !!projectId })
  const create = useMutation({ mutationFn: () => post('/campaigns', { project_id: projectId, name, question }), onSuccess: c => { qc.invalidateQueries({ queryKey: ['campaigns'] }); nav(`/campaigns/${c.id}`) } })
  return (
    <div className="space-y-3"><h1 className="text-xl font-semibold">Campaigns &amp; Reports</h1>
      <div className="panel p-3 flex gap-2 flex-wrap"><input className="input" placeholder="Campaign name" value={name} onChange={e => setName(e.target.value)} /><input className="input flex-1" placeholder="The question this decides (e.g. Replace Whisper for Arabic+English?)" value={question} onChange={e => setQuestion(e.target.value)} />
        <button className="btn btn-primary" disabled={!name} onClick={() => create.mutate()}>Create campaign</button></div>
      {list.data?.items.map((c: Json) => <Link key={c.id} to={`/campaigns/${c.id}`} className="panel p-3 block"><b>{c.name}</b> <span className="mut text-sm">{c.run_ids.length} eval runs · {c.load_run_ids.length} load runs</span><div className="mut text-sm bidi" dir="auto">{c.question}</div></Link>)}
      {list.data && !list.data.items.length && <Empty title="No campaigns yet" />}</div>
  )
}

export function CampaignDetail() {
  const { campaignId = '' } = useParams()
  const qc = useQueryClient()
  const nav = useNavigate()
  const toast = useToast()
  const projectId = useStore(s => s.projectId)
  const c = useQuery({ queryKey: ['campaign', campaignId], queryFn: () => get(`/campaigns/${campaignId}`) })
  const reports = useQuery({ queryKey: ['reports', campaignId], queryFn: () => get(`/campaigns/${campaignId}/reports`), refetchInterval: q => (q.state.data?.items.some((r: Json) => r.status === 'pending') ? 1500 : false) })
  const runs = useQuery({ queryKey: ['runs', projectId, 'ok'], queryFn: () => get(`/runs?project_id=${projectId}&status=succeeded&limit=100`), enabled: !!projectId })
  const loads = useQuery({ queryKey: ['loadruns', projectId, 'ok'], queryFn: () => get(`/load-runs?project_id=${projectId}`), enabled: !!projectId })
  const eps = useQuery({ queryKey: ['endpoints'], queryFn: () => get('/model-endpoints') })
  const epName = (id: string) => eps.data?.items.find((e: Json) => e.id === id)?.display_name ?? id.slice(0, 6)
  const attach = async (kind: 'run' | 'load', id: string) => { await patch(`/campaigns/${campaignId}`, kind === 'run' ? { add_run_ids: [id] } : { add_load_run_ids: [id] }); qc.invalidateQueries({ queryKey: ['campaign', campaignId] }) }
  const gen = async () => { try { await post(`/campaigns/${campaignId}/reports`, { template: 'decision_v1' }); qc.invalidateQueries({ queryKey: ['reports', campaignId] }); toast.push({ text: 'Generating report…' }) } catch (e) { toast.push({ text: (e as Error).message, tone: 'err' }) } }
  const d = c.data
  if (!d) return <ErrorBox error={c.error} />
  return (
    <div className="space-y-3">
      <div className="flex items-center"><div><h1 className="text-xl font-semibold">{d.name}</h1><div className="bidi mut" dir="auto">{d.question}</div></div><button className="btn btn-primary ms-auto" onClick={gen} data-testid="generate">Generate report</button></div>
      <div className="grid md:grid-cols-2 gap-3">
        <div className="panel p-3"><b>Evaluation runs</b>{runs.data?.items.map((r: Json) => <label key={r.id} className="flex gap-2 items-center text-sm"><input type="checkbox" checked={d.run_ids.includes(r.id)} disabled={d.run_ids.includes(r.id)} onChange={() => attach('run', r.id)} />{r.name} <span className="mut">{r.summary?.wer !== undefined ? `WER ${(r.summary.wer * 100).toFixed(1)}%` : ''}</span></label>)}</div>
        <div className="panel p-3"><b>Load runs</b>{loads.data?.items.filter((l: Json) => l.status === 'succeeded').map((l: Json) => <label key={l.id} className="flex gap-2 items-center text-sm"><input type="checkbox" checked={d.load_run_ids.includes(l.id)} disabled={d.load_run_ids.includes(l.id)} onChange={() => attach('load', l.id)} />{epName(l.model_endpoint_id)} <span className="mut">RTFx {fmt(l.best_rtfx, 1)}</span></label>)}</div>
      </div>
      <h2 className="font-semibold">Report versions</h2>
      <table className="t panel"><tbody>{reports.data?.items.map((r: Json) => <tr key={r.id}><td>v{r.version}</td><td><StatusBadge status={r.status} /></td><td className="mut">{r.generated_at ? new Date(r.generated_at).toLocaleString() : ''}</td><td>{r.status === 'ready' && <Link className="btn" to={`/reports/${r.id}`}>Open</Link>}{r.error && <span style={{ color: 'var(--bad)' }}>{r.error}</span>}</td></tr>)}</tbody></table>
    </div>
  )
}

const cell = (v: unknown) => (v === null || v === undefined ? '—' : typeof v === 'number' ? fmt(v) : String(v)).replace(/\|/g, '\\|')
const toMd = (b: Json) => `| ${b.columns.join(' | ')} |\n|${b.columns.map(() => '---').join('|')}|\n${b.rows.map((r: Json[]) => `| ${r.map(cell).join(' | ')} |`).join('\n')}`

function Block({ b }: { b: Json }) {
  const toast = useToast()
  if (b.type === 'paragraph') return <p className="bidi" dir="auto">{b.text}</p>
  if (b.type === 'heading') return <h3 className="font-semibold mt-3">{b.text}</h3>
  if (b.type === 'bullets') return <ul className="list-disc ps-5">{b.items.map((i: string, k: number) => <li key={k} className="bidi" dir="auto">{i}</li>)}</ul>
  if (b.type === 'code') return <pre className="panel p-3 text-xs overflow-auto" dir="ltr">{b.text}</pre>
  if (b.type === 'table') return (
    <div className="space-y-1">{b.title && <div className="text-sm font-semibold">{b.title}</div>}
      <div className="overflow-auto"><table className="t panel"><thead><tr>{b.columns.map((c: string) => <th key={c}>{c}</th>)}</tr></thead><tbody>{b.rows.map((r: Json[], i: number) => <tr key={i}>{r.map((v, j) => <td key={j} className="bidi" dir="auto">{cell(v)}</td>)}</tr>)}</tbody></table></div>
      <button className="text-xs mut" onClick={() => { navigator.clipboard?.writeText(toMd(b)); toast.push({ text: 'Copied as Markdown' }) }}>Copy as Markdown</button></div>)
  if (b.type === 'chart') {
    if (b.kind === 'bar') return <div className="panel p-3"><div className="text-sm font-semibold">{b.title}</div><Bars data={b.categories.map((c: string, i: number) => ({ x: c, ...Object.fromEntries(b.series.map((s: Json) => [s.name, s.values[i]])) }))} xKey="x" bars={b.series.map((s: Json) => ({ key: s.name }))} height={200} /></div>
    const xs = [...new Set(b.series.flatMap((s: Json) => s.points.map((p: number[]) => p[0])))].sort((a: any, z: any) => a - z)
    return <div className="panel p-3"><div className="text-sm font-semibold">{b.title}</div><LineSeries data={xs.map((x: any) => ({ x, ...Object.fromEntries(b.series.map((s: Json) => [s.name, s.points.find((p: number[]) => p[0] === x)?.[1]])) }))} xKey="x" lines={b.series.map((s: Json) => ({ key: s.name }))} height={200} yLabel={b.y_label} /></div>
  }
  return null
}

export function ReportView() {
  const { reportId = '' } = useParams()
  const qc = useQueryClient()
  const toast = useToast()
  const rep = useQuery({ queryKey: ['report', reportId], queryFn: () => get(`/reports/${reportId}`) })
  const [weights, setWeights] = useState<Json | null>(null)
  const [summary, setSummary] = useState<string | null>(null)
  const timer = useRef<number>()
  useEffect(() => { if (rep.data?.decision && !weights) setWeights(rep.data.decision.weights) }, [rep.data])
  const save = (w: Json) => { clearTimeout(timer.current); timer.current = window.setTimeout(async () => { try { await patch(`/reports/${reportId}`, { weights: w }); qc.invalidateQueries({ queryKey: ['report', reportId] }) } catch (e) { toast.push({ text: (e as Error).message, tone: 'err' }) } }, 250) }
  const r = rep.data
  if (!r) return <ErrorBox error={rep.error} />
  const names = r.decision.endpoint_names
  const comps = r.decision.components
  // live re-rank in the browser for instant feedback; the server remains authoritative (PATCH persists + re-ranks)
  const w = weights ?? r.decision.weights
  const live = Object.keys(comps).map(ep => { const av = Object.entries(w).filter(([k, v]: [string, any]) => comps[ep][k] != null && v > 0); const tw = av.reduce((a, [, v]: any) => a + v, 0); return { ep, score: tw ? av.reduce((a, [k, v]: any) => a + comps[ep][k] * v, 0) / tw : null } }).sort((a, b) => (b.score ?? -1) - (a.score ?? -1))
  const exp = (f: string) => window.open(`/api/v1/reports/${reportId}/export?format=${f}&token=${localStorage.getItem('token')}`)
  return (
    <div className="grid gap-4" style={{ gridTemplateColumns: '220px 1fr' }}>
      <nav className="panel p-3 h-fit sticky top-2 text-sm space-y-1" aria-label="Contents">{r.sections.map((s: Json, i: number) => <a key={s.id} href={`#sec-${s.id}`} className="block">{i + 1}. {s.title}</a>)}
        <div className="pt-2 flex flex-wrap gap-1">{['md', 'html', 'json', 'csv'].map(f => <button key={f} className="btn" onClick={() => exp(f)}>{f.toUpperCase()}</button>)}</div></nav>
      <article className="space-y-4 min-w-0">
        <header><h1 className="text-2xl font-bold">{r.title}</h1><div className="mut bidi" dir="auto">{r.question}</div>{r.header.map((h: string) => <div key={h} className="text-sm mut">{h.replace(/\*\*/g, '')}</div>)}</header>
        {r.sections.map((s: Json, i: number) => (
          <section key={s.id} id={`sec-${s.id}`} className="space-y-2"><h2 className="text-lg font-semibold border-b pb-1" style={{ borderColor: 'var(--line)' }}>{i + 1}. {s.title}</h2>
            {s.id === 'summary' ? <><textarea className="input w-full bidi" dir="auto" rows={4} value={summary ?? r.summary_text} onChange={e => setSummary(e.target.value)} aria-label="Executive summary" />
              {summary !== null && summary !== r.summary_text && <button className="btn" onClick={async () => { await patch(`/reports/${reportId}`, { summary_text: summary }); setSummary(null); qc.invalidateQueries({ queryKey: ['report', reportId] }) }}>Save summary</button>}
              {s.blocks.slice(1).map((b: Json, k: number) => <Block key={k} b={b} />)}</>
              : s.id === 'decision' ? <>
                <div className="panel p-3 space-y-2" aria-label="Decision weights">{Object.keys(w).map(k => (
                  <label key={k} className="flex items-center gap-3 text-sm"><span className="w-28">{k.replace('_', '-')}</span>
                    <input type="range" min={0} max={100} value={Math.round(w[k] * 100)} onChange={e => { const nw = { ...w, [k]: +e.target.value / 100 }; setWeights(nw); save(nw) }} className="flex-1" /><span className="w-10 text-end">{Math.round(w[k] * 100)}</span></label>))}
                  <ol className="text-sm list-decimal ps-5" data-testid="live-ranking">{live.map(x => <li key={x.ep}>{names[x.ep]} — {x.score == null ? 'n/a' : x.score.toFixed(3)}</li>)}</ol></div>
                {s.blocks.map((b: Json, k: number) => <Block key={k} b={b} />)}</>
                : s.blocks.map((b: Json, k: number) => <Block key={k} b={b} />)}
          </section>))}
      </article>
    </div>
  )
}
