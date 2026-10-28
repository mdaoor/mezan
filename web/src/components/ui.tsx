import { createContext, ReactNode, useCallback, useContext, useEffect, useState } from 'react'
import Form from '@rjsf/core'
import validator from '@rjsf/validator-ajv8'
import { Json } from '../api'

// ---- toasts with 5 s undo (destructive actions use undo, not confirm dialogs)
type Toast = { id: number; text: string; undo?: () => void; tone?: 'ok' | 'err' }
const ToastCtx = createContext<{ push: (t: Omit<Toast, 'id'>) => void }>({ push: () => {} })
export const useToast = () => useContext(ToastCtx)
export function ToastHost({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([])
  const push = useCallback((t: Omit<Toast, 'id'>) => {
    const id = Date.now() + Math.random()
    setItems(x => [...x, { ...t, id }])
    setTimeout(() => setItems(x => x.filter(i => i.id !== id)), t.undo ? 5000 : 3500)
  }, [])
  return (
    <ToastCtx.Provider value={{ push }}>
      {children}
      <div className="fixed bottom-16 end-4 z-50 flex flex-col gap-2" role="status" aria-live="polite">
        {items.map(t => (
          <div key={t.id} className="panel px-3 py-2 text-sm shadow-lg flex gap-3 items-center" style={t.tone === 'err' ? { borderColor: 'var(--bad)' } : undefined}>
            <span>{t.text}</span>
            {t.undo && <button className="btn" onClick={() => { t.undo?.(); setItems(x => x.filter(i => i.id !== t.id)) }}>Undo</button>}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  )
}

export const Spinner = () => <span className="mut text-sm">Loading…</span>
export const ErrorBox = ({ error }: { error: unknown }) => error ? <div className="panel p-3 text-sm" style={{ color: 'var(--bad)' }}>{String((error as Error).message || error)}</div> : null

export function Tabs({ tabs, value, onChange }: { tabs: string[]; value: string; onChange: (t: string) => void }) {
  return (
    <div className="flex gap-1 border-b" style={{ borderColor: 'var(--line)' }} role="tablist">
      {tabs.map(t => (
        <button key={t} role="tab" aria-selected={t === value} onClick={() => onChange(t)}
          className="px-3 py-2 text-sm" style={{ borderBottom: t === value ? '2px solid var(--accent)' : '2px solid transparent', fontWeight: t === value ? 600 : 400 }}>{t}</button>
      ))}
    </div>
  )
}

export function Progress({ value }: { value: number }) {
  return <div className="h-2 rounded" style={{ background: 'var(--line)' }}><div className="h-2 rounded" style={{ width: `${Math.min(100, Math.max(0, value * 100))}%`, background: 'var(--accent)' }} /></div>
}

export function Ring({ value, size = 56, label }: { value: number; size?: number; label?: string }) {
  const r = size / 2 - 5, c = 2 * Math.PI * r
  return (
    <svg width={size} height={size} role="img" aria-label={label ?? `${Math.round(value * 100)}%`}>
      <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--line)" strokeWidth="5" />
      <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--accent)" strokeWidth="5" strokeDasharray={c} strokeDashoffset={c * (1 - Math.min(1, value))}
        transform={`rotate(-90 ${size / 2} ${size / 2})`} strokeLinecap="round" />
      <text x="50%" y="50%" dy=".35em" textAnchor="middle" fontSize="12" fill="var(--fg)">{Math.round(value * 100)}%</text>
    </svg>
  )
}

export function StatusBadge({ status }: { status: string }) {
  const color = ({ succeeded: 'var(--accent)', ready: 'var(--accent)', approved: 'var(--accent)', running: 'var(--warn)', failed: 'var(--bad)', cancelled: 'var(--mut)', paused: 'var(--warn)', ok: 'var(--accent)', error: 'var(--bad)' } as Json)[status] || 'var(--mut)'
  return <span className="chip" style={{ borderColor: color, color }}>{status}</span>
}

/** Renders a JSON-Schema-driven form (rjsf) so new engines/segmenters need no UI code (N1). */
export function SchemaForm({ schema, value, onChange }: { schema: Json; value: Json; onChange: (v: Json) => void }) {
  return (
    <div className="text-sm [&_input]:input [&_select]:input [&_textarea]:input [&_label]:text-xs [&_label]:mut [&_fieldset]:border-0 [&_fieldset]:p-0 [&_fieldset]:m-0 [&_.form-group]:mb-2">
      <Form schema={schema} formData={value} validator={validator} onChange={e => onChange(e.formData)} uiSchema={{ 'ui:submitButtonOptions': { norender: true } }} liveValidate={false} />
    </div>
  )
}

export function Drawer({ open, onClose, title, children }: { open: boolean; onClose: () => void; title: string; children: ReactNode }) {
  useEffect(() => { const h = (e: KeyboardEvent) => e.key === 'Escape' && onClose(); if (open) window.addEventListener('keydown', h); return () => window.removeEventListener('keydown', h) }, [open, onClose])
  if (!open) return null
  return (
    <div className="fixed inset-0 z-40 flex justify-end" style={{ background: 'rgba(0,0,0,.35)' }} onClick={onClose}>
      <aside className="panel h-full w-full max-w-lg overflow-auto p-4" onClick={e => e.stopPropagation()} role="dialog" aria-label={title}>
        <div className="flex justify-between items-center mb-3"><h2 className="text-lg font-semibold">{title}</h2><button className="btn" onClick={onClose}>Close</button></div>
        {children}
      </aside>
    </div>
  )
}

export function Empty({ title, steps, children }: { title: string; steps?: { label: string; done?: boolean }[]; children?: ReactNode }) {
  return (
    <div className="panel p-6 text-center">
      <div className="font-semibold mb-2">{title}</div>
      {steps && <ol className="inline-block text-start mb-3 text-sm">{steps.map((s, i) => <li key={i} className={s.done ? 'mut line-through' : ''}>{i + 1}. {s.label}</li>)}</ol>}
      <div>{children}</div>
    </div>
  )
}
