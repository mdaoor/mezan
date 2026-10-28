import { useEffect, useRef } from 'react'
import { useStore } from './store'

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type Json = any

export class ApiError extends Error {
  constructor(public status: number, message: string, public body?: Json) { super(message) }
}

export async function api<T = Json>(method: string, path: string, body?: Json, opts: { form?: FormData } = {}): Promise<T> {
  const token = useStore.getState().token
  const headers: Record<string, string> = token ? { Authorization: `Bearer ${token}` } : {}
  let payload: BodyInit | undefined
  if (opts.form) payload = opts.form
  else if (body !== undefined) { headers['Content-Type'] = 'application/json'; payload = JSON.stringify(body) }
  const r = await fetch(`/api/v1${path}`, { method, headers, body: payload })
  if (r.status === 401) { useStore.getState().logout(); throw new ApiError(401, 'Session expired') }
  const ct = r.headers.get('content-type') || ''
  const data = ct.includes('json') ? await r.json() : await r.text()
  if (!r.ok) {
    const msg = data?.error?.message || data?.detail || (typeof data === 'string' ? data : r.statusText)
    throw new ApiError(r.status, typeof msg === 'string' ? msg : JSON.stringify(msg), data)
  }
  return data as T
}

export const get = <T = Json>(p: string) => api<T>('GET', p)
export const post = <T = Json>(p: string, b?: Json) => api<T>('POST', p, b ?? {})
export const put = <T = Json>(p: string, b?: Json) => api<T>('PUT', p, b)
export const patch = <T = Json>(p: string, b?: Json) => api<T>('PATCH', p, b)
export const del = <T = Json>(p: string) => api<T>('DELETE', p)

/** URL for <audio>/wavesurfer: media elements cannot send headers, so the token rides in the query string. */
export function audioUrl(audioId: string, start?: number, end?: number): string {
  const q = new URLSearchParams({ token: useStore.getState().token || '' })
  if (start !== undefined) q.set('start', String(Math.max(0, start)))
  if (end !== undefined) q.set('end', String(end))
  return `/api/v1/audio/${audioId}/stream?${q}`
}

/** Subscribe to hub topics. `onEvent` always sees the latest closure. */
export function useTopics(topics: string[], onEvent: (e: Json) => void, presence?: { dataset: string; segment: string | null }) {
  const token = useStore(s => s.token)
  const cb = useRef(onEvent)
  cb.current = onEvent
  const wsRef = useRef<WebSocket | null>(null)
  const key = topics.join('|')
  useEffect(() => {
    if (!token || !topics.length) return
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(`${proto}://${location.host}/api/v1/ws?token=${token}`)
    wsRef.current = ws
    ws.onopen = () => topics.forEach(t => ws.send(JSON.stringify({ subscribe: t })))
    ws.onmessage = m => { try { cb.current(JSON.parse(m.data)) } catch { /* ignore */ } }
    return () => ws.close()
  }, [token, key])
  useEffect(() => {
    const ws = wsRef.current
    if (presence && ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ presence }))
  }, [presence?.dataset, presence?.segment])
}

export const fmt = (v: unknown, digits = 3): string => {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : v.toFixed(digits)
  return String(v)
}
export const pct = (v: number | null | undefined, d = 1) => (v === null || v === undefined ? '—' : `${(v * 100).toFixed(d)}%`)
export const mmss = (s: number) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`
