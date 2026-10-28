import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { get, Json, pct, post } from '../api'
import { useStore } from '../store'
import { Empty, Ring, Spinner } from '../components/ui'

export default function Dashboard() {
  const projectId = useStore(s => s.projectId)
  const setProject = useStore(s => s.setProject)
  const role = useStore(s => s.user?.role)
  const qc = useQueryClient()
  const [name, setName] = useState('')
  const projects = useQuery({ queryKey: ['projects'], queryFn: () => get('/projects') })
  const datasets = useQuery({ queryKey: ['datasets', projectId], queryFn: () => get(`/projects/${projectId}/datasets`), enabled: !!projectId })
  const runs = useQuery({ queryKey: ['runs', projectId], queryFn: () => get(`/runs?project_id=${projectId}&limit=5`), enabled: !!projectId })
  const create = useMutation({ mutationFn: () => post('/projects', { name }), onSuccess: p => { setName(''); setProject(p.id); qc.invalidateQueries({ queryKey: ['projects'] }) } })
  const [dsName, setDsName] = useState('')
  const createDs = useMutation({ mutationFn: () => post(`/projects/${projectId}/datasets`, { name: dsName, language_hints: ['ar', 'en'] }), onSuccess: () => { setDsName(''); qc.invalidateQueries({ queryKey: ['datasets'] }) } })
  const manage = role === 'admin' || role === 'reviewer'

  if (projects.isLoading) return <Spinner />
  if (!projects.data?.items.length) return (
    <Empty title="Welcome — create your first project" steps={[{ label: 'Create a project' }, { label: 'Upload audio' }, { label: 'Segment' }, { label: 'Bootstrap drafts' }, { label: 'Claim & annotate' }]}>
      {manage ? <div className="flex gap-2 justify-center"><input className="input" placeholder="Project name" value={name} onChange={e => setName(e.target.value)} /><button className="btn btn-primary" disabled={!name} onClick={() => create.mutate()}>Create project</button></div>
        : <span className="mut text-sm">Ask a reviewer or admin to create a project.</span>}
    </Empty>
  )
  return (
    <div className="space-y-4">
      <div className="flex items-end gap-2 flex-wrap">
        <h1 className="text-xl font-semibold">Datasets</h1>
        {manage && <><input className="input ms-auto" placeholder="New dataset name" value={dsName} onChange={e => setDsName(e.target.value)} aria-label="New dataset name" />
          <button className="btn btn-primary" disabled={!dsName || !projectId} onClick={() => createDs.mutate()}>Create dataset</button></>}
      </div>
      <div className="grid gap-3" style={{ gridTemplateColumns: 'repeat(auto-fill,minmax(280px,1fr))' }}>
        {datasets.data?.items.map((d: Json) => (
          <div key={d.id} className="panel p-4 flex gap-3 items-center">
            <Ring value={d.stats.progress} />
            <div className="flex-1 min-w-0">
              <Link to={`/datasets/${d.id}`} className="font-semibold">{d.name}</Link>
              <div className="mut text-xs">{d.stats.files} files · {d.stats.audio_minutes.toFixed(1)} min · {d.stats.segments} segments · {d.stats.gold_sets} gold</div>
              <div className="mt-2 flex gap-2"><Link className="btn btn-primary" to={`/datasets/${d.id}/annotate`}>Annotate</Link>{manage && <Link className="btn" to={`/datasets/${d.id}/review`}>Review</Link>}</div>
            </div>
          </div>))}
        {datasets.data && !datasets.data.items.length && <Empty title="No datasets yet" steps={[{ label: 'Create a dataset' }, { label: 'Upload audio' }, { label: 'Segment' }, { label: 'Bootstrap' }, { label: 'Claim' }]} />}
      </div>
      <h2 className="text-lg font-semibold">Recent runs</h2>
      <table className="t panel"><thead><tr><th>Name</th><th>Type</th><th>Status</th><th>WER</th></tr></thead><tbody>
        {runs.data?.items.map((r: Json) => <tr key={r.id}><td><Link to={`/runs/${r.id}`}>{r.name}</Link></td><td>{r.type}</td><td>{r.status}</td><td>{r.summary?.wer !== undefined ? pct(r.summary.wer) : '—'}</td></tr>)}</tbody></table>
    </div>
  )
}
