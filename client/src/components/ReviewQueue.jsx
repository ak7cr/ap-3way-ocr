import { useCallback, useEffect, useState } from 'react';
import { api, money, reasonLabel } from '../api';
import { Badge, SideBySide, STATUS_STYLES } from './MatchDetail';

const DECISION_STYLES = {
  APPROVE: 'bg-emerald-950/60 text-emerald-300 border-emerald-800',
  REVIEW: 'bg-amber-950/60 text-amber-300 border-amber-800',
  REJECT: 'bg-rose-950/60 text-rose-300 border-rose-900',
};

const CHECK_STYLES = {
  PASS: 'text-emerald-300',
  WARN: 'text-amber-300',
  FAIL: 'text-rose-300',
};

function CheckList({ validation }) {
  const checks = validation?.checks || [];
  if (!checks.length) return <p className="text-xs text-slate-500">No validation checks recorded.</p>;
  return (
    <ul className="mt-2 space-y-1">
      {checks.map((check) => (
        <li key={`${check.code}-${check.message}`} className="flex gap-2 text-xs">
          <span className={`w-10 shrink-0 font-mono ${CHECK_STYLES[check.status] || 'text-slate-400'}`}>{check.status}</span>
          <span className="text-slate-400">
            <span className="font-medium text-slate-300">{reasonLabel(check.code)}</span> — {check.message}
          </span>
        </li>
      ))}
    </ul>
  );
}

function ProposalList({ items, onConfirm, busy }) {
  if (!items.length) return <p className="text-sm text-slate-500">No layouts waiting to be confirmed.</p>;
  return (
    <ul className="space-y-2">
      {items.map((proposal) => (
        <li key={proposal.extraction_log_id} className="rounded-lg border border-slate-800 bg-slate-950/40 p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <div className="text-sm font-semibold text-slate-100">
                {proposal.vendor || 'Unknown vendor'} · {proposal.invoice_number || 'no number'}
              </div>
              <div className="text-xs text-slate-500">
                AI read this layout. Confirming makes it a candidate template; it is trusted only after repeated clean runs.
              </div>
            </div>
            <div className="flex gap-2">
              <button
                disabled={busy}
                onClick={() => onConfirm(proposal.extraction_log_id, true)}
                className="rounded-lg bg-cyan-400 px-3 py-1.5 text-xs font-semibold text-slate-950 hover:bg-cyan-300 disabled:opacity-40"
              >
                Confirm layout
              </button>
              <button
                disabled={busy}
                onClick={() => onConfirm(proposal.extraction_log_id, false)}
                className="rounded-lg border border-slate-700 px-3 py-1.5 text-xs font-semibold text-slate-300 hover:bg-slate-800 disabled:opacity-40"
              >
                Not this layout
              </button>
            </div>
          </div>
        </li>
      ))}
    </ul>
  );
}

export default function ReviewQueue() {
  const [queue, setQueue] = useState([]);
  const [selected, setSelected] = useState(null);
  const [detail, setDetail] = useState(null);
  const [templates, setTemplates] = useState({ templates: [], proposals: [] });
  const [shadow, setShadow] = useState(null);
  const [actor, setActor] = useState('accountant');
  const [note, setNote] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const [queueData, templateData, shadowData] = await Promise.all([
        api.get('/api/review/queue'),
        api.get('/api/templates/candidates'),
        api.get('/api/shadow/agreement'),
      ]);
      setQueue(queueData);
      setTemplates(templateData);
      setShadow(shadowData);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const open = async (id) => {
    setBusy(true);
    setError('');
    try {
      const [detailData, validationData] = await Promise.all([
        api.get(`/api/matches/${id}`),
        api.get(`/api/review/queue/${id}/validation`),
      ]);
      setDetail({ ...detailData, validationInfo: validationData });
      setSelected(id);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const decide = async (decision) => {
    if (!selected) return;
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const result = await api.post(`/api/review/queue/${selected}/decision`, { decision, actor, note });
      setNotice(`${decision} recorded (system had recommended ${result.decision}).`);
      setNote('');
      await load();
      await open(selected);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const confirmProposal = async (extractionLogId, accept) => {
    setBusy(true);
    setError('');
    try {
      await api.post(`/api/templates/proposals/${extractionLogId}/confirm`, { accept, actor });
      setNotice(accept ? 'Layout saved as a candidate template.' : 'Layout rejected — it will not be reused.');
      await load();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const validation = detail?.validationInfo?.validation || {};

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3 rounded-2xl border border-slate-800 bg-slate-900/60 p-4">
        <div>
          <h2 className="text-lg font-semibold text-slate-100">Review queue</h2>
          <p className="text-sm text-slate-400">
            The system's recommendation sits beside your decision. Agreement is measured per vendor before auto-approval is
            switched on.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <label className="text-xs text-slate-400">
            Your name
            <input
              value={actor}
              onChange={(event) => setActor(event.target.value)}
              className="ml-2 w-36 rounded-lg border border-slate-700 bg-slate-950 px-2 py-1.5 text-sm text-slate-100 outline-none focus:border-cyan-400"
            />
          </label>
          <button
            onClick={load}
            disabled={loading}
            className="rounded-lg border border-slate-700 px-3 py-2 text-xs font-semibold text-slate-300 hover:bg-slate-800 disabled:opacity-40"
          >
            {loading ? 'Refreshing…' : 'Refresh'}
          </button>
        </div>
      </div>

      {error ? <p className="rounded-lg border border-rose-900 bg-rose-950/40 p-3 text-sm text-rose-300">{error}</p> : null}
      {notice ? <p className="rounded-lg border border-emerald-900 bg-emerald-950/40 p-3 text-sm text-emerald-300">{notice}</p> : null}

      {shadow ? (
        <div className="flex flex-wrap items-center gap-3 rounded-xl border border-slate-800 bg-slate-900/40 px-4 py-3 text-xs text-slate-400">
          <Badge className={shadow.auto_approve_enabled ? DECISION_STYLES.APPROVE : DECISION_STYLES.REVIEW}>
            {shadow.auto_approve_enabled ? 'live' : 'shadow mode'}
          </Badge>
          <span>
            Overall agreement {Math.round((shadow.overall_agreement || 0) * 100)}% over {shadow.total_decisions} decision(s)
          </span>
          <span>
            Auto-approval needs {shadow.thresholds.min_decisions} decisions at{' '}
            {Math.round(shadow.thresholds.min_agreement * 100)}% per vendor
          </span>
          {shadow.vendors.filter((v) => v.earned).length ? (
            <span className="text-emerald-300">
              Earned: {shadow.vendors.filter((v) => v.earned).map((v) => v.vendor).join(', ')}
            </span>
          ) : null}
        </div>
      ) : null}

      <div className="grid gap-5 xl:grid-cols-[400px_minmax(0,1fr)]">
        <div className="space-y-4">
          <div className="rounded-2xl border border-slate-800 bg-slate-900/60">
            <div className="border-b border-slate-800 px-4 py-3">
              <h3 className="text-sm font-semibold text-slate-200">Waiting for a human ({queue.length})</h3>
            </div>
            <div className="max-h-[420px] overflow-y-auto">
              {queue.length === 0 ? (
                <p className="p-4 text-sm text-slate-500">Nothing waiting.</p>
              ) : (
                queue.map((item) => (
                  <button
                    key={item.id}
                    onClick={() => open(item.id)}
                    className={`flex w-full flex-col gap-1 border-b border-slate-800/60 px-4 py-3 text-left transition hover:bg-slate-800/40 ${
                      selected === item.id ? 'bg-slate-800/60' : ''
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-sm font-semibold text-slate-100">{item.invoice_number || '—'}</span>
                      <span className="text-xs text-slate-400">{money(item.grand_total, item.currency)}</span>
                    </div>
                    <div className="text-xs text-slate-500">
                      {item.vendor || 'Unknown vendor'} · PO {item.po_number || '—'}
                    </div>
                    <div className="flex flex-wrap gap-1 pt-1">
                      <Badge className={DECISION_STYLES[item.decision] || DECISION_STYLES.REVIEW}>
                        system: {String(item.decision || 'n/a').toLowerCase()}
                      </Badge>
                      {(item.decision_reasons || []).slice(0, 2).map((code) => (
                        <Badge key={code} className={STATUS_STYLES.EXCEPTION}>
                          {reasonLabel(code)}
                        </Badge>
                      ))}
                    </div>
                  </button>
                ))
              )}
            </div>
          </div>

          <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-4">
            <h3 className="text-sm font-semibold text-slate-200">Layouts to confirm</h3>
            <p className="mb-3 text-xs text-slate-500">
              AI reads are never saved as templates without a human. A confirmation creates a candidate; trust is earned.
            </p>
            <ProposalList items={templates.proposals || []} onConfirm={confirmProposal} busy={busy} />
          </div>
        </div>

        <div className="space-y-4">
          {detail ? (
            <>
              <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-5">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <h3 className="text-lg font-semibold text-slate-100">Invoice {detail.invoice?.invoice_number || '—'}</h3>
                    <Badge className={STATUS_STYLES[detail.status] || STATUS_STYLES.EXCEPTION}>{detail.status}</Badge>
                    <Badge className={DECISION_STYLES[detail.decision] || DECISION_STYLES.REVIEW}>
                      system recommends: {String(detail.decision || 'n/a').toLowerCase()}
                    </Badge>
                    <Badge
                      className={validation.passed ? DECISION_STYLES.APPROVE : DECISION_STYLES.REJECT}
                    >
                      validation {validation.passed ? 'passed' : 'failed'}
                    </Badge>
                  </div>
                  <div className="text-xs text-slate-500">
                    PO {detail.po?.po_number || '—'} · approval {String(detail.workflow?.approval_status || 'n/a').toLowerCase()}
                  </div>
                </div>

                {detail.decision_explanation ? (
                  <p className="mt-2 text-sm text-slate-400">{detail.decision_explanation}</p>
                ) : null}

                <div className="mt-4 grid gap-4 lg:grid-cols-2">
                  <div className="rounded-xl border border-slate-800 bg-slate-950/40 p-3">
                    <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-400">Validation checks</h4>
                    <CheckList validation={validation} />
                  </div>
                  <div className="rounded-xl border border-slate-800 bg-slate-950/40 p-3">
                    <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-400">Decision reasons</h4>
                    {(detail.validationInfo?.decision_reasons || []).length === 0 ? (
                      <p className="mt-2 text-xs text-slate-500">No blocking reasons.</p>
                    ) : (
                      <ul className="mt-2 space-y-1 text-xs text-amber-200/90">
                        {(detail.validationInfo?.decision_reasons || []).map((code) => (
                          <li key={code}>• {reasonLabel(code)}</li>
                        ))}
                      </ul>
                    )}
                  </div>
                </div>

                <div className="mt-4 rounded-xl border border-slate-800 bg-slate-950/40 p-3">
                  <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-400">Your decision</h4>
                  <textarea
                    value={note}
                    onChange={(event) => setNote(event.target.value)}
                    rows={2}
                    placeholder="Why you agree or disagree (recorded in the audit log and the shadow ledger)"
                    className="mt-2 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none focus:border-cyan-400"
                  />
                  <div className="mt-2 flex flex-wrap gap-2">
                    <button
                      disabled={busy || !actor.trim()}
                      onClick={() => decide('APPROVE')}
                      className="rounded-lg bg-emerald-400 px-3 py-2 text-xs font-semibold text-slate-950 hover:bg-emerald-300 disabled:opacity-40"
                    >
                      Approve
                    </button>
                    <button
                      disabled={busy || !actor.trim()}
                      onClick={() => decide('REVIEW')}
                      className="rounded-lg bg-slate-800 px-3 py-2 text-xs font-semibold text-slate-100 hover:bg-slate-700 disabled:opacity-40"
                    >
                      Keep in review
                    </button>
                    <button
                      disabled={busy || !actor.trim()}
                      onClick={() => decide('REJECT')}
                      className="rounded-lg bg-rose-600 px-3 py-2 text-xs font-semibold text-white hover:bg-rose-500 disabled:opacity-40"
                    >
                      Reject
                    </button>
                  </div>
                  <p className="mt-2 text-[11px] text-slate-500">
                    Approving an exception needs it resolved first, and the person who overrode it cannot approve it.
                  </p>
                </div>
              </div>

              <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-5">
                <h3 className="text-sm font-semibold text-slate-200">Side-by-side PO / GRN / invoice</h3>
                <p className="mb-3 text-xs text-slate-500">Mismatched fields are highlighted.</p>
                <SideBySide detail={detail} />
              </div>
            </>
          ) : (
            <div className="flex min-h-[320px] items-center justify-center rounded-2xl border border-slate-800 bg-slate-900/60 p-6 text-center">
              <div>
                <p className="text-sm font-semibold text-slate-300">Select an item from the queue</p>
                <p className="mt-1 max-w-md text-xs text-slate-500">
                  You will see the validation checks, the system's recommendation, and the side-by-side comparison before
                  deciding.
                </p>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
