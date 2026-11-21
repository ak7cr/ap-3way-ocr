import { useCallback, useEffect, useRef, useState } from 'react';
import { api, money, reasonLabel } from '../api';
import DetailPanel, { Badge, APPROVAL_STYLES, STATUS_STYLES } from './MatchDetail';

const TYPE_STYLES = {
  PURCHASE_ORDER: 'bg-sky-950/60 text-sky-300 border-sky-800',
  GRN: 'bg-violet-950/60 text-violet-300 border-violet-800',
  INVOICE: 'bg-cyan-950/60 text-cyan-300 border-cyan-800',
  UNKNOWN: 'bg-slate-900 text-slate-400 border-slate-700',
};

const ACTION_STYLES = {
  purchase_order_imported: STATUS_STYLES.MATCHED,
  grn_imported: STATUS_STYLES.MATCHED,
  invoice_matched: STATUS_STYLES.MATCHED,
  invoice_review: STATUS_STYLES.EXCEPTION,
  invoice_exception: STATUS_STYLES.EXCEPTION,
  invoice_rejected: STATUS_STYLES.REJECT,
  unclassified: STATUS_STYLES.REJECT,
  error: STATUS_STYLES.REJECT,
};

const DECISION_STYLES = {
  APPROVE: STATUS_STYLES.MATCHED,
  REVIEW: STATUS_STYLES.EXCEPTION,
  REJECT: STATUS_STYLES.REJECT,
};

const ACTION_LABELS = {
  purchase_order_imported: 'PO imported',
  grn_imported: 'GRN recorded',
  invoice_matched: 'Invoice auto-approved',
  invoice_review: 'Invoice needs review',
  invoice_exception: 'Invoice exception',
  invoice_rejected: 'Invoice rejected',
  unclassified: 'Not recognised',
  error: 'Needs attention',
};

const ACCEPT = 'application/pdf,image/jpeg,image/png,image/webp,image/tiff';

const fileKey = (file) => `${file.name}-${file.size}-${file.lastModified}`;

function DropZone({ files, onPick, onRemove, onClear, disabled = false }) {
  const input = useRef(null);
  const [hover, setHover] = useState(false);

  const pick = (event) => {
    const picked = Array.from(event.target.files || []);
    if (picked.length) onPick(picked);
    event.target.value = '';
  };

  return (
    <div>
      <div
        onDragOver={(event) => {
          event.preventDefault();
          if (!disabled) setHover(true);
        }}
        onDragLeave={() => setHover(false)}
        onDrop={(event) => {
          event.preventDefault();
          setHover(false);
          if (disabled) return;
          const dropped = Array.from(event.dataTransfer.files || []);
          if (dropped.length) onPick(dropped);
        }}
        onClick={() => !disabled && input.current?.click()}
        className={`flex min-h-40 flex-col items-center justify-center rounded-xl border border-dashed p-6 text-center transition ${
          disabled
            ? 'cursor-not-allowed border-slate-800 bg-slate-950/20 opacity-60'
            : hover
              ? 'cursor-pointer border-cyan-400 bg-cyan-950/20'
              : 'cursor-pointer border-slate-600 bg-slate-950/40 hover:border-cyan-400'
        }`}
      >
        <span className="text-3xl">⇧</span>
        <span className="mt-3 font-medium text-slate-100">Add PO, GRN and invoice files to the queue</span>
        <span className="mt-1 text-sm text-slate-500">
          Add them one at a time or all at once — nothing is processed until you press Process all. PDF · JPG · PNG · WebP · TIFF
        </span>
      </div>
      <input ref={input} className="hidden" type="file" multiple accept={ACCEPT} onChange={pick} />
      {files.length > 0 ? (
        <>
          <div className="mt-3 flex items-center justify-between text-xs text-slate-500">
            <span>
              Queued: {files.length} file{files.length === 1 ? '' : 's'}
            </span>
            <button onClick={onClear} disabled={disabled} className="underline hover:text-slate-300 disabled:opacity-40">
              Clear queue
            </button>
          </div>
          <ul className="mt-2 space-y-1.5">
            {files.map((file, index) => (
              <li
                key={fileKey(file)}
                className="flex items-center justify-between gap-2 rounded-lg border border-slate-800 bg-slate-950/60 px-3 py-2 text-sm"
              >
                <span className="flex min-w-0 items-center gap-2">
                  <span className="shrink-0 text-xs text-slate-600">{index + 1}</span>
                  <span className="truncate text-slate-300">{file.name}</span>
                </span>
                <span className="flex shrink-0 items-center gap-2">
                  <span className="text-xs text-slate-500">{(file.size / 1024).toFixed(0)} KB</span>
                  <button
                    onClick={() => onRemove(fileKey(file))}
                    disabled={disabled}
                    aria-label={`Remove ${file.name}`}
                    className="rounded px-1 text-slate-500 hover:text-rose-300 disabled:opacity-40"
                  >
                    ✕
                  </button>
                </span>
              </li>
            ))}
          </ul>
        </>
      ) : null}
    </div>
  );
}

function DocumentCard({ doc, onOpenMatch }) {
  const meta = doc.preview || {};
  return (
    <div className="rounded-xl border border-slate-800 bg-slate-950/40 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate text-sm font-semibold text-slate-100">{doc.file_name}</div>
          <div className="text-xs text-slate-500">
            {meta.vendor || 'Vendor not read'}
            {meta.document_number ? ` · ${meta.document_number}` : ''}
            {meta.document_date ? ` · ${meta.document_date}` : ''}
            {meta.total ? ` · ${money(meta.total, meta.currency)}` : ''}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge className={TYPE_STYLES[doc.doc_type] || TYPE_STYLES.UNKNOWN}>
            {doc.doc_type_label} {Math.round((doc.type_confidence || 0) * 100)}%
          </Badge>
          <Badge className={ACTION_STYLES[doc.action] || TYPE_STYLES.UNKNOWN}>
            {ACTION_LABELS[doc.action] || doc.action}
          </Badge>
          {doc.validation_passed === false ? (
            <Badge className={STATUS_STYLES.REJECT}>validation failed</Badge>
          ) : doc.validation_passed ? (
            <Badge className={STATUS_STYLES.MATCHED}>validated</Badge>
          ) : null}
          {doc.match?.decision ? (
            <Badge className={DECISION_STYLES[doc.match.decision] || STATUS_STYLES.EXCEPTION}>
              system: {String(doc.match.decision).toLowerCase()}
            </Badge>
          ) : null}
        </div>
      </div>

      <p className="mt-2 text-sm text-slate-400">{doc.message}</p>

      {doc.doc_type === 'GRN' && (doc.record?.line_items || []).length ? (
        <ul className="mt-2 space-y-0.5 text-xs text-slate-400">
          {doc.record.line_items.map((line) => (
            <li key={line.line_no}>
              <span className="text-slate-300">{line.item_code || '—'}</span>
              {line.description ? ` · ${line.description}` : ''}
              <span className="text-slate-200"> · received {line.received_qty}</span>
              {line.uom ? ` ${line.uom}` : ''}
            </li>
          ))}
        </ul>
      ) : null}

      {(doc.ocr_attempts || []).some((attempt) => !attempt.ok && attempt.reason !== 'not_an_image') ? (
        <div className="mt-2 text-xs text-amber-300">
          OCR attempts:{' '}
          {doc.ocr_attempts
            .filter((attempt) => !attempt.ok && attempt.reason !== 'not_an_image')
            .map((attempt) => `${attempt.engine}: ${attempt.reason || 'no text'}`)
            .join(' · ')}
        </div>
      ) : null}

      {doc.match?.decision_explanation ? (
        <p className="mt-1 text-xs text-slate-500">{doc.match.decision_explanation}</p>
      ) : null}

      {(doc.validation?.errors || []).length ? (
        <ul className="mt-2 space-y-0.5 text-xs text-rose-300/90">
          {doc.validation.errors.map((code) => (
            <li key={code}>• {reasonLabel(code)}</li>
          ))}
        </ul>
      ) : null}

      {doc.match ? (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          {doc.match.reason_codes?.map((code) => (
            <Badge key={code} className={STATUS_STYLES.EXCEPTION}>
              {reasonLabel(code)}
            </Badge>
          ))}
          <button
            onClick={() => onOpenMatch(doc.match.match_result_id)}
            className="rounded-lg border border-slate-700 px-2.5 py-1 text-xs font-semibold text-slate-200 hover:bg-slate-800"
          >
            Open side-by-side
          </button>
        </div>
      ) : null}

      {doc.extraction?.ai_fallback ? (
        <p className="mt-2 text-[11px] uppercase tracking-wide text-slate-600">
          AI fallback used ({doc.extraction.engine})
        </p>
      ) : null}
    </div>
  );
}

export default function DocumentsPage() {
  const [files, setFiles] = useState([]);
  const [method, setMethod] = useState('auto');
  const [poNumber, setPoNumber] = useState('');
  const [processing, setProcessing] = useState(false);
  const [ingest, setIngest] = useState(null);
  const [error, setError] = useState('');

  const [queue, setQueue] = useState([]);
  const [matches, setMatches] = useState([]);
  const [detail, setDetail] = useState(null);
  const [selectedId, setSelectedId] = useState(null);
  const [busy, setBusy] = useState(false);
  const [showResolved, setShowResolved] = useState(false);
  const [notice, setNotice] = useState('');
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [queueData, matchData] = await Promise.all([
        api.get(`/api/exceptions${showResolved ? '?include_resolved=true' : ''}`),
        api.get('/api/matches?limit=50'),
      ]);
      setQueue(queueData);
      setMatches(matchData);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [showResolved]);

  useEffect(() => {
    load();
  }, [load]);

  const openDetail = useCallback(async (id) => {
    if (!id) return;
    setBusy(true);
    setError('');
    try {
      setDetail(await api.get(`/api/matches/${id}`));
      setSelectedId(id);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }, []);

  const addFiles = useCallback((picked) => {
    setFiles((current) => {
      const seen = new Set(current.map(fileKey));
      return [...current, ...picked.filter((file) => !seen.has(fileKey(file)))];
    });
  }, []);

  const removeFile = useCallback((key) => {
    setFiles((current) => current.filter((file) => fileKey(file) !== key));
  }, []);

  const process = async () => {
    if (!files.length) return;
    const batch = files;
    setProcessing(true);
    setError('');
    setNotice('');
    try {
      const body = new FormData();
      batch.forEach((file) => body.append('files', file));
      body.append('extraction_method', method);
      if (poNumber.trim()) body.append('po_number', poNumber.trim());
      const result = await api.postForm('/api/documents/ingest', body);
      setIngest(result);
      const summary = result.summary || {};
      setNotice(
        `${summary.files} file(s): ${summary.purchase_orders} PO, ${summary.goods_receipts} GRN, ${summary.invoices} invoice — ` +
          `${summary.matched} auto-approved, ${(summary.review || 0) + (summary.exceptions || 0)} for review, ${summary.rejected} rejected` +
          `${summary.validation_failed ? `, ${summary.validation_failed} failed validation` : ''}.`
      );
      // Drop only the files that were just processed; anything added while the
      // request was running stays queued.
      setFiles((current) => current.filter((file) => !batch.includes(file)));
      await load();
      const firstInvoice = (result.documents || []).find((doc) => doc.match?.match_result_id);
      if (firstInvoice) await openDetail(firstInvoice.match.match_result_id);
    } catch (err) {
      setError(err.message);
    } finally {
      setProcessing(false);
    }
  };

  const seedDemo = async () => {
    setBusy(true);
    setError('');
    try {
      const result = await api.post('/api/threeway/seed-demo');
      setNotice(
        `Demo data loaded: ${result.clean_match.status} invoice and ${result.exception_match.status} invoice (${result.exception_match.reason_codes.length} reasons).`
      );
      await load();
      await openDetail(result.exception_match.match_result_id);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const handleAction = async (action, reason, actor) => {
    if (!selectedId) return;
    setBusy(true);
    setError('');
    try {
      const updated = await api.post(`/api/matches/${selectedId}/actions`, { action, reason, actor });
      setDetail(updated);
      setNotice(`${action.replaceAll('_', ' ').toLowerCase()} recorded. Continue in Approvals.`);
      await load();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const summary = ingest?.summary;
  const cleanMatches = matches.filter((item) => item.status !== 'EXCEPTION');

  return (
    <div className="space-y-5">
      <div className="grid gap-5 xl:grid-cols-[420px_minmax(0,1fr)]">
        <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-5">
          <h2 className="text-lg font-semibold text-slate-100">1. Add documents to the queue</h2>
          <p className="mt-1 text-sm text-slate-400">
            Add a purchase order, goods receipt or invoice one at a time or all at once. Files wait in the queue until
            you press Process all; then each one is read, recognised and routed automatically.
          </p>

          <div className="mt-4">
            <DropZone
              files={files}
              onPick={addFiles}
              onRemove={removeFile}
              onClear={() => setFiles([])}
              disabled={processing}
            />
          </div>

          <label className="mt-4 block text-sm font-medium text-slate-300">
            Text extraction
            <select
              value={method}
              onChange={(event) => setMethod(event.target.value)}
              className="mt-1.5 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2.5 text-slate-100 outline-none focus:border-cyan-400"
            >
              <option value="auto">Automatic — local OCR, AI fallback when confidence is low</option>
              <option value="ai">Always use AI (Claude → Gemini)</option>
              <option value="local">Local OCR only (no AI)</option>
            </select>
          </label>

          <label className="mt-3 block text-sm font-medium text-slate-300">
            PO number (optional)
            <input
              value={poNumber}
              onChange={(event) => setPoNumber(event.target.value)}
              placeholder="Only if the invoice does not print it"
              className="mt-1.5 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2.5 text-sm text-slate-100 outline-none focus:border-cyan-400"
            />
          </label>

          <button
            onClick={process}
            disabled={!files.length || processing}
            className="mt-4 w-full rounded-xl bg-cyan-400 px-4 py-3 font-semibold text-slate-950 transition hover:bg-cyan-300 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {processing
              ? 'Processing queue…'
              : files.length
                ? `Process all ${files.length} document${files.length === 1 ? '' : 's'}`
                : 'Process all documents'}
          </button>

          <div className="mt-3 flex items-center justify-between text-xs text-slate-500">
            <button onClick={load} disabled={loading} className="underline hover:text-slate-300 disabled:opacity-40">
              {loading ? 'Refreshing…' : 'Refresh queue'}
            </button>
            <button onClick={seedDemo} disabled={busy} className="underline hover:text-slate-300 disabled:opacity-40">
              Load demo data
            </button>
          </div>

          <div className="mt-4 border-t border-slate-800 pt-4 text-xs text-slate-500">
            <p className="font-semibold text-slate-400">What happens next</p>
            <ol className="mt-1.5 space-y-1">
              <li>1. OCR reads the file; AI breaks ties only when confidence is low.</li>
              <li>2. The document type is detected from its content, not its file name.</li>
              <li>3. PO and GRN update the ledger; invoices run the deterministic 3-way match.</li>
              <li>4. Anything that does not match lands in the exception queue below.</li>
            </ol>
          </div>
        </div>

        <div className="space-y-4">
          {error ? <p className="rounded-lg border border-rose-900 bg-rose-950/40 p-3 text-sm text-rose-300">{error}</p> : null}
          {notice ? <p className="rounded-lg border border-emerald-900 bg-emerald-950/40 p-3 text-sm text-emerald-300">{notice}</p> : null}

          {summary ? (
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
              {[
                ['Files', summary.files],
                ['POs', summary.purchase_orders],
                ['GRNs', summary.goods_receipts],
                ['Approved', summary.matched],
                ['To review', (summary.review || 0) + (summary.exceptions || 0)],
                ['Rejected', summary.rejected],
              ].map(([label, value]) => (
                <div key={label} className="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2">
                  <div className="text-lg font-semibold text-slate-100">{value ?? 0}</div>
                  <div className="text-[11px] uppercase tracking-wide text-slate-500">{label}</div>
                </div>
              ))}
            </div>
          ) : null}

          <div className="rounded-2xl border border-slate-800 bg-slate-900/60">
            <div className="flex items-center justify-between border-b border-slate-800 px-4 py-3">
              <h3 className="text-sm font-semibold text-slate-200">2. What we read</h3>
              <span className="text-[11px] text-slate-500">
                {ingest?.documents?.length ? `${ingest.documents.length} document(s)` : 'no upload yet'}
              </span>
            </div>
            <div className="space-y-3 p-4">
              {ingest?.documents?.length ? (
                ingest.documents.map((doc) => (
                  <DocumentCard key={`${doc.file_name}-${doc.match?.match_result_id || doc.action}`} doc={doc} onOpenMatch={openDetail} />
                ))
              ) : (
                <p className="text-sm text-slate-500">
                  Add the PO, GRN and invoice for one purchase to the queue, then press Process all. Recognition, import
                  and matching then run file by file.
                </p>
              )}
            </div>
          </div>
        </div>
      </div>

      <div className="grid gap-5 xl:grid-cols-[380px_minmax(0,1fr)]">
        <div className="space-y-4">
          <div className="rounded-2xl border border-slate-800 bg-slate-900/60">
            <div className="flex items-center justify-between border-b border-slate-800 px-4 py-3">
              <h3 className="text-sm font-semibold text-slate-200">3. Exception queue</h3>
              <label className="flex items-center gap-1.5 text-[11px] text-slate-500">
                <input type="checkbox" checked={showResolved} onChange={(event) => setShowResolved(event.target.checked)} />
                include resolved
              </label>
            </div>
            <div className="max-h-[360px] overflow-y-auto">
              {queue.length === 0 ? (
                <p className="p-4 text-sm text-slate-500">Nothing waiting.</p>
              ) : (
                queue.map((item) => (
                  <button
                    key={item.id}
                    onClick={() => openDetail(item.id)}
                    className={`flex w-full flex-col gap-1 border-b border-slate-800/60 px-4 py-3 text-left transition hover:bg-slate-800/40 ${
                      selectedId === item.id ? 'bg-slate-800/60' : ''
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
                      {item.reason_codes.slice(0, 3).map((code) => (
                        <Badge key={code} className={STATUS_STYLES.EXCEPTION}>
                          {reasonLabel(code)}
                        </Badge>
                      ))}
                      {item.resolution ? <Badge className={APPROVAL_STYLES.PENDING}>{reasonLabel(item.resolution)}</Badge> : null}
                    </div>
                  </button>
                ))
              )}
            </div>
          </div>

          <div className="rounded-2xl border border-slate-800 bg-slate-900/60">
            <div className="border-b border-slate-800 px-4 py-3">
              <h3 className="text-sm font-semibold text-slate-200">Matched / rejected</h3>
            </div>
            <div className="max-h-[260px] overflow-y-auto">
              {cleanMatches.length === 0 ? (
                <p className="p-4 text-sm text-slate-500">No matched invoices yet.</p>
              ) : (
                cleanMatches.map((item) => (
                  <button
                    key={item.id}
                    onClick={() => openDetail(item.id)}
                    className="flex w-full items-center justify-between gap-2 border-b border-slate-800/60 px-4 py-2.5 text-left hover:bg-slate-800/40"
                  >
                    <div>
                      <div className="text-sm text-slate-200">{item.invoice_number || '—'}</div>
                      <div className="text-xs text-slate-500">{item.vendor || '—'}</div>
                    </div>
                    <div className="text-right">
                      <Badge className={STATUS_STYLES[item.status]}>{item.status}</Badge>
                      <div className="mt-1 text-xs text-slate-400">{money(item.grand_total, item.currency)}</div>
                    </div>
                  </button>
                ))
              )}
            </div>
          </div>
        </div>

        <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-5">
          {detail ? (
            <DetailPanel detail={detail} onAction={handleAction} onRefresh={() => openDetail(selectedId)} busy={busy} />
          ) : (
            <div className="flex h-full min-h-[320px] flex-col items-center justify-center gap-2 text-center">
              <p className="text-sm font-semibold text-slate-300">Select an exception to review it</p>
              <p className="max-w-md text-xs text-slate-500">
                The side-by-side view highlights the exact PO / GRN / invoice fields that disagree. Actions here feed the
                audit log and the approval chain.
              </p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
