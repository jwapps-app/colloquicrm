import { useEffect, useState } from 'react';
import { get } from '../api';
import { useToast } from '../components/Toast';
import { Empty, Loading } from '../components/ui';

const RANGES = [
  { id: '30d', label: '30d' },
  { id: '90d', label: '90d' },
  { id: '12m', label: '12 months' },
  { id: 'all', label: 'All time' },
];

// Stage segment colors — accent family, darkest first (earliest stage).
const STAGE_COLORS = ['#6d28d9', '#7c3aed', '#9d6ef0', '#b794f6', '#d0bcf9', '#e5d8fc'];

const money0 = (v) =>
  new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    maximumFractionDigits: 0,
  }).format(Number(v) || 0);

function compact(n) {
  n = Number(n) || 0;
  const abs = Math.abs(n);
  if (abs >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, '') + 'M';
  if (abs >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, '') + 'k';
  return String(Math.round(n));
}
const compactMoney = (n) => '$' + compact(n);
const pct = (x) => (x === null || x === undefined ? '—' : Math.round(x * 100) + '%');

function Tiles({ tiles }) {
  return (
    <div className="report-tiles">
      {tiles.map((t) => (
        <div key={t.label} className="report-tile">
          <strong>{t.value}</strong>
          <span className="muted">{t.label}</span>
        </div>
      ))}
    </div>
  );
}

function Legend({ items }) {
  return (
    <div className="report-legend">
      {items.map((it) => (
        <span key={it.label}>
          <i className="legend-swatch" style={{ background: it.color }} />
          {it.label}
        </span>
      ))}
    </div>
  );
}

/** Hand-rolled grouped bar chart. series: [{bucket_label, ...values}]. */
function BarChart({ series, bars, fmt = compact, height = 190 }) {
  const W = 720;
  const padL = 48;
  const padR = 6;
  const padT = 10;
  const padB = 22;
  const innerW = W - padL - padR;
  const innerH = height - padT - padB;
  const max = Math.max(1, ...series.flatMap((s) => bars.map((b) => Number(s[b.key]) || 0)));
  const n = Math.max(1, series.length);
  const group = innerW / n;
  const barW = Math.max(3, Math.min(26, (group * 0.72) / bars.length));
  const labelEvery = Math.ceil(series.length / 12);
  return (
    <svg className="report-chart" viewBox={`0 0 ${W} ${height}`} role="img">
      {[0, 0.5, 1].map((t) => {
        const y = padT + innerH * (1 - t);
        return (
          <g key={t}>
            <line x1={padL} x2={W - padR} y1={y} y2={y} stroke="var(--border)" strokeWidth="1" />
            <text x={padL - 6} y={y + 3.5} textAnchor="end" fontSize="10" fill="var(--muted)">
              {fmt(max * t)}
            </text>
          </g>
        );
      })}
      {series.map((s, i) => {
        const x0 = padL + group * i + (group - barW * bars.length) / 2;
        return (
          <g key={s.bucket_label + i}>
            {bars.map((b, j) => {
              const v = Number(s[b.key]) || 0;
              const h = (v / max) * innerH;
              return (
                <rect
                  key={b.key}
                  x={x0 + j * barW}
                  y={padT + innerH - Math.max(h, v > 0 ? 2 : 0)}
                  width={Math.max(1, barW - 2)}
                  height={Math.max(h, v > 0 ? 2 : 0)}
                  rx="2"
                  fill={b.color}
                >
                  <title>{`${s.bucket_label} — ${b.label}: ${fmt(v)}`}</title>
                </rect>
              );
            })}
            {i % labelEvery === 0 && (
              <text
                x={padL + group * i + group / 2}
                y={height - 6}
                textAnchor="middle"
                fontSize="10"
                fill="var(--muted)"
              >
                {s.bucket_label}
              </text>
            )}
          </g>
        );
      })}
    </svg>
  );
}

function PipelineSection({ data }) {
  if (data === null) return <Loading small label="Loading pipeline…" />;
  const pipelines = data.pipelines || [];
  const withOpps = pipelines.some((p) => p.totals.open_count > 0);
  return (
    <>
      <Tiles
        tiles={[
          { label: 'Open value', value: money0(data.totals.open_value) },
          { label: 'Weighted forecast', value: money0(data.totals.weighted_forecast) },
          { label: 'Open opportunities', value: data.totals.open_count },
        ]}
      />
      {!withOpps ? (
        <Empty label="No open opportunities yet." hint="Open deals will show up here by stage." />
      ) : (
        pipelines
          .filter((p) => p.stages.length > 0)
          .map((p) => {
            const barTotal = p.stages.reduce((a, s) => a + s.total_value, 0);
            return (
              <div key={p.id} className="report-pipeline">
                <div className="report-subhead">
                  <h3>{p.name}</h3>
                  <span className="muted">
                    {money0(p.totals.open_value)} open · {money0(p.totals.weighted_forecast)}{' '}
                    weighted
                  </span>
                </div>
                {barTotal > 0 && (
                  <div className="stack-bar">
                    {p.stages.map(
                      (s, i) =>
                        s.total_value > 0 && (
                          <div
                            key={s.stage_id}
                            className="stack-seg"
                            style={{
                              flexGrow: s.total_value,
                              background: STAGE_COLORS[i % STAGE_COLORS.length],
                            }}
                            title={`${s.name}: ${money0(s.total_value)} (${s.count})`}
                          />
                        )
                    )}
                  </div>
                )}
                <div className="table-wrap">
                  <table className="report-table">
                    <thead>
                      <tr>
                        <th>Stage</th>
                        <th className="num">Win %</th>
                        <th className="num">Deals</th>
                        <th className="num">Value</th>
                        <th className="num">Weighted</th>
                      </tr>
                    </thead>
                    <tbody>
                      {p.stages.map((s, i) => (
                        <tr key={s.stage_id}>
                          <td>
                            <i
                              className="legend-swatch"
                              style={{ background: STAGE_COLORS[i % STAGE_COLORS.length] }}
                            />
                            {s.name}
                          </td>
                          <td className="num">{s.win_probability}%</td>
                          <td className="num">{s.count}</td>
                          <td className="num">{money0(s.total_value)}</td>
                          <td className="num">{money0(s.weighted_value)}</td>
                        </tr>
                      ))}
                      <tr className="report-total">
                        <td>Total</td>
                        <td className="num" />
                        <td className="num">{p.totals.open_count}</td>
                        <td className="num">{money0(p.totals.open_value)}</td>
                        <td className="num">{money0(p.totals.weighted_forecast)}</td>
                      </tr>
                    </tbody>
                  </table>
                </div>
              </div>
            );
          })
      )}
    </>
  );
}

function SalesSection({ data }) {
  if (data === null) return <Loading small label="Loading sales…" />;
  const s = data.summary;
  if (!s.won_count && !s.lost_count) {
    return <Empty label="No won or lost opportunities in this range yet." />;
  }
  return (
    <>
      <Tiles
        tiles={[
          { label: 'Win rate', value: pct(s.win_rate) },
          { label: 'Won value', value: money0(s.won_value) },
          { label: 'Deals won', value: s.won_count },
          { label: 'Avg deal size', value: s.avg_deal_size === null ? '—' : money0(s.avg_deal_size) },
          {
            label: 'Avg days to close',
            value: s.avg_days_to_close === null ? '—' : s.avg_days_to_close,
          },
        ]}
      />
      <BarChart
        series={data.series}
        fmt={compactMoney}
        bars={[
          { key: 'won_value', label: 'Won', color: 'var(--success)' },
          { key: 'lost_value', label: 'Lost', color: '#fca5a5' },
        ]}
      />
      <Legend
        items={[
          { label: `Won (${s.won_count})`, color: 'var(--success)' },
          { label: `Lost (${s.lost_count} · ${money0(s.lost_value)})`, color: '#fca5a5' },
        ]}
      />
    </>
  );
}

const ACTIVITY_METRICS = [
  ['emails_sent', 'Emails out'],
  ['emails_received', 'Emails in'],
  ['calls', 'Calls'],
  ['texts', 'Texts'],
  ['notes', 'Notes'],
  ['tasks_completed', 'Tasks done'],
];

function MetricCell({ value, max }) {
  return (
    <td className="num">
      <span className="metric-cell">
        <span className="metric-bar">
          <i style={{ width: max ? `${Math.round((value / max) * 100)}%` : 0 }} />
        </span>
        {value}
      </span>
    </td>
  );
}

function ActivitySection({ data }) {
  if (data === null) return <Loading small label="Loading activity…" />;
  const rows = data.rows || [];
  if (!rows.some((r) => r.total > 0)) {
    return <Empty label="No activity in this range yet." hint="Emails, calls, notes, and completed tasks count here." />;
  }
  const maxes = {};
  ACTIVITY_METRICS.forEach(([key]) => {
    maxes[key] = Math.max(...rows.map((r) => r[key] || 0));
  });
  return (
    <>
      <div className="table-wrap">
        <table className="report-table">
          <thead>
            <tr>
              <th>User</th>
              {ACTIVITY_METRICS.map(([key, label]) => (
                <th key={key} className="num">
                  {label}
                </th>
              ))}
              <th className="num">Total</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.user_id || 'team'} className={r.user_id ? '' : 'muted'}>
                <td>{r.display_name}</td>
                {ACTIVITY_METRICS.map(([key]) => (
                  <MetricCell key={key} value={r[key] || 0} max={maxes[key]} />
                ))}
                <td className="num">
                  <strong>{r.total}</strong>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="muted report-footnote">{data.attribution_note}</p>
    </>
  );
}

function LeadsSection({ data }) {
  if (data === null) return <Loading small label="Loading leads…" />;
  const s = data.summary;
  if (!s.new_leads && !s.converted) {
    return <Empty label="No leads in this range yet." />;
  }
  return (
    <>
      <Tiles
        tiles={[
          { label: 'New leads', value: s.new_leads },
          { label: 'Converted', value: s.converted },
          { label: 'Conversion rate', value: pct(s.conversion_rate) },
          {
            label: 'Avg days to convert',
            value: s.avg_days_to_convert === null ? '—' : s.avg_days_to_convert,
          },
        ]}
      />
      <BarChart
        series={data.series}
        bars={[
          { key: 'new_count', label: 'New', color: 'var(--accent)' },
          { key: 'converted_count', label: 'Converted', color: 'var(--success)' },
        ]}
      />
      <Legend
        items={[
          { label: 'New leads', color: 'var(--accent)' },
          { label: 'Converted', color: 'var(--success)' },
        ]}
      />
      {data.by_source.length > 0 && (
        <div className="table-wrap report-source-table">
          <table className="report-table">
            <thead>
              <tr>
                <th>Source</th>
                <th className="num">New</th>
                <th className="num">Converted</th>
                <th className="num">Conversion</th>
              </tr>
            </thead>
            <tbody>
              {data.by_source.map((row) => (
                <tr key={row.source}>
                  <td>{row.source}</td>
                  <td className="num">{row.new_count}</td>
                  <td className="num">{row.converted_count}</td>
                  <td className="num">{pct(row.conversion_rate)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

export default function Reports() {
  const toast = useToast();
  const [range, setRange] = useState('90d');
  const [pipeline, setPipeline] = useState(null);
  const [sales, setSales] = useState(null);
  const [activity, setActivity] = useState(null);
  const [leads, setLeads] = useState(null);

  useEffect(() => {
    let on = true;
    get('/reports/pipeline')
      .then((d) => on && setPipeline(d))
      .catch((e) => toast.error(e.message));
    return () => {
      on = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    let on = true;
    setSales(null);
    setActivity(null);
    setLeads(null);
    const load = (path, set) =>
      get(path, { range })
        .then((d) => on && set(d))
        .catch((e) => toast.error(e.message));
    load('/reports/sales', setSales);
    load('/reports/activity', setActivity);
    load('/reports/leads', setLeads);
    return () => {
      on = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [range]);

  return (
    <div className="page">
      <div className="page-head">
        <h1>Reports</h1>
        <div className="page-head-actions">
          <div className="seg-toggle">
            {RANGES.map((r) => (
              <button
                key={r.id}
                className={range === r.id ? 'active' : ''}
                onClick={() => setRange(r.id)}
              >
                {r.label}
              </button>
            ))}
          </div>
        </div>
      </div>

      <div className="card report-section">
        <div className="report-head">
          <h2>Pipeline</h2>
          <span className="muted">Live snapshot</span>
        </div>
        <PipelineSection data={pipeline} />
      </div>

      <div className="card report-section">
        <h2>Sales</h2>
        <SalesSection data={sales} />
      </div>

      <div className="card report-section">
        <h2>Activity</h2>
        <ActivitySection data={activity} />
      </div>

      <div className="card report-section">
        <h2>Leads funnel</h2>
        <LeadsSection data={leads} />
      </div>
    </div>
  );
}
