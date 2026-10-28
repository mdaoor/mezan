import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { del, get, Json, post } from '../api'
import { useStore } from '../store'
import { Drawer, ErrorBox, SchemaForm, StatusBadge, useToast } from '../components/ui'

const CAPS: [string, string][] = [['batch', 'batch'], ['streaming', 'stream'], ['language_hint', 'lang hint'], ['word_timestamps', 'word ts']]

export default function Models() {
  const qc = useQueryClient()
  const toast = useToast()
  const role = useStore(s => s.user?.role)
  const manage = role === 'admin' || role === 'reviewer'
  const [open, setOpen] = useState(false)
  const [results, setResults] = useState<Record<string, Json>>({})
  const eps = useQuery({ queryKey: ['endpoints'], queryFn: () => get('/model-endpoints') })
  const test = useMutation({ mutationFn: (id: string) => post(`/model-endpoints/${id}/test`), onSuccess: (r, id) => { setResults(x => ({ ...x, [id]: r })); qc.invalidateQueries({ queryKey: ['endpoints'] }) } })
  const remove = async (e: Json) => {
    try { await del(`/model-endpoints/${e.id}`); qc.invalidateQueries({ queryKey: ['endpoints'] }); toast.push({ text: `Deleted ${e.display_name}` }) } catch (x) { toast.push({ text: (x as Error).message, tone: 'err' }) }
  }
  return (
    <div className="space-y-3">
      <div className="flex items-center"><h1 className="text-xl font-semibold">Models &amp; Engines</h1>{manage && <button className="btn btn-primary ms-auto" onClick={() => setOpen(true)} data-testid="add-endpoint">Add endpoint</button>}</div>
      <ErrorBox error={eps.error} />
      <div className="grid gap-3" style={{ gridTemplateColumns: 'repeat(auto-fill,minmax(340px,1fr))' }}>
        {eps.data?.items.map((e: Json) => {
          const r = results[e.id]
          return (
            <div key={e.id} className="panel p-4 space-y-2">
              <div className="flex items-center gap-2"><b className="flex-1">{e.display_name}</b><StatusBadge status={e.status} />{e.external && <span className="chip" style={{ color: 'var(--warn)', borderColor: 'var(--warn)' }} title="Audio and transcripts leave this host">⚠ external</span>}</div>
              <div className="text-sm"><span className="chip">{e.engine_kind}</span> <code className="text-xs">{e.model_id}</code></div>
              <div className="text-xs mut break-all">{e.base_url}</div>
              <div className="flex gap-1 flex-wrap text-xs">{CAPS.map(([k, l]) => <span key={k} className="chip" style={{ opacity: e.capabilities?.[k] ? 1 : .4 }}>{e.capabilities?.[k] ? '✓' : '✗'} {l}</span>)}<span className="chip">≤{e.capabilities?.max_segment_s}s</span><span className="chip">×{e.config?.max_concurrency ?? e.capabilities?.concurrency_hint}</span></div>
              <div className="text-xs mut">Last check: {e.last_health_at ? new Date(e.last_health_at).toLocaleString() : 'never'}</div>
              {r && <div className="text-sm p-2 rounded" style={{ background: 'var(--bg)' }}>{r.smoke.ok ? <><b>{r.smoke.latency_ms.toFixed(0)} ms</b> · <span className="bidi" dir="auto">{r.smoke.text || '(empty)'}</span></> : <span style={{ color: 'var(--bad)' }}>{r.smoke.error}: {r.smoke.detail ?? r.health.detail}</span>}</div>}
              <div className="flex gap-2">{manage && <button className="btn" onClick={() => test.mutate(e.id)} disabled={test.isPending}>Test</button>}{manage && <button className="btn btn-danger" onClick={() => remove(e)}>Delete</button>}</div>
            </div>)
        })}
      </div>
      <AddEndpoint open={open} onClose={() => setOpen(false)} />
    </div>
  )
}

function AddEndpoint({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient()
  const toast = useToast()
  const engines = useQuery({ queryKey: ['engines'], queryFn: () => get('/engines'), enabled: open })
  const presets = useQuery({ queryKey: ['presets'], queryFn: () => get('/model-endpoint-presets'), enabled: open })
  const [kind, setKind] = useState('vllm_openai')
  const [cfg, setCfg] = useState<Json>({})
  const [display, setDisplay] = useState('')
  const [tested, setTested] = useState<Json | null>(null)
  const eng = engines.data?.items.find((e: Json) => e.kind === kind)
  const apply = (p: Json) => { setKind(p.engine_kind); setCfg({ ...p.config, model: p.model_id, base_url: p.base_url }); setDisplay(p.display_name); setTested(null) }
  const build = () => { const { model, base_url, ...rest } = cfg; return { engine_kind: kind, model_id: model, display_name: display || model, base_url: base_url ?? '', config: rest } }
  const create = async (testAfter: boolean) => {
    try {
      const ep = await post('/model-endpoints', build())
      if (testAfter) { const r = await post(`/model-endpoints/${ep.id}/test`); setTested(r); toast.push({ text: r.ok ? 'Endpoint works' : 'Saved, but the test failed', tone: r.ok ? 'ok' : 'err' }) } else toast.push({ text: 'Endpoint saved' })
      qc.invalidateQueries({ queryKey: ['endpoints'] }); if (!testAfter || tested) onClose()
    } catch (e) { toast.push({ text: (e as Error).message, tone: 'err' }) }
  }
  return (
    <Drawer open={open} onClose={onClose} title="Add endpoint">
      <div className="space-y-3">
        <div><div className="text-xs mut mb-1">Presets</div><div className="flex gap-2 flex-wrap">{presets.data?.items.map((p: Json) => <button key={p.display_name} className="chip" onClick={() => apply(p)}>{p.display_name}</button>)}</div></div>
        <label className="block text-sm">Engine<select className="input w-full" value={kind} onChange={e => { setKind(e.target.value); setCfg({}) }} aria-label="Engine kind">{engines.data?.items.map((e: Json) => <option key={e.kind} value={e.kind}>{e.kind}{e.external ? '  (external)' : ''}</option>)}</select></label>
        {eng?.external && <div className="text-sm p-2 rounded" style={{ background: 'color-mix(in srgb, var(--warn) 18%, transparent)' }} role="alert">⚠ External engine: audio and transcripts are sent to a third party.</div>}
        <label className="block text-sm">Display name<input className="input w-full" value={display} onChange={e => setDisplay(e.target.value)} /></label>
        {eng && <SchemaForm schema={eng.config_schema} value={cfg} onChange={setCfg} />}
        {tested && <div className="text-sm">{tested.ok ? '✓ health and smoke test passed' : `✗ ${tested.smoke.error ?? tested.health.detail}`}</div>}
        <div className="flex gap-2"><button className="btn btn-primary" onClick={() => create(true)}>Test &amp; save</button><button className="btn" onClick={() => create(false)}>Save without testing</button></div>
      </div>
    </Drawer>
  )
}
