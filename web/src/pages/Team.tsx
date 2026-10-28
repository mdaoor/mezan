import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { get, Json, patch, pct, post } from '../api'
import { useStore } from '../store'
import { useToast } from '../components/ui'

export default function Team() {
  const me = useStore(s => s.user)
  const projectId = useStore(s => s.projectId)
  const qc = useQueryClient()
  const toast = useToast()
  const c = useQuery({ queryKey: ['contrib', projectId], queryFn: () => get(`/team/contributions${projectId ? `?project=${projectId}` : ''}`) })
  const users = useQuery({ queryKey: ['users'], queryFn: () => get('/users'), enabled: me?.role === 'admin' || me?.role === 'reviewer' })
  const [f, setF] = useState({ email: '', name: '', role: 'annotator', password: '' })
  const add = useMutation({ mutationFn: () => post('/users', f), onSuccess: () => { setF({ email: '', name: '', role: 'annotator', password: '' }); qc.invalidateQueries({ queryKey: ['users'] }); toast.push({ text: 'User created' }) }, onError: e => toast.push({ text: (e as Error).message, tone: 'err' }) })
  const optIn = useMutation({ mutationFn: (v: boolean) => patch('/me/preferences', { leaderboard_opt_in: v }), onSuccess: () => qc.invalidateQueries({ queryKey: ['contrib'] }) })
  const mine = c.data?.users.find((u: Json) => u.user_id === me?.id)
  return (
    <div className="space-y-4"><h1 className="text-xl font-semibold">Team</h1>
      <div className="panel p-3 flex items-center gap-4 flex-wrap"><b>Your contribution</b>
        {mine ? <><span>{mine.minutes_annotated} min annotated</span><span>{mine.segments_reviewed} reviewed</span><span>🔥 {mine.streak_days}-day streak</span>{mine.approval_rate != null && <span>approval {pct(mine.approval_rate, 0)}</span>}{mine.iaa != null && <span>IAA {pct(mine.iaa, 0)}</span>}</> : <span className="mut">no activity yet</span>}
        <label className="ms-auto text-sm"><input type="checkbox" checked={!!mine?.leaderboard_opt_in} onChange={e => optIn.mutate(e.target.checked)} /> Show me on the leaderboard</label></div>
      <div className="grid md:grid-cols-2 gap-3">
        <div className="panel p-3"><b>Leaderboard (opt-in)</b><table className="t"><thead><tr><th>Name</th><th>Minutes</th><th>Reviews</th><th>Streak</th></tr></thead><tbody>
          {c.data?.leaderboard.map((u: Json) => <tr key={u.user_id}><td>{u.name}</td><td>{u.minutes_annotated}</td><td>{u.segments_reviewed}</td><td>{u.streak_days}</td></tr>)}</tbody></table>
          {c.data && !c.data.leaderboard.length && <div className="mut text-sm">Nobody has opted in yet.</div>}</div>
        {(me?.role === 'admin' || me?.role === 'reviewer') && <div className="panel p-3"><b>Contributors</b><table className="t"><thead><tr><th>Name</th><th>Role</th><th>Min</th><th>Approval</th><th>IAA</th></tr></thead><tbody>
          {c.data?.users.map((u: Json) => <tr key={u.user_id}><td>{u.name}</td><td>{u.role}</td><td>{u.minutes_annotated}</td><td>{u.approval_rate == null ? '—' : pct(u.approval_rate, 0)}</td><td>{u.iaa == null ? '—' : pct(u.iaa, 0)}</td></tr>)}</tbody></table>
          <div className="text-xs mut">Quality signals are visible to reviewers only, never on the public leaderboard.</div></div>}
      </div>
      {me?.role === 'admin' && <div className="panel p-3 space-y-2"><b>Users &amp; roles</b>
        <table className="t"><tbody>{users.data?.items.map((u: Json) => <tr key={u.id}><td>{u.name}</td><td>{u.email}</td><td><select className="input" value={u.role} onChange={async e => { try { await patch(`/users/${u.id}`, { role: e.target.value }); qc.invalidateQueries({ queryKey: ['users'] }) } catch (x) { toast.push({ text: (x as Error).message, tone: 'err' }) } }}>{['viewer', 'annotator', 'reviewer', 'admin'].map(r => <option key={r}>{r}</option>)}</select></td></tr>)}</tbody></table>
        <div className="flex gap-2 flex-wrap"><input className="input" placeholder="email" value={f.email} onChange={e => setF({ ...f, email: e.target.value })} /><input className="input" placeholder="name" value={f.name} onChange={e => setF({ ...f, name: e.target.value })} />
          <select className="input" value={f.role} onChange={e => setF({ ...f, role: e.target.value })}>{['viewer', 'annotator', 'reviewer', 'admin'].map(r => <option key={r}>{r}</option>)}</select>
          <input className="input" type="password" placeholder="password" value={f.password} onChange={e => setF({ ...f, password: e.target.value })} /><button className="btn btn-primary" disabled={!f.email || !f.name || f.password.length < 6} onClick={() => add.mutate()}>Add user</button></div></div>}
    </div>
  )
}
