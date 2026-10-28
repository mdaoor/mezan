import { Json } from '../api'

const LAT = /[A-Za-zÀ-ɏ]/
const AR = /[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]/

/** Character-level LCS: which characters of `text` differ from `base` (for the faint "changed since machine draft" underline). */
export function changedMask(base: string, text: string): boolean[] {
  const a = Array.from(base), b = Array.from(text)
  if (a.length * b.length > 400_000) return b.map(() => false)
  const dp: number[][] = Array.from({ length: a.length + 1 }, () => new Array(b.length + 1).fill(0))
  for (let i = a.length - 1; i >= 0; i--) for (let j = b.length - 1; j >= 0; j--)
    dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1])
  const keep = new Array(b.length).fill(false)
  let i = 0, j = 0
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) { keep[j] = true; i++; j++ } else if (dp[i + 1][j] >= dp[i][j + 1]) i++; else j++
  }
  return keep.map(k => !k)
}

/** Text with Latin runs tinted and (optionally) characters changed vs a base underlined. Never alters the stored text. */
export function RichText({ text, base, className = '' }: { text: string; base?: string | null; className?: string }) {
  const chars = Array.from(text)
  const mask = base != null ? changedMask(base, text) : null
  const out: Json[] = []
  let run = '', key = 0, cls = ''
  const flush = () => { if (run) out.push(<span key={key++} className={cls}>{run}</span>); run = '' }
  chars.forEach((ch, i) => {
    const c = [LAT.test(ch) ? 'latin' : AR.test(ch) ? '' : '', mask?.[i] ? 'changed' : ''].join(' ').trim()
    if (c !== cls) { flush(); cls = c }
    run += ch
  })
  flush()
  return <span className={`bidi ${className}`} dir="auto">{out}{text.endsWith('\n') ? '​' : ''}</span>
}

export function DiffTokens({ diff }: { diff: { op: string; ref: string | null; hyp: string | null }[] }) {
  return (
    <span className="bidi" dir="auto">
      {diff.map((d, i) => d.op === 'equal' ? <span key={i}>{d.hyp} </span>
        : d.op === 'sub' ? <span key={i} className="diff-sub" title={`ref: ${d.ref}`}>{d.hyp} </span>
        : d.op === 'ins' ? <span key={i} className="diff-ins" title="inserted">{d.hyp} </span>
        : <span key={i} className="diff-del" title="deleted">{d.ref} </span>)}
    </span>
  )
}
