import { useEffect, useState } from 'react';
import { get } from '../api';
import { useToast } from '../components/Toast';
import { Empty, Loading } from '../components/ui';
import { money0 } from '../format';

const RANGES = [
  { id: '30d', label: '30d' },
  { id: '90d', label: '90d' },
  { id: '12m', label: '12 months' },
  { id: 'all', label: 'All time' },
];

// Stage segment colors — accent family, darkest first (earliest stage).
// Synthetic "No stage" / "Unassigned" rows — neutral, not part of the ramp.
const UNASSIGNED_COLOR = '#c4c7cf';
const STAGE_COLORS = ['#6d28d9', '#7c3aed', '#9d6ef0', '#b794f6', '#d0bcf9', '#e5d8fc'];

function compact(n) {
  n = Number(n) || 0;
  const abs = Math.abs(n);
  if (abs >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, '') + 'M';
  if (abs >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, '') + 'k';
  return String(Math.round(n));
}
/** Compact money for chart axes, in the report's own currency. */
function compactMoneyIn(currency) {
  let nf;
  try {
    nf = new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency: currency || 'USD',
      notation: 'compact',
      maximumFractionDigits: 1,
    });
  } catch {
    return (n) => `${currency} ${compact(n)}`;
  }
  return (n) => nf.format(Number(n) || 0);
}
const pct = (x) => (x === null || x === undefined ? '—' : Math.round(x * 100) + '%');

/** Inline failure state for one report section, with a retry affordance. */
function SectionError({ onRetry }) {
  return (
    <div className="state empty">
      <span>Couldn&apos;t load this report.</span>
      <button className="btn btn-small" onClick={onRetry}>
        Retry
      </button>
    </div>
  );
}

function Tiles({ tiles }) {
  return (
    <div className="report-tiles">
      {tiles.map((t) => (
        <div key={t.label} className="report-tile">
          <strong>{t.value}</strong>
          <span className="muted">{t.label}</span>
          {t.hint && <span className="muted report-tile-hint">{t.hint}</span>}
        </div>
      ))}
    </div>
  );
}

/** Amounts in currencies other than the report's — listed, never added in. */
function otherCurrencies(byCurrency, reportCurrency, field) {
  return Object.entries(byCurrency || {})
    .filter(([cur, c]) => cur !== reportCurrency && Number(c?.[field]))
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([cur, c]) => ({ cur, text: money0(c[field], cur) }));
}

/** A money cell: the report-currency figure, with any other currencies'
 * figures on their own lines underneath. */
function MoneyCell({ value, currency, byCurrency, field }) {
  const others = otherCurrencies(byCurrency, currency, field);
  return (
    <td className="num">
      {money0(value, currency)}
      {others.map((o) => (
        <div key={o.cur} className="muted report-othercur">
          + {o.text}
        </div>
      ))}
    </td>
  );
}

/** Shown when a report holds deals in more than one currency. `describe`
 * turns one by_currency entry into text. */
function MixedCurrencyNote({ currency, byCurrency, describe }) {
  const rows = Object.entries(byCurrency || {})
    .filter(([cur]) => cur !== currency)
    .sort(([a], [b]) => a.localeCompare(b));
  if (rows.length === 0) return null;
  return (
    <div className="report-mixed" role="note">
      <strong>Mixed currencies.</strong> Money figures and charts here cover {currency} deals only — other currencies
      are not converted or added in. Counts and rates include every deal.
      <ul>
        {rows.map(([cur, c]) => (
          <li key={cur}>
            <strong>{cur}</strong>: {describe(c, cur)}
          </li>
        ))}
      </ul>
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
  const cur = data.currency || 'USD';
  const plural = (n) => `${n} deal${n === 1 ? '' : 's'}`;
  return (
    <>
      <Tiles
        tiles={[
          { label: `Open value (${cur})`, value: money0(data.totals.open_value, cur) },
          { label: `Weighted forecast (${cur})`, value: money0(data.totals.weighted_forecast, cur) },
          { label: 'Open opportunities', value: data.totals.open_count },
        ]}
      />
      {data.mixed_currencies && (
        <MixedCurrencyNote
          currency={cur}
          byCurrency={data.totals.by_currency}
          describe={(c, code) =>
            `${money0(c.open_value, code)} open · ${money0(c.weighted_forecast, code)} weighted (${plural(c.open_count)})`
          }
        />
      )}
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
                    {money0(p.totals.open_value, cur)} open · {money0(p.totals.weighted_forecast, cur)}{' '}
                    weighted
                  </span>
                </div>
                {p.unassigned && (
                  <p className="muted report-footnote">
                    Open opportunities with no pipeline or stage. They count toward the totals above but appear on no
                    board — open them and pick a pipeline.
                  </p>
                )}
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
                              background: s.unassigned ? UNASSIGNED_COLOR : STAGE_COLORS[i % STAGE_COLORS.length],
                            }}
                            title={`${s.name}: ${money0(s.total_value, cur)} (${s.count})`}
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
                        <tr key={s.stage_id} className={s.unassigned ? 'report-unassigned' : undefined}>
                          <td>
                            <i
                              className="legend-swatch"
                              style={{
                                background: s.unassigned ? UNASSIGNED_COLOR : STAGE_COLORS[i % STAGE_COLORS.length],
                              }}
                            />
                            {s.name}
                          </td>
                          {/* A synthetic row has no stage probability — the
                              figure is the deals' own effective average. */}
                          <td className="num" title={s.unassigned ? 'Average of these deals’ own win probabilities' : undefined}>
                            {s.unassigned ? `~${s.win_probability}%` : `${s.win_probability}%`}
                          </td>
                          <td className="num">{s.count}</td>
                          <MoneyCell value={s.total_value} currency={cur} byCurrency={s.by_currency} field="total_value" />
                          <MoneyCell value={s.weighted_value} currency={cur} byCurrency={s.by_currency} field="weighted_value" />
                        </tr>
                      ))}
                      <tr className="report-total">
                        <td>Total</td>
                        <td className="num" />
                        <td className="num">{p.totals.open_count}</td>
                        <MoneyCell value={p.totals.open_value} currency={cur} byCurrency={p.totals.by_currency} field="open_value" />
                        <MoneyCell
                          value={p.totals.weighted_forecast}
                          currency={cur}
                          byCurrency={p.totals.by_currency}
                          field="weighted_forecast"
                        />
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
  const cur = data.currency || 'USD';
  return (
    <>
      <Tiles
        tiles={[
          { label: 'Win rate', value: pct(s.win_rate) },
          { label: `Won value (${cur})`, value: money0(s.won_value, cur) },
          { label: 'Deals won', value: s.won_count },
          {
            label: `Avg deal size (${cur})`,
            value: s.avg_deal_size === null ? '—' : money0(s.avg_deal_size, cur),
          },
          {
            label: 'Avg days to close',
            value: s.avg_days_to_close === null ? '—' : s.avg_days_to_close,
          },
        ]}
      />
      {data.mixed_currencies && (
        <MixedCurrencyNote
          currency={cur}
          byCurrency={data.by_currency}
          describe={(c, code) =>
            `${money0(c.won_value, code)} won (${c.won_count}) · ${money0(c.lost_value, code)} lost (${c.lost_count})`
          }
        />
      )}
      <BarChart
        series={data.series}
        fmt={compactMoneyIn(cur)}
        bars={[
          { key: 'won_value', label: 'Won', color: 'var(--success)' },
          { key: 'lost_value', label: 'Lost', color: '#fca5a5' },
        ]}
      />
      <Legend
        items={[
          { label: `Won (${s.won_count})`, color: 'var(--success)' },
          { label: `Lost (${s.lost_count} · ${money0(s.lost_value, cur)})`, color: '#fca5a5' },
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
  // Two different questions, two different numbers:
  //  - flow: conversions that HAPPENED in the period (whenever the lead was created)
  //  - cohort: of the leads CREATED in the period, how many have converted so far
  // Only the cohort figure is a rate (it can't pass 100%).
  const convertedInPeriod = s.converted_in_period ?? s.converted ?? 0;
  const cohortConverted = s.cohort_converted ?? 0;
  if (!s.new_leads && !convertedInPeriod) {
    return <Empty label="No leads in this range yet." />;
  }
  return (
    <>
      <Tiles
        tiles={[
          { label: 'New leads', value: s.new_leads },
          { label: 'Conversions in period', value: convertedInPeriod, hint: 'any lead, whenever created' },
          {
            label: 'New-lead conversion rate',
            value: pct(s.conversion_rate),
            hint: `${cohortConverted} of ${s.new_leads} new leads converted so far`,
          },
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
          { key: 'converted_count', label: 'Conversions', color: 'var(--success)' },
        ]}
      />
      <Legend
        items={[
          { label: 'New leads', color: 'var(--accent)' },
          { label: 'Conversions in period', color: 'var(--success)' },
        ]}
      />
      {data.by_source.length > 0 && (
        <div className="table-wrap report-source-table">
          <table className="report-table">
            <thead>
              <tr>
                <th>Source</th>
                <th className="num">New</th>
                <th className="num">Conversions in period</th>
                <th className="num">Of new, converted</th>
                <th className="num">New-lead conversion</th>
              </tr>
            </thead>
            <tbody>
              {data.by_source.map((row) => (
                <tr key={row.source}>
                  <td>{row.source}</td>
                  <td className="num">{row.new_count}</td>
                  <td className="num">{row.converted_count}</td>
                  <td className="num">{row.cohort_converted ?? '—'}</td>
                  <td className="num">{pct(row.conversion_rate)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="muted report-footnote">
        The conversion rate follows the leads created in this range: how many of them have converted so far. It is
        separate from &ldquo;Conversions in period&rdquo;, which counts every conversion that happened in the range —
        including leads created earlier.
      </p>
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
  const [pipelineVersion, setPipelineVersion] = useState(0);
  const [rangeVersion, setRangeVersion] = useState(0);

  useEffect(() => {
    let on = true;
    setPipeline(null);
    get('/reports/pipeline')
      .then((d) => on && setPipeline(d))
      .catch((e) => {
        toast.error(e.message);
        // Error sentinel — a permanent spinner would otherwise sit here.
        if (on) setPipeline({ error: true });
      });
    return () => {
      on = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pipelineVersion]);

  useEffect(() => {
    let on = true;
    setSales(null);
    setActivity(null);
    setLeads(null);
    const load = (path, set) =>
      get(path, { range })
        .then((d) => on && set(d))
        .catch((e) => {
          toast.error(e.message);
          if (on) set({ error: true });
        });
    load('/reports/sales', setSales);
    load('/reports/activity', setActivity);
    load('/reports/leads', setLeads);
    return () => {
      on = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [range, rangeVersion]);

  const retryRange = () => setRangeVersion((v) => v + 1);

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
        {pipeline?.error ? (
          <SectionError onRetry={() => setPipelineVersion((v) => v + 1)} />
        ) : (
          <PipelineSection data={pipeline} />
        )}
      </div>

      <div className="card report-section">
        <h2>Sales</h2>
        {sales?.error ? <SectionError onRetry={retryRange} /> : <SalesSection data={sales} />}
      </div>

      <div className="card report-section">
        <h2>Activity</h2>
        {activity?.error ? <SectionError onRetry={retryRange} /> : <ActivitySection data={activity} />}
      </div>

      <div className="card report-section">
        <h2>Leads funnel</h2>
        {leads?.error ? <SectionError onRetry={retryRange} /> : <LeadsSection data={leads} />}
      </div>
    </div>
  );
}
