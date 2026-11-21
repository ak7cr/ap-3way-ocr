import { useEffect, useState } from 'react';
import { api, money, reasonLabel } from '../api';

const STATUS_STYLES = {
  MATCHED: 'bg-emerald-950/60 text-emerald-300 border-emerald-800',
  EXCEPTION: 'bg-amber-950/60 text-amber-300 border-amber-800',
  REJECT: 'bg-rose-950/60 text-rose-300 border-rose-900',
};

const APPROVAL_STYLES = {
  APPROVED: 'bg-emerald-950/60 text-emerald-300 border-emerald-800',
  REJECTED: 'bg-rose-950/60 text-rose-300 border-rose-900',
  PENDING: 'bg-slate-900 text-slate-300 border-slate-700',
  NOT_REQUIRED: 'bg-slate-900 text-slate-400 border-slate-800',
};

const ACTIONS = [
  { key: 'ACCEPT_OVERRIDE', label: 'Accept with override', tone: 'bg-amber-400 text-slate-950 hover:bg-amber-300', needsReason: true },
  { key: 'CREDIT_NOTE', label: 'Request credit note', tone: 'bg-slate-800 text-slate-100 hover:bg-slate-700' },
  { key: 'HOLD', label: 'Put on hold', tone: 'bg-slate-800 text-slate-100 hover:bg-slate-700' },
  { key: 'REJECT', label: 'Reject invoice', tone: 'bg-rose-600 text-white hover:bg-rose-500' },
];

function Badge({ children, className = '' }) {
  return (
    <span className={`inline-flex items-center rounded-full border px-2.5 py-0.5 text-[11px] font-semibold uppercase tracking-wide ${className}`}>
      {children}
    </span>
  );
}

function MismatchCell({ value, highlight, unit }) {
  return (
    <td
      className={`whitespace-nowrap px-3 py-2 text-sm ${
        highlight ? 'bg-rose-950/50 font-semibold text-rose-300 ring-1 ring-inset ring-rose-800' : 'text-slate-300'
      }`}
    >
      {value ?? '—'}
      {unit ? <span className="ml-1 text-xs text-slate-500">{unit}</span> : null}
    </td>
  );
}

function SideBySide({ detail }) {
  return (
    <div className="space-y-4">
      <div className="overflow-x-auto rounded-xl border border-slate-800">
        <table className="min-w-full text-left">
          <thead className="bg-slate-950/70 text-[11px] uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-3 py-2">Header field</th>
              <th className="px-3 py-2">Invoice</th>
              <th className="px-3 py-2">Purchase order</th>
              <th className="px-3 py-2">Check</th>
            </tr>
          </thead>
          <tbody>
            {detail.header_fields.map((field) => (
              <tr key={field.field} className="border-t border-slate-800/70">
                <td className="px-3 py-2 text-sm font-medium text-slate-400">{reasonLabel(field.field)}</td>
                <td className="px-3 py-2 text-sm text-slate-200">{String(field.invoice ?? '—')}</td>
                <td className="px-3 py-2 text-sm text-slate-200">{String(field.po ?? '—')}</td>
                <td className="px-3 py-2">
                  <Badge className={field.match ? STATUS_STYLES.MATCHED : STATUS_STYLES.REJECT}>
                    {field.match ? 'ok' : 'mismatch'}
                  </Badge>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="overflow-x-auto rounded-xl border border-slate-800">
        <table className="min-w-full text-left">
          <thead className="bg-slate-950/70 text-[10px] uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-3 py-2">#</th>
              <th className="px-3 py-2" colSpan={4}>Invoice</th>
              <th className="px-3 py-2" colSpan={3}>PO</th>
              <th className="px-3 py-2" colSpan={2}>GRN / balance</th>
              <th className="px-3 py-2">Flags</th>
            </tr>
            <tr className="bg-slate-950/40 text-[10px] uppercase tracking-wide text-slate-600">
              <th />
              <th className="px-3 py-1.5">Item</th>
              <th className="px-3 py-1.5">Qty</th>
              <th className="px-3 py-1.5">Rate</th>
              <th className="px-3 py-1.5">Amount</th>
              <th className="px-3 py-1.5">Item</th>
              <th className="px-3 py-1.5">Qty</th>
              <th className="px-3 py-1.5">Rate</th>
              <th className="px-3 py-1.5">Received</th>
              <th className="px-3 py-1.5">Billable</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {detail.lines.map((line) => {
              const flags = new Set(line.mismatched_fields);
              const inv = line.invoice_line || {};
              const po = line.po_line || {};
              const grn = line.grn || {};
              return (
                <tr key={`${inv.line_no}-${po.line_no || 'none'}`} className="border-t border-slate-800/70">
                  <td className="px-3 py-2 text-xs text-slate-500">{inv.line_no}</td>
                  <MismatchCell value={inv.item_code || inv.description} highlight={flags.has('item')} />
                  <MismatchCell value={inv.quantity} highlight={flags.has('quantity')} unit={inv.unit} />
                  <MismatchCell value={money(inv.rate)} highlight={flags.has('rate')} />
                  <MismatchCell value={money(inv.amount)} highlight={flags.has('amount')} />
                  <MismatchCell value={po.item_code || po.description} highlight={flags.has('item')} />
                  <MismatchCell value={po.quantity} highlight={flags.has('quantity')} unit={po.unit} />
                  <MismatchCell value={money(po.rate)} highlight={flags.has('rate')} />
                  <MismatchCell value={grn.received_qty} highlight={flags.has('quantity')} />
                  <MismatchCell value={grn.billable_qty} highlight={flags.has('quantity')} />
                  <td className="px-3 py-2">
                    <div className="flex flex-wrap gap-1">
                      {line.reason_codes.map((code) => (
                        <Badge key={code} className={STATUS_STYLES.EXCEPTION}>{reasonLabel(code)}</Badge>
                      ))}
                      {line.reason_codes.length === 0 ? <Badge className={STATUS_STYLES.MATCHED}>ok</Badge> : null}
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="text-xs text-slate-500">
        Line mapping used: {detail.lines.map((l) => `${l.invoice_line.line_no}→${l.grn.mapping_method}`).join(', ') || '—'}
      </p>
    </div>
  );
}

export { Badge, STATUS_STYLES, APPROVAL_STYLES, SideBySide };

export default function DetailPanel({ detail, onAction, onRefresh, busy }) {
  const [reason, setReason] = useState('');
  const [actor, setActor] = useState('accountant');
  const [explanation, setExplanation] = useState(null);
  const [email, setEmail] = useState(null);
  const [suggestions, setSuggestions] = useState(null);
  const [localError, setLocalError] = useState('');

  useEffect(() => {
    setReason('');
    setExplanation(null);
    setEmail(null);
    setSuggestions(null);
    setLocalError('');
  }, [detail.match_result_id]);

  const workflow = detail.workflow || {};
  const resolved = Boolean(workflow.resolution);
  const invoice = detail.invoice || {};

  const run = async (fn) => {
    setLocalError('');
    try {
      await fn();
    } catch (err) {
      setLocalError(err.message);
    }
  };

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-lg font-semibold text-slate-100">Invoice {invoice.invoice_number || '—'}</h3>
            <Badge className={STATUS_STYLES[detail.status] || STATUS_STYLES.PENDING}>{detail.status}</Badge>
            <Badge className={APPROVAL_STYLES[workflow.approval_status] || APPROVAL_STYLES.PENDING}>
              approval: {String(workflow.approval_status || 'n/a').toLowerCase()}
            </Badge>
            {workflow.resolution ? <Badge className={STATUS_STYLES.EXCEPTION}>{reasonLabel(workflow.resolution)}</Badge> : null}
          </div>
          <p className="mt-1 text-sm text-slate-400">
            {invoice.vendor || 'Unknown vendor'} · {invoice.currency} {money(invoice.grand_total, invoice.currency)} · PO{' '}
            {detail.po?.po_number || '—'}
          </p>
        </div>
        <div className="text-right text-xs text-slate-500">
          <div>Approvals: {workflow.approvals_received}/{workflow.approvals_required}</div>
          <div>Roles: {(workflow.required_roles || []).join(', ') || '—'}</div>
        </div>
      </div>

      {detail.reason_codes.length > 0 ? (
        <div className="rounded-xl border border-amber-800/60 bg-amber-950/20 p-3">
          <div className="text-xs font-semibold uppercase tracking-wide text-amber-300">Why it stopped</div>
          <ul className="mt-2 space-y-1 text-sm text-amber-100/90">
            {detail.reason_codes.map((code, index) => (
              <li key={code}>• {detail.reason_descriptions[index] || reasonLabel(code)}</li>
            ))}
          </ul>
        </div>
      ) : null}

      <SideBySide detail={detail} />

      <div className="grid gap-4 lg:grid-cols-2">
        <div className="rounded-xl border border-slate-800 bg-slate-950/40 p-4">
          <h4 className="text-sm font-semibold text-slate-200">Exception actions</h4>
          <p className="mt-1 text-xs text-slate-500">
            An override reason is required and is written to the audit log. The approver can never be the person who overrode it.
          </p>
          <label className="mt-3 block text-xs font-medium text-slate-400">
            Override / note reason
            <textarea
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              rows={2}
              placeholder="e.g. Vendor confirmed 120 shipped, GRN pending update"
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none focus:border-cyan-400"
            />
          </label>
          <label className="mt-2 block text-xs font-medium text-slate-400">
            Your name
            <input
              value={actor}
              onChange={(event) => setActor(event.target.value)}
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none focus:border-cyan-400"
            />
          </label>
          <div className="mt-3 flex flex-wrap gap-2">
            {ACTIONS.map((action) => (
              <button
                key={action.key}
                disabled={busy || resolved || (action.needsReason && !reason.trim())}
                onClick={() => onAction(action.key, reason, actor)}
                className={`rounded-lg px-3 py-2 text-xs font-semibold transition disabled:cursor-not-allowed disabled:opacity-40 ${action.tone}`}
              >
                {action.label}
              </button>
            ))}
          </div>
          {resolved ? (
            <p className="mt-2 text-xs text-slate-500">
              Resolved as {reasonLabel(workflow.resolution)} by {workflow.resolved_by}. Continue in the Approvals tab.
            </p>
          ) : null}
          {localError ? <p className="mt-2 text-xs text-rose-400">{localError}</p> : null}
        </div>

        <div className="rounded-xl border border-slate-800 bg-slate-950/40 p-4">
          <h4 className="text-sm font-semibold text-slate-200">LLM assist (advisory only)</h4>
          <p className="mt-1 text-xs text-slate-500">
            The match decision never uses an LLM. These helpers only explain, draft, or suggest.
          </p>
          <div className="mt-3 flex flex-wrap gap-2">
            <button
              onClick={() => run(async () => setExplanation(await api.post(`/api/matches/${detail.match_result_id}/explain`)))}
              className="rounded-lg bg-slate-800 px-3 py-2 text-xs font-semibold text-slate-100 hover:bg-slate-700"
            >
              Explain mismatch
            </button>
            <button
              onClick={() => run(async () => setEmail(await api.post(`/api/matches/${detail.match_result_id}/draft-email`)))}
              className="rounded-lg bg-slate-800 px-3 py-2 text-xs font-semibold text-slate-100 hover:bg-slate-700"
            >
              Draft vendor email
            </button>
            <button
              onClick={() => run(async () => setSuggestions(await api.get(`/api/matches/${detail.match_result_id}/line-suggestions`)))}
              className="rounded-lg bg-slate-800 px-3 py-2 text-xs font-semibold text-slate-100 hover:bg-slate-700"
            >
              Suggest line mappings
            </button>
            <button
              onClick={() => onRefresh()}
              className="rounded-lg border border-slate-700 px-3 py-2 text-xs font-semibold text-slate-300 hover:bg-slate-800"
            >
              Refresh
            </button>
          </div>
          {explanation ? (
            <div className="mt-3 rounded-lg border border-slate-800 bg-slate-900/70 p-3">
              <div className="text-[11px] uppercase tracking-wide text-slate-500">Explanation ({explanation.generated_by})</div>
              <pre className="mt-1 whitespace-pre-wrap text-xs text-slate-300">{explanation.explanation}</pre>
            </div>
          ) : null}
          {email ? (
            <div className="mt-3 rounded-lg border border-slate-800 bg-slate-900/70 p-3">
              <div className="text-[11px] uppercase tracking-wide text-slate-500">Vendor email ({email.generated_by})</div>
              <div className="mt-1 text-xs font-semibold text-slate-200">{email.subject}</div>
              <pre className="mt-1 whitespace-pre-wrap text-xs text-slate-300">{email.body}</pre>
            </div>
          ) : null}
          {suggestions ? (
            <div className="mt-3 rounded-lg border border-slate-800 bg-slate-900/70 p-3">
              <div className="text-[11px] uppercase tracking-wide text-slate-500">Fuzzy line suggestions</div>
              {suggestions.suggestions.length === 0 ? (
                <p className="mt-1 text-xs text-slate-400">All lines are already mapped deterministically.</p>
              ) : (
                <ul className="mt-1 space-y-1 text-xs text-slate-300">
                  {suggestions.suggestions.map((s) => (
                    <li key={s.invoice_line_no}>
                      Line {s.invoice_line_no} ({s.invoice_description}) → PO line {s.suggested_po_line_no} (
                      {s.suggested_po_description}) · confidence {Math.round(s.confidence * 100)}%
                    </li>
                  ))}
                </ul>
              )}
            </div>
          ) : null}
        </div>
      </div>

      {invoice.source_file_url ? (
        <details className="rounded-xl border border-slate-800 bg-slate-950/40 p-4">
          <summary className="cursor-pointer text-sm font-semibold text-slate-200">Source document</summary>
          <a href={invoice.source_file_url} target="_blank" rel="noreferrer" className="mt-2 inline-block text-xs text-cyan-400 underline">
            Open original document
          </a>
        </details>
      ) : null}
    </div>
  );
}

