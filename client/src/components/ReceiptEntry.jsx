import { useRef, useState } from 'react';
import { api } from '../api';

const emptyRow = () => ({ received_qty: '' });

export default function ReceiptEntry() {
  const [poNumber, setPoNumber] = useState('');
  const [poLines, setPoLines] = useState(null);
  const [rows, setRows] = useState({});
  const [receiptDate, setReceiptDate] = useState(new Date().toISOString().slice(0, 10));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const sheetInput = useRef(null);

  const loadPo = async () => {
    if (!poNumber.trim()) return;
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const data = await api.get(`/api/grns/entry/${encodeURIComponent(poNumber.trim())}`);
      setPoLines(data.lines);
      setRows(Object.fromEntries(data.lines.map((line) => [line.po_line_no, emptyRow()])));
    } catch (err) {
      setPoLines(null);
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const setQty = (lineNo, value) => setRows((current) => ({ ...current, [lineNo]: { received_qty: value } }));

  const submit = async () => {
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const lines = Object.entries(rows)
        .filter(([, row]) => String(row.received_qty).trim() !== '')
        .map(([lineNo, row]) => ({ po_line_no: Number(lineNo), received_qty: row.received_qty }));
      if (!lines.length) throw new Error('Enter at least one received quantity');
      const result = await api.post('/api/grns/manual', {
        po_number: poNumber.trim(),
        receipt_date: receiptDate,
        lines,
      });
      setNotice(`Receipt ${result.grn_number} recorded against ${result.po_number} (${result.lines.length} line(s)).`);
      await loadPo();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const uploadSheet = async (file) => {
    if (!file) return;
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const body = new FormData();
      body.append('file', file);
      const result = await api.postForm('/api/grns/manual/import', body);
      setNotice(`${result.imported} receipt(s) imported from the sheet.`);
      if (poLines) await loadPo();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-5">
      <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-5">
        <h2 className="text-lg font-semibold text-slate-100">Record a goods receipt</h2>
        <p className="mt-1 text-sm text-slate-400">
          For customers without an ERP, this replaces scanning a GRN. The store person types the received quantity against
          the PO they already have — the weakest OCR input simply stops existing.
        </p>

        <div className="mt-4 flex flex-wrap items-end gap-3">
          <label className="text-sm font-medium text-slate-300">
            PO number
            <input
              value={poNumber}
              onChange={(event) => setPoNumber(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') loadPo();
              }}
              placeholder="e.g. PO-2026-001"
              className="mt-1.5 block w-56 rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none focus:border-cyan-400"
            />
          </label>
          <label className="text-sm font-medium text-slate-300">
            Receipt date
            <input
              type="date"
              value={receiptDate}
              onChange={(event) => setReceiptDate(event.target.value)}
              className="mt-1.5 block rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none focus:border-cyan-400"
            />
          </label>
          <button
            onClick={loadPo}
            disabled={busy || !poNumber.trim()}
            className="rounded-lg bg-cyan-400 px-4 py-2.5 text-sm font-semibold text-slate-950 hover:bg-cyan-300 disabled:opacity-40"
          >
            {busy ? 'Loading…' : 'Load PO lines'}
          </button>
          <button
            onClick={() => sheetInput.current?.click()}
            disabled={busy}
            className="rounded-lg border border-slate-700 px-4 py-2.5 text-sm font-semibold text-slate-300 hover:bg-slate-800 disabled:opacity-40"
          >
            Upload a receipt sheet
          </button>
          <input
            ref={sheetInput}
            type="file"
            accept=".csv,.tsv,.xlsx,.xlsm"
            className="hidden"
            onChange={(event) => {
              uploadSheet(event.target.files?.[0]);
              event.target.value = '';
            }}
          />
        </div>

        <p className="mt-2 text-xs text-slate-500">
          Sheet columns: <code className="text-slate-400">po_number, po_line_no, item_code, received_qty</code> (one row per line).
        </p>

        {error ? <p className="mt-3 rounded-lg border border-rose-900 bg-rose-950/40 p-3 text-sm text-rose-300">{error}</p> : null}
        {notice ? <p className="mt-3 rounded-lg border border-emerald-900 bg-emerald-950/40 p-3 text-sm text-emerald-300">{notice}</p> : null}
      </div>

      {poLines ? (
        <div className="rounded-2xl border border-slate-800 bg-slate-900/60">
          <div className="flex items-center justify-between border-b border-slate-800 px-4 py-3">
            <h3 className="text-sm font-semibold text-slate-200">Lines on {poNumber}</h3>
            <button
              onClick={submit}
              disabled={busy}
              className="rounded-lg bg-emerald-400 px-3 py-2 text-xs font-semibold text-slate-950 hover:bg-emerald-300 disabled:opacity-40"
            >
              {busy ? 'Saving…' : 'Record receipt'}
            </button>
          </div>
          <div className="overflow-x-auto">
            <table className="min-w-full text-left text-sm">
              <thead className="bg-slate-950/60 text-[11px] uppercase tracking-wide text-slate-500">
                <tr>
                  <th className="px-3 py-2">Line</th>
                  <th className="px-3 py-2">Item code</th>
                  <th className="px-3 py-2">Description</th>
                  <th className="px-3 py-2">Ordered</th>
                  <th className="px-3 py-2">Already received</th>
                  <th className="px-3 py-2">Remaining</th>
                  <th className="px-3 py-2">Received now</th>
                </tr>
              </thead>
              <tbody>
                {poLines.map((line) => (
                  <tr key={line.po_line_no} className="border-t border-slate-800/70">
                    <td className="px-3 py-2 text-slate-400">{line.po_line_no}</td>
                    <td className="px-3 py-2 font-mono text-xs text-slate-300">{line.item_code || '—'}</td>
                    <td className="px-3 py-2 text-slate-300">{line.description || '—'}</td>
                    <td className="px-3 py-2 text-slate-300">
                      {line.ordered_qty} {line.unit || ''}
                    </td>
                    <td className="px-3 py-2 text-slate-400">{line.already_received_qty}</td>
                    <td className="px-3 py-2 text-slate-400">{line.remaining_qty}</td>
                    <td className="px-3 py-2">
                      <input
                        value={rows[line.po_line_no]?.received_qty || ''}
                        onChange={(event) => setQty(line.po_line_no, event.target.value)}
                        inputMode="decimal"
                        placeholder="0"
                        className="w-24 rounded-lg border border-slate-700 bg-slate-950 px-2 py-1.5 text-sm text-slate-100 outline-none focus:border-cyan-400"
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}
    </div>
  );
}
