import { useCallback, useEffect, useState } from 'react';
import { api, money, pct, reasonLabel } from '../api';

function Kpi({ label, value, hint, tone = 'text-slate-100' }) {
  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-4">
      <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">{label}</div>
      <div className={`mt-1 text-2xl font-semibold ${tone}`}>{value}</div>
      {hint ? <div className="mt-1 text-xs text-slate-500">{hint}</div> : null}
    </div>
  );
}

export default function Metrics() {
  const [metrics, setMetrics] = useState(null);
  const [config, setConfig] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const [metricsData, configData] = await Promise.all([api.get('/api/metrics'), api.get('/api/threeway/config')]);
      setMetrics(metricsData);
      setConfig(configData);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (!metrics) {
    return (
      <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-6 text-sm text-slate-400">
        {loading ? 'Loading metrics…' : error || 'No metrics yet.'}
      </div>
    );
  }

  const reasons = Object.entries(metrics.exception_counts_by_reason || {});
  const maxReason = Math.max(1, ...reasons.map(([, count]) => count));

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3 rounded-2xl border border-slate-800 bg-slate-900/60 p-4">
        <div>
          <h2 className="text-lg font-semibold text-slate-100">Metrics</h2>
          <p className="text-sm text-slate-400">The numbers behind “invoices processed in minutes with X% touchless”.</p>
        </div>
        <button onClick={load} className="rounded-lg border border-slate-700 px-3 py-2 text-xs font-semibold text-slate-300 hover:bg-slate-800">
          {loading ? 'Refreshing…' : 'Refresh'}
        </button>
      </div>

      {error ? <p className="rounded-lg border border-rose-900 bg-rose-950/40 p-3 text-sm text-rose-300">{error}</p> : null}

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Kpi
          label="Straight-through rate"
          value={pct(metrics.straight_through_rate)}
          hint={`${metrics.touchless_invoices} of ${metrics.total_invoices} invoices needed no touch`}
          tone="text-emerald-300"
        />
        <Kpi
          label="Exception rate"
          value={pct(metrics.exception_rate)}
          hint={`${metrics.exceptions} exception(s), ${metrics.rejects} rejected`}
          tone="text-amber-300"
        />
        <Kpi label="Duplicates caught" value={metrics.duplicates_caught} hint="vendor + number + amount + date" tone="text-rose-300" />
        <Kpi
          label="Avg cycle time"
          value={`${metrics.avg_cycle_time_hours}h`}
          hint={`max ${metrics.max_cycle_time_hours}h · match ${metrics.avg_match_duration_ms}ms`}
        />
        <Kpi label="Invoices processed" value={metrics.total_invoices} hint={`${metrics.matched} matched`} />
        <Kpi label="Value processed" value={money(metrics.total_value_processed)} />
        <Kpi label="Pending approvals" value={metrics.approvals_pending} hint={`${metrics.approved} approved`} />
        <Kpi
          label="Auto-approve threshold"
          value={config ? config.escalation.auto_accept : '—'}
          hint={config ? `template match ≥ ${config.escalation.template_match} · review floor ${config.escalation.review_floor}` : ''}
        />
      </div>

      {metrics.pipeline ? (
        <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-5">
          <h3 className="text-sm font-semibold text-slate-200">Pipeline health</h3>
          <p className="text-xs text-slate-500">
            Template hit rate, AI fallback share and validation failure rate — the three numbers that show whether
            extraction is getting better or just busier.
          </p>
          <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <Kpi label="Template hit rate" value={pct(metrics.pipeline.template_hit_rate)} hint={`${metrics.pipeline.template_hits} of ${metrics.pipeline.documents_extracted} documents`} />
            <Kpi label="AI fallback share" value={pct(metrics.pipeline.ai_fallback_share)} hint={`${metrics.pipeline.ai_fallbacks} AI extraction(s)`} tone="text-amber-300" />
            <Kpi label="Validation failure rate" value={pct(metrics.pipeline.validation_failure_rate)} hint={`${metrics.pipeline.validation_failures} document(s) held`} tone="text-rose-300" />
            <Kpi
              label="Templates"
              value={`${metrics.pipeline.templates.trusted} trusted`}
              hint={`${metrics.pipeline.templates.candidates} candidate · ${metrics.pipeline.templates.quarantined} quarantined`}
            />
          </div>
          {metrics.decisions ? (
            <div className="mt-4 grid gap-3 sm:grid-cols-3">
              <Kpi label="Approved by the system" value={metrics.decisions.APPROVE || 0} tone="text-emerald-300" />
              <Kpi label="Sent to review" value={metrics.decisions.REVIEW || 0} hint={pct(metrics.review_rate)} tone="text-amber-300" />
              <Kpi label="Rejected" value={metrics.decisions.REJECT || 0} tone="text-rose-300" />
            </div>
          ) : null}
        </div>
      ) : null}

      <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-5">
        <h3 className="text-sm font-semibold text-slate-200">Exception reasons</h3>
        <p className="text-xs text-slate-500">Counts per reason code — this is the tuning list for tolerances and vendor rules.</p>
        <div className="mt-4 space-y-3">
          {reasons.length === 0 ? (
            <p className="text-sm text-slate-500">No exceptions recorded yet.</p>
          ) : (
            reasons.map(([code, count]) => (
              <div key={code}>
                <div className="flex items-center justify-between text-xs">
                  <span className="font-medium text-slate-300">{reasonLabel(code)}</span>
                  <span className="text-slate-500">
                    {count} · {pct(metrics.exception_rate_by_reason[code])}
                  </span>
                </div>
                <div className="mt-1 h-2 w-full overflow-hidden rounded-full bg-slate-800">
                  <div className="h-full rounded-full bg-amber-500" style={{ width: `${(count / maxReason) * 100}%` }} />
                </div>
              </div>
            ))
          )}
        </div>
      </div>

      {config ? (
        <div className="grid gap-4 lg:grid-cols-2">
          <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-5">
            <h3 className="text-sm font-semibold text-slate-200">Match tolerances</h3>
            <dl className="mt-3 grid grid-cols-2 gap-2 text-xs text-slate-400">
              {Object.entries(config.tolerances).map(([key, value]) => (
                <div key={key} className="flex items-center justify-between rounded-lg bg-slate-950/50 px-3 py-2">
                  <dt>{reasonLabel(key)}</dt>
                  <dd className="font-mono text-slate-200">{value}</dd>
                </div>
              ))}
              <div className="flex items-center justify-between rounded-lg bg-slate-950/50 px-3 py-2">
                <dt>duplicate window</dt>
                <dd className="font-mono text-slate-200">{config.match.duplicate_window_days}d</dd>
              </div>
              <div className="flex items-center justify-between rounded-lg bg-slate-950/50 px-3 py-2">
                <dt>require GRN</dt>
                <dd className="font-mono text-slate-200">{String(config.match.require_grn)}</dd>
              </div>
            </dl>
          </div>
          <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-5">
            <h3 className="text-sm font-semibold text-slate-200">Validation &amp; trust policy</h3>
            <dl className="mt-3 space-y-2 text-xs text-slate-400">
              {config.validation ? (
                <>
                  <div className="flex items-center justify-between rounded-lg bg-slate-950/50 px-3 py-2">
                    <dt>GSTIN checksum</dt>
                    <dd className="font-mono text-slate-200">{config.validation.gstin_checksum ? 'enforced' : 'off'}</dd>
                  </div>
                  <div className="flex items-center justify-between rounded-lg bg-slate-950/50 px-3 py-2">
                    <dt>line / tax / total slack</dt>
                    <dd className="font-mono text-slate-200">
                      {config.validation.line_sum_abs} / {config.validation.tax_recompute_abs} / {config.validation.grand_total_abs}
                    </dd>
                  </div>
                </>
              ) : null}
              {config.template_trust ? (
                <div className="flex items-center justify-between rounded-lg bg-slate-950/50 px-3 py-2">
                  <dt>confirmations before trusted</dt>
                  <dd className="font-mono text-slate-200">{config.template_trust.candidate_confirmations}</dd>
                </div>
              ) : null}
              {config.shadow ? (
                <div className="flex items-center justify-between rounded-lg bg-slate-950/50 px-3 py-2">
                  <dt>decision mode</dt>
                  <dd className="font-mono text-slate-200">{config.shadow.mode}</dd>
                </div>
              ) : null}
              {config.shadow ? (
                <div className="flex items-center justify-between rounded-lg bg-slate-950/50 px-3 py-2">
                  <dt>auto-approve at</dt>
                  <dd className="font-mono text-slate-200">
                    {config.shadow.min_decisions} decisions / {Math.round(config.shadow.min_agreement * 100)}%
                  </dd>
                </div>
              ) : null}
            </dl>
          </div>
          <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-5">
            <h3 className="text-sm font-semibold text-slate-200">Approval rules</h3>
            <ul className="mt-3 space-y-2 text-xs text-slate-400">
              {config.approval_rules.map((rule) => (
                <li key={rule.name} className="rounded-lg bg-slate-950/50 px-3 py-2">
                  <span className="font-semibold text-slate-200">{rule.name}</span> · {money(rule.min_amount)} –{' '}
                  {rule.max_amount ? money(rule.max_amount) : '∞'} · roles: {(rule.roles || []).join(', ')}
                </li>
              ))}
            </ul>
          </div>
        </div>
      ) : null}
    </div>
  );
}
