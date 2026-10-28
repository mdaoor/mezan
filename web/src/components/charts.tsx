import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis, Bar, BarChart } from 'recharts'
import { Json } from '../api'

const COLORS = ['#4e79a7', '#f28e2b', '#59a14f', '#e15759', '#76b7b2', '#edc948', '#b07aa1', '#9c755f']

export function LineSeries({ data, xKey, lines, height = 240, yLabel }: { data: Json[]; xKey: string; lines: { key: string; name?: string; yAxisId?: string }[]; height?: number; yLabel?: string }) {
  const right = lines.some(l => l.yAxisId === 'r')
  return (
    <div style={{ height }} role="img" aria-label={yLabel || 'chart'}>
      <ResponsiveContainer>
        <LineChart data={data}>
          <CartesianGrid stroke="var(--line)" strokeDasharray="3 3" />
          <XAxis dataKey={xKey} stroke="var(--mut)" fontSize={12} />
          <YAxis yAxisId="l" stroke="var(--mut)" fontSize={12} label={yLabel ? { value: yLabel, angle: -90, position: 'insideLeft', fill: 'var(--mut)', fontSize: 11 } : undefined} />
          {right && <YAxis yAxisId="r" orientation="right" stroke="var(--mut)" fontSize={12} />}
          <Tooltip contentStyle={{ background: 'var(--panel)', border: '1px solid var(--line)' }} />
          <Legend />
          {lines.map((l, i) => <Line key={l.key} yAxisId={l.yAxisId || 'l'} type="monotone" dataKey={l.key} name={l.name || l.key} stroke={COLORS[i % 8]} strokeWidth={2} dot={{ r: 3 }} connectNulls />)}
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}

export function Bars({ data, xKey, bars, height = 240 }: { data: Json[]; xKey: string; bars: { key: string; name?: string }[]; height?: number }) {
  return (
    <div style={{ height }}>
      <ResponsiveContainer>
        <BarChart data={data}>
          <CartesianGrid stroke="var(--line)" strokeDasharray="3 3" />
          <XAxis dataKey={xKey} stroke="var(--mut)" fontSize={12} />
          <YAxis stroke="var(--mut)" fontSize={12} />
          <Tooltip contentStyle={{ background: 'var(--panel)', border: '1px solid var(--line)' }} />
          <Legend />
          {bars.map((b, i) => <Bar key={b.key} dataKey={b.key} name={b.name || b.key} fill={COLORS[i % 8]} />)}
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

export function Spark({ values, height = 36, width = 140 }: { values: number[]; height?: number; width?: number }) {
  if (values.length < 2) return <span className="mut text-xs">—</span>
  const mx = Math.max(...values), mn = Math.min(...values)
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * width},${height - ((v - mn) / (mx - mn || 1)) * (height - 4) - 2}`).join(' ')
  return <svg width={width} height={height} role="img" aria-label="p95 latency trend"><polyline fill="none" stroke="var(--accent)" strokeWidth="2" points={pts} /></svg>
}
