import { useEffect, useMemo, useState } from 'react'
import { Link, NavLink, Navigate, Route, Routes, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { get, Json } from './api'
import { useStore } from './store'
import Login from './pages/Login'
import Dashboard from './pages/Dashboard'
import { DatasetDetail } from './pages/Datasets'
import Workbench from './pages/Workbench'
import Review from './pages/Review'
import Models from './pages/Models'
import { RunsList, NewRun, RunDetail, Compare, Disagreements } from './pages/Runs'
import { LoadList, NewLoad, LoadDetail } from './pages/Load'
import { Campaigns, CampaignDetail, ReportView } from './pages/Reports'
import Team from './pages/Team'

const NAV = [['/', 'Dashboard'], ['/models', 'Models & Engines'], ['/runs', 'Eval runs'], ['/load', 'Load runs'], ['/campaigns', 'Campaigns & Reports'], ['/team', 'Team']]

export default function App() {
  const { token, user, rtl, theme, toggleTheme, toggleRtl, logout, projectId, setProject } = useStore()
  const nav = useNavigate()
  const [palette, setPalette] = useState(false)
  useEffect(() => { document.documentElement.dir = rtl ? 'rtl' : 'ltr' }, [rtl])
  useEffect(() => {
    const h = (e: KeyboardEvent) => { if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k' && !location.hash.includes('/annotate/')) { e.preventDefault(); setPalette(p => !p) } }
    window.addEventListener('keydown', h); return () => window.removeEventListener('keydown', h)
  }, [])
  const projects = useQuery({ queryKey: ['projects'], queryFn: () => get('/projects'), enabled: !!token })
  useEffect(() => { if (projects.data?.items.length && !projects.data.items.find((p: Json) => p.id === projectId)) setProject(projects.data.items[0].id) }, [projects.data])
  if (!token) return <Login />
  return (
    <div className="min-h-full flex flex-col">
      <header className="flex items-center gap-4 px-4 py-2 panel rounded-none border-x-0 border-t-0">
        <Link to="/" className="font-bold text-lg" style={{ color: 'var(--accent)' }}>ميزان Mizan</Link>
        <nav className="flex gap-3 text-sm flex-wrap">{NAV.map(([to, label]) => <NavLink key={to} to={to} end={to === '/'} className={({ isActive }) => (isActive ? 'font-semibold underline' : 'mut')}>{label}</NavLink>)}</nav>
        <div className="ms-auto flex items-center gap-2 text-sm">
          <select className="input" value={projectId ?? ''} onChange={e => setProject(e.target.value)} aria-label="Project">{projects.data?.items.map((p: Json) => <option key={p.id} value={p.id}>{p.name}</option>)}</select>
          <button className="btn" onClick={() => setPalette(true)} title="Command palette">⌘K</button>
          <button className="btn" onClick={toggleTheme} aria-label="Toggle theme">{theme === 'dark' ? '☀' : '☾'}</button>
          <button className="btn" onClick={toggleRtl} aria-label="Toggle RTL layout">{rtl ? 'LTR' : 'RTL'}</button>
          <span className="mut">{user?.name} · {user?.role}</span>
          <button className="btn" onClick={logout}>Sign out</button>
        </div>
      </header>
      <main className="flex-1 p-4 max-w-[1500px] w-full mx-auto">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/datasets/:datasetId" element={<DatasetDetail />} />
          <Route path="/datasets/:datasetId/annotate" element={<Workbench />} />
          <Route path="/datasets/:datasetId/review" element={<Review />} />
          <Route path="/review" element={<Review />} />
          <Route path="/models" element={<Models />} />
          <Route path="/runs" element={<RunsList />} />
          <Route path="/runs/new" element={<NewRun />} />
          <Route path="/runs/compare" element={<Compare />} />
          <Route path="/runs/disagreements" element={<Disagreements />} />
          <Route path="/runs/:runId" element={<RunDetail />} />
          <Route path="/load" element={<LoadList />} />
          <Route path="/load/new" element={<NewLoad />} />
          <Route path="/load/:loadId" element={<LoadDetail />} />
          <Route path="/campaigns" element={<Campaigns />} />
          <Route path="/campaigns/:campaignId" element={<CampaignDetail />} />
          <Route path="/reports/:reportId" element={<ReportView />} />
          <Route path="/team" element={<Team />} />
          <Route path="*" element={<Navigate to="/" />} />
        </Routes>
      </main>
      <MiniPlayer />
      {palette && <Palette onClose={() => setPalette(false)} go={p => { setPalette(false); nav(p) }} projectId={projectId} />}
    </div>
  )
}

/** Persistent mini-player: pages publish the active clip via window event; it keeps playing across navigation. */
function MiniPlayer() {
  const [src, setSrc] = useState<string | null>(null)
  useEffect(() => { const h = (e: Event) => setSrc((e as CustomEvent).detail); window.addEventListener('mizan:play', h); return () => window.removeEventListener('mizan:play', h) }, [])
  if (!src) return null
  return <div className="fixed bottom-0 inset-x-0 panel rounded-none p-2 flex items-center gap-3 z-30"><audio src={src} controls autoPlay className="h-8 flex-1" /><button className="btn" onClick={() => setSrc(null)}>×</button></div>
}

function Palette({ onClose, go, projectId }: { onClose: () => void; go: (p: string) => void; projectId: string | null }) {
  const [q, setQ] = useState('')
  const datasets = useQuery({ queryKey: ['datasets', projectId], queryFn: () => get(`/projects/${projectId}/datasets`), enabled: !!projectId })
  const items = useMemo(() => {
    const ds: Json[] = datasets.data?.items ?? []
    const out: [string, string][] = [['New evaluation run', '/runs/new'], ['New load run', '/load/new'], ['Models & Engines', '/models'], ['Campaigns', '/campaigns'], ['Team', '/team'], ['Review queue', '/review']]
    ds.forEach(d => { out.push([`Annotate next batch · ${d.name}`, `/datasets/${d.id}/annotate`], [`Dataset · ${d.name}`, `/datasets/${d.id}`]) })
    return out.filter(([l]) => l.toLowerCase().includes(q.toLowerCase()))
  }, [q, datasets.data])
  const run = q.match(/^open run #?(\S+)/i)
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center pt-24" style={{ background: 'rgba(0,0,0,.4)' }} onClick={onClose}>
      <div className="panel w-full max-w-lg p-2" onClick={e => e.stopPropagation()} role="dialog" aria-label="Command palette">
        <input autoFocus className="input w-full" placeholder="Type a command…  (open run <id>)" value={q} onChange={e => setQ(e.target.value)}
          onKeyDown={e => { if (e.key === 'Escape') onClose(); if (e.key === 'Enter') { if (run) go(`/runs/${run[1]}`); else if (items[0]) go(items[0][1]) } }} />
        <ul className="mt-2 max-h-72 overflow-auto">{run && <li><button className="w-full text-start px-2 py-1" onClick={() => go(`/runs/${run[1]}`)}>Open run {run[1]}</button></li>}
          {items.map(([l, p]) => <li key={p + l}><button className="w-full text-start px-2 py-1 rounded hover:bg-black/5" onClick={() => go(p)}>{l}</button></li>)}</ul>
      </div>
    </div>
  )
}
