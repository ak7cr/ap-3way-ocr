import { useCallback, useEffect, useState } from 'react';
import { api, money, reasonLabel } from '../api';

export default function Approvals() {
  const [rows, setRows] = useState([]);
  const [approver, setApprover] = useState('bob');
  const [role, setRole] = useState('finance');
  const [note, setNote] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      setRows(await api.get('/api/approvals'));
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const decide = async (id, decision) => {
    setError('');
    setNotice('');
    try {
      const updated = await api.post(`/api/matches/${id}/approve`, { approver, role, decision, note });
      const workflow = updated.workflow || {};
      setNotice(
        `${decision.toLowerCase()} recorded on invoice ${updated.invoice?.invoice_number || id}. Status: ${workflow.approval_status} (${workflow.approvals_received}/${workflow.approvals_required}).`
      );
      await load();
    } catch (err) {
      setError(err.message);
    }
  };

  const download = async (path, filename) => {
    setError('');
    try {
      await api.downloadBlob(path, filename);
    } catch (err) {
      setError(err.message);
    }
  };

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3 rounded-2xl border border-slate-800 bg-slate-900/60 p-4">
        <div>
          <h2 className="text-lg font-semibold text-slate-100">Approvals & payment release</h2>
          <p className="text-sm text-slate-400">
            Rules by amount and vendor risk. The person who overrode an exception cannot approve it.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <label className="text-xs text-slate-400">
            Approver
            <input
              value={approver}
              onChange={(event) => setApprover(event.target.value)}
              className="ml-2 w-28 rounded-lg border border-slate-700 bg-slate-950 px-2 py-1.5 text-sm text-slate-100 outline-none focus:border-cyan-400"
            />
          </label>
          <label className="text-xs text-slate-400">
            Role
            <select
              value={role}
              onChange={(event) => setRole(event.target.value)}
              className="ml-2 rounded-lg border border-slate-700 bg-slate-950 px-2 py-1.5 text-sm text-slate-100 outline-none focus:border-cyan-400"
            >
              <option value="finance">finance</option>
              <option value="manager">manager</option>
              <option value="director">director</option>
            </select>
          </label>
          <button onClick={load} className="rounded-lg border border-slate-700 px-3 py-2 text-xs font-semibold text-slate-300 hover:bg-slate-800">
            {loading ? 'Refreshing…' : 'Refresh'}
          </button>
          <button
            onClick={() => download('/api/payments/export.csv', 'payment_ready.csv')}
            className="rounded-lg bg-emerald-500 px-3 py-2 text-xs font-semibold text-slate-950 hover:bg-emerald-400"
          >
            Export payment CSV
          </button>
          <button
            onClick={() => download('/api/payments/tally.xml', 'payment_ready_tally.xml')}
            className="rounded-lg bg-slate-800 px-3 py-2 text-xs font-semibold text-slate-100 hover:bg-slate-700"
          >
            Tally XML
          </button>
        </div>
      </div>

      {notice ? <p className="rounded-lg border border-emerald-900 bg-emerald-950/40 p-3 text-sm text-emerald-300">{notice}</p> : null}
      {error ? <p className="rounded-lg border border-rose-900 bg-rose-950/40 p-3 text-sm text-rose-300">{error}</p> : null}

      <label className="block text-xs text-slate-400">
        Approval note (optional)
        <input
          value={note}
          onChange={(event) => setNote(event.target.value)}
          className="mt-1 w-full max-w-xl rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none focus:border-cyan-400"
        />
      </label>

      <div className="overflow-x-auto rounded-2xl border border-slate-800">
        <table className="min-w-full text-left">
          <thead className="bg-slate-950/70 text-[11px] uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-4 py-3">Invoice</th>
              <th className="px-4 py-3">Vendor</th>
              <th className="px-4 py-3">Amount</th>
              <th className="px-4 py-3">Match</th>
              <th className="px-4 py-3">Resolution</th>
              <th className="px-4 py-3">Approvals</th>
              <th className="px-4 py-3">Actions</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr>
                <td colSpan={7} className="px-4 py-6 text-sm text-slate-500">
                  No pending approvals.
                </td>
              </tr>
            ) : (
              rows.map((row) => {
                const blocked = row.match_status === 'EXCEPTION' && !row.resolution;
                return (
                  <tr key={row.id} className="border-t border-slate-800/70 align-top">
                    <td className="px-4 py-3 text-sm font-medium text-slate-200">{row.invoice_number || '—'}</td>
                    <td className="px-4 py-3 text-sm text-slate-400">{row.vendor || '—'}</td>
                    <td className="px-4 py-3 text-sm text-slate-200">{money(row.grand_total, row.currency)}</td>
                    <td className="px-4 py-3">
                      <span
                        className={`inline-flex rounded-full border px-2 py-0.5 text-[11px] font-semibold ${
                          row.match_status === 'MATCHED'
                            ? 'border-emerald-800 bg-emerald-950/60 text-emerald-300'
                            : 'border-amber-800 bg-amber-950/60 text-amber-300'
                        }`}
                      >
                        {row.match_status}
                      </span>
                      {row.reason_codes.length > 0 ? (
                        <div className="mt-1 text-[11px] text-slate-500">{row.reason_codes.map(reasonLabel).join(', ')}</div>
                      ) : null}
                    </td>
                    <td className="px-4 py-3 text-xs text-slate-400">
                      {row.resolution ? `${reasonLabel(row.resolution)} by ${row.resolved_by || '—'}` : blocked ? 'needs resolution' : 'auto'}
                    </td>
                    <td className="px-4 py-3 text-xs text-slate-400">
                      <div>
                        {row.approvals_received}/{row.approvals_required} · {(row.required_roles || []).join(', ')}
                      </div>
                      <div className="text-[11px] text-slate-500">{(row.approvers || []).join(', ') || 'no approvals yet'}</div>
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex flex-wrap gap-2">
                        <button
                          disabled={blocked}
                          onClick={() => decide(row.id, 'APPROVED')}
                          className="rounded-lg bg-emerald-500 px-2.5 py-1.5 text-xs font-semibold text-slate-950 hover:bg-emerald-400 disabled:cursor-not-allowed disabled:opacity-40"
                        >
                          Approve
                        </button>
                        <button
                          onClick={() => decide(row.id, 'REJECTED')}
                          className="rounded-lg bg-rose-600 px-2.5 py-1.5 text-xs font-semibold text-white hover:bg-rose-500"
                        >
                          Reject
                        </button>
                      </div>
                      {blocked ? <div className="mt-1 text-[11px] text-amber-400">Resolve in 3-Way Match first</div> : null}
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
