import { useState } from 'react'
import { api } from '../api'
import { useStore } from '../store'

export default function Login() {
  const [email, setEmail] = useState('admin@mizan.local')
  const [password, setPassword] = useState('')
  const [err, setErr] = useState('')
  const setAuth = useStore(s => s.setAuth)
  const go = async (e: React.FormEvent) => {
    e.preventDefault(); setErr('')
    try { const r = await api('POST', '/auth/login', { email, password }); setAuth(r.access_token, r.user) } catch (x) { setErr((x as Error).message || 'Login failed') }
  }
  return (
    <div className="h-full flex items-center justify-center">
      <form onSubmit={go} className="panel p-6 w-80 space-y-3">
        <h1 className="text-xl font-bold" style={{ color: 'var(--accent)' }}>ميزان Mizan</h1>
        <p className="mut text-sm">Golden data, model evaluation and load tests in one workbench.</p>
        <input className="input w-full" type="email" value={email} onChange={e => setEmail(e.target.value)} aria-label="Email" data-testid="email" />
        <input className="input w-full" type="password" value={password} onChange={e => setPassword(e.target.value)} placeholder="Password" aria-label="Password" data-testid="password" />
        {err && <div style={{ color: 'var(--bad)' }} className="text-sm" role="alert">{err}</div>}
        <button className="btn btn-primary w-full" data-testid="login">Sign in</button>
      </form>
    </div>
  )
}
