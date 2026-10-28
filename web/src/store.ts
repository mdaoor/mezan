import { create } from 'zustand'

type User = { id: string; name: string; email: string; role: string }
type State = {
  token: string | null; user: User | null; theme: 'light' | 'dark'; rtl: boolean; projectId: string | null
  setAuth: (t: string, u: User) => void; logout: () => void; toggleTheme: () => void; toggleRtl: () => void; setProject: (id: string) => void
}
const ls = (k: string): string | null => { try { return localStorage.getItem(k) } catch { return null } }
const save = (k: string, v: string | null) => { try { v === null ? localStorage.removeItem(k) : localStorage.setItem(k, v) } catch { /* private mode */ } }

const initialTheme = (ls('theme') as 'light' | 'dark') || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
document.documentElement.dataset.theme = initialTheme

export const useStore = create<State>((set, get) => ({
  token: ls('token'),
  user: (() => { try { return JSON.parse(ls('user') || 'null') } catch { return null } })(),
  theme: initialTheme,
  rtl: ls('rtl') === '1',
  projectId: ls('project'),
  setAuth: (token, user) => { save('token', token); save('user', JSON.stringify(user)); set({ token, user }) },
  logout: () => { save('token', null); save('user', null); set({ token: null, user: null }) },
  toggleTheme: () => { const t = get().theme === 'dark' ? 'light' : 'dark'; document.documentElement.dataset.theme = t; save('theme', t); set({ theme: t }) },
  toggleRtl: () => { const r = !get().rtl; save('rtl', r ? '1' : '0'); set({ rtl: r }) },
  setProject: id => { save('project', id); set({ projectId: id }) },
}))
