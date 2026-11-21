# Document OCR integration

Accounts-payable 3-way matching (PO ↔ GRN ↔ invoice) on top of an existing OCR engine: EasyOCR spatial cell-grid extraction, AI fallback (Claude, then Gemini) only when local confidence is low, a canonical schema, a normalisation layer and a fully deterministic match engine, served behind a **React/Vite frontend**.

## Stack

- **Backend**: `backend/` — FastAPI + SQLAlchemy + their `services/*` (EasyOCR full-page OCR, 2D cell-grid table engine, geometry template engine, validator). Claude wired in as the primary AI fallback.
- **Frontend**: `client/` — React + Vite, proxies `/api` to the API port (`API_PORT`, default `8000`).
- **DB**: Postgres — their schema (`invoice_templates`, `extraction_logs`, `extraction_reviews`), auto-created on startup. SQLite `templates.db` used only if Postgres is unavailable.
- **Compatibility layer**: `backend/adapter.py` exposes our frontend's contract (`/api/extractions`, `/api/templates`, `/api/extractions/:id/corrections`, `/api/health`) on top of their native `/api/extract-invoice` pipeline.

## Repository layout

- `backend/` — FastAPI app + services (`adapter.py`, `main.py`, `services/*`) — the API served on :8000 (uvicorn).
- `client/` — React/Vite frontend (dev server :5173, proxies `/api` → `:8000`).
- `testing/` — sample invoices / PDFs used for manual checks.
- `requirements.txt` / `package.json` — Python and Node dependencies.

## Run locally

Use Python 3.12–3.15 (verified on 3.13 and 3.15). PaddleOCR is unavailable on
Python >= 3.13 — EasyOCR is the OCR engine there and is included in `requirements.txt`.
For the test suite install `requirements-dev.txt` as well.

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
npm install
cp .env.example .env        # set DATABASE_URL, ANTHROPIC_API_KEY, GEMINI_API_KEY
npm run dev                 # concurrently starts uvicorn (:8000) + vite (:5173)
```

Open **http://localhost:5173** (UI) and **http://localhost:8000/docs** (API docs).
The backend auto-creates its tables in Postgres on startup (`db:init` script also does this).

If 8000/5173 are already used by another local service, either free them or move this app:

```bash
npm run dev:alt                          # API :8010, UI :5180
API_PORT=8020 WEB_PORT=5190 npm run dev  # any pair
```

The Vite proxy follows `API_PORT` automatically (override with `API_TARGET` if needed).
Run the pieces separately when you want two terminals:

```bash
./venv/bin/python main.py --reload --port 8010             # API only
API_PORT=8010 WEB_PORT=5180 npm run dev:web                # UI only, proxying :8010
```

### Running the API directly

`backend/main.py` is a **package module** (it uses relative imports), so `python backend/main.py`
cannot work — run it through one of these instead:

```bash
./venv/bin/uvicorn backend.main:app --reload --port 8000   # canonical
./venv/bin/python main.py --reload                        # thin launcher at the repo root
./venv/bin/python -m backend.main                         # module form
./venv/bin/python -m backend                              # package form
```

`main.py` accepts `--host`, `--port` and `--reload` (defaults: `127.0.0.1:8000`).

### Worker (optional, for heavy OCR volume)

The API can do the work inline, but the queue is there for when it should not:

```bash
./venv/bin/python -m backend.worker --watch        # poll and process jobs
./venv/bin/python -m backend.worker --once         # one job then exit
```

Queue depth is visible at `GET /api/jobs`.

## Extraction flow

1. Upload a PDF/image; choose **Automatic**, **AI-assisted**, **Local OCR only**, or **PDFPlumber**.
2. The page is OCR'd (EasyOCR) and matched against a learned geometry template (`invoice_templates`). Only a **TRUSTED** template can take the auto-accept fast path; a candidate template may still extract but its result always goes to review.
3. On a miss (or a weak result) the 2D cell-grid engine extracts line items; weak/flagged output routes to **Claude vision** (primary), then **Gemini** (backup).
4. The result goes through the **validation gate** (arithmetic + format). Failures are held; passes continue to normalization and matching.
5. Local extractions may learn a **CANDIDATE** template. An AI extraction never saves one — it stores a *proposal* that a human confirms in the Review queue.

`DATABASE_URL` is required for templates/history. Configure `ANTHROPIC_API_KEY` (primary) and `GEMINI_API_KEY` (fallback). Without keys, local extraction still works.

### Scanned images: which engine actually reads them

The three extractors in this repo measure very differently depending on the input, so the routing is
per input type and is backed by the sample kit rather than by preference:

| Engine | Digital PDFs | Scanned images |
| --- | --- | --- |
| this repo (`backend/`) | **25/40 — 62.5%** | 0/40 — 0% |
| `invoice-ocr-c++` | 9/40 — 22.5% | **2/40 — 5%** |
| `invoice-ocr-line-system` | 9/40 — 22.5% | **2/40 — 5%** |

So:

- **Scanned images → the two upstream engines, best-of, then this repo's engine as fallback.**
- **Digital PDFs → this repo's extractor, unchanged.**

The upstream engines run as HTTP sidecars and are never modified — only their public
`POST /api/extract-invoice` endpoint is called, and the response is mapped onto our schema
(`backend/services/external_ocr.py`). Routing lives in `backend/services/ocr_router.py`; if the sidecars are
not running, images degrade silently to the local engine instead of failing.

```bash
npm run dev:ocr     # both sidecars: c++ :8021, line-system :8022 (state in .sidecars/)
npm run dev:all     # sidecars + API + UI
```

> **The interpreter is part of the measurement.** Run both sidecars with
> `invoice-ocr-c++/venv/bin/python` (Python 3.13 + their pinned deps). On this repo's Python 3.15 environment
> the same code scores 0/40, so the launcher hard-codes that interpreter.

Sidecar health is reported at `GET /api/threeway/config` under `ocr_routing.services`, and the scored eval uses
the same router:

```bash
python tests/ap_samples/run_eval.py --mode scanned   # prints the engine that served each file
```


---

# 3-Way Matching (PO ↔ GRN ↔ Invoice)

OCR is only the input. Everything after it — canonical schema, normalization, the
deterministic match engine, the exception queue, approvals and payment export —
lives in the modules below. **The match decision never uses an LLM.**

| Concern | Module |
| --- | --- |
| Thresholds, tolerances, approval policy, storage config | `backend/config.py` |
| Canonical Invoice / PO / GRN contract + OCR adapter | `backend/services/canonical.py` |
| Vendor resolution, unit/item-code normalization, invoice→PO line mapping | `backend/services/normalization.py` |
| Pure deterministic engine (MATCHED / EXCEPTION / REJECT + reason codes) | `backend/services/matching_engine.py` |
| DB orchestration: imports, balances, approvals, audit, metrics, export | `backend/services/matching_service.py` |
| Advisory LLM helpers (explain, vendor email, fuzzy mapping suggestions) | `backend/services/llm_assist.py` |
| Preview/source object storage (local disk or S3/MinIO) | `backend/services/storage.py` |
| Scanned-image routing to the upstream OCR engines | `backend/services/ocr_router.py`, `backend/services/external_ocr.py` |
| Scanned GRN table reader (rule/cell based, no AI) | `backend/services/grn_table_reader.py`, `backend/services/grn_lines.py` |
| Validation gate (arithmetic + format checks) | `backend/services/validation_gate.py` |
| Approve / review / reject decision | `backend/services/decision.py` |
| Shadow-mode ledger and per-vendor agreement | `backend/services/shadow.py` |
| Manual / spreadsheet goods-receipt intake | `backend/services/grn_intake.py` |
| Durable job queue + stateless worker | `backend/jobs.py`, `backend/worker.py`, `backend/pipeline/` |
| HTTP surface | `backend/api/threeway.py` |

## OCR quick wins (Step 0)

- `QUARANTINED` templates are filtered inside `find_matching_template`, so a bad template can never become a candidate.
- Every threshold (0.98 / 0.70 / 0.85) lives in `backend/config.py` (`ESCALATION`); every extraction logs the band that fired into `extraction_logs.threshold_decision`.
- `layout_match_score` stores the real computed composite score (no more `1.0`/`0.92` placeholders).
- Previews and source documents go to object storage; the DB keeps only the storage key (`/api/files/<key>` serves the bytes).

## Matching rules

1. **Header** — vendor resolved from the vendor master (GSTIN first, then fuzzy name), PO exists and is `OPEN`, currency matches, and invoice vendor equals PO vendor. Duplicate-invoice detection is not enabled; an invoice re-uploaded with the same vendor, number and date updates the existing document.
2. **Lines** — invoice qty ≤ GRN received qty ≤ PO ordered qty, rate within tolerance, line arithmetic consistent, tax rate consistent with the PO line. Lines are mapped deterministically: item code → fuzzy description → rate + quantity.
3. **Totals** — tax recomputed from lines, CGST+SGST+IGST reconciled, grand total within tolerance, cumulative invoiced value not above PO value.
4. **Partial deliveries** — each PO line keeps a balance (`po_line_balances`: received / invoiced / credited), so only `received − already invoiced` can be billed.

Outcome: `MATCHED`, `EXCEPTION(reason_codes[])`, or `REJECT` (hard blocks such as unknown vendor, missing/closed PO, currency mismatch).

## Validation gate, decisions and trust

Extraction quality is judged by **arithmetic and format**, never by OCR confidence: a layout can be read
confidently and still be wrong, and a wrong number is what reaches a payment.

`backend/services/validation_gate.py` runs on every document:

| Check | Blocks? |
| --- | --- |
| GSTIN format **and check digit** (mod-36) | yes |
| Line amounts sum to the taxable total (net or gross basis) | yes |
| Tax recomputes from line tax amounts/rates, including per-component CGST/SGST rates | yes |
| CGST + SGST + IGST agrees with the stated tax | yes |
| Grand total = taxable + tax (rounding only is a warning) | yes |
| Implied tax rate within the GST ceiling | yes |
| Invoice number, date and PO-reference format | warning unless policy makes them required |
| Quantity × rate explains each line amount | warning |

**Only documents that pass reach the match engine.** A failure is recorded with the failed checks as its reason
codes and lands in the review queue instead. PO/GRN header-total problems are warnings (the lines are what
matching uses); a PO/GRN that is unusable as goods data still fails.

`backend/services/decision.py` turns the gate verdict plus the match result into one of:

- **APPROVE** — validated, vendor known, PO open, every check clean, *and* the vendor has earned auto-approval.
- **REJECT** — hard failures only: an unverifiable GSTIN, a PO that does not exist.
- **REVIEW** — everything else. When unsure, a human looks at it.

### Template trust

A learned layout is created as **CANDIDATE** and only becomes **TRUSTED** after
`TEMPLATE_CANDIDATE_CONFIRMATIONS` (default 3) clean human confirmations. AI extractions **never** save a template
on their own: the geometry is stored beside the extraction as a proposal, and `/api/templates/proposals/{id}/confirm`
materialises it after a human agrees. Only a TRUSTED template can take the auto-accept fast path. Legacy
`DRAFT`/`ACTIVE` statuses still read correctly and are mapped onto candidate/trusted; auto-trusted legacy rows are
demoted back to candidate by the schema migration.

### Shadow mode

`SHADOW_MODE=shadow` (the default) sends everything to a human while the engine's recommendation is recorded in
`decision_observations`. Agreement per vendor is measured from real decisions (`/api/shadow/agreement`), and
`SHADOW_MODE=live` auto-approves only vendors with `SHADOW_MIN_DECISIONS` decisions at `SHADOW_MIN_AGREEMENT`
agreement. A vendor with no history stays in review even in live mode.

### Receipts without OCR

GRNs are the weakest OCR input — often handwritten or just a stamp. For customers without an ERP the store person
enters received quantities against the PO they already have:

- `GET /api/grns/entry/{po_number}` — the PO lines with ordered / already-received / remaining quantities.
- `POST /api/grns/manual` — `{po_number, receipt_date?, lines:[{po_line_no|item_code, received_qty}]}`.
- `POST /api/grns/manual/import` — a CSV/TSV (or XLSX when `openpyxl` is installed) sheet, one row per PO line.

Both produce the same canonical GRN the OCR path produces, so the match engine cannot tell the difference.

## Architecture: modular monolith + workers

One codebase, one deployable, explicit internal seams (`backend/pipeline/__init__.py` documents the stage map):

| Stage | Module |
| --- | --- |
| intake | `services/storage.py`, `jobs.py` |
| extraction | `main.py` (OCR pipeline), `services/template_engine.py` |
| validation | `services/validation_gate.py` |
| normalization | `services/normalization.py`, `services/canonical.py` |
| matching | `services/matching_engine.py` (pure) driven by `services/matching_service.py` |
| review | `services/matching_service.py`, `services/decision.py`, `services/shadow.py` |
| approval | `services/matching_service.py` |
| export | `services/matching_service.py` |

Heavy work goes through a durable queue (`jobs` table) so the API stays fast and OCR can be scaled separately:

```bash
python -m backend.worker --watch        # stateless worker; run as many as you need
python -m backend.worker --once         # drain one job
python -m backend.worker --kinds INGEST_DOCUMENT
```

`backend/pipeline/jobs_handlers.py` registers `INGEST_DOCUMENT` and `RUN_MATCH`. Claiming uses a conditional
`UPDATE … WHERE status='QUEUED'`, so two workers can never take the same job. Every domain row carries
`tenant_id` (from `TENANT_ID`) from day one, and every import, match, exception action, approval and review
decision writes to `audit_logs`.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET/POST | `/api/vendors`, `/api/vendors/import` | Vendor master (GSTIN authoritative) |
| GET/POST | `/api/purchase-orders`, `/api/purchase-orders/import`, `/api/purchase-orders/bulk` | PO import (CSV/JSON file, JSON array, or single PO; see `testing/sample_po.csv`) |
| GET/POST | `/api/grns`, `/api/grns/import` | GRN import (JSON or CSV, `testing/sample_grn.csv`) |
| POST | `/api/documents/ingest` | **Single upload box**: multi-file upload, auto-detect PO/GRN/invoice, import POs/GRNs and match invoices in one call (form fields: `files`, `extraction_method`, `po_number`) |
| POST | `/api/extractions/{id}/canonicalize` | Validate OCR output against the canonical schema |
| POST | `/api/extractions/{id}/match` | Canonicalize + run the 3-way match (`?po_number=` optional) |
| POST | `/api/match` | Match a canonical invoice supplied as JSON |
| GET | `/api/matches`, `/api/matches/{id}` | Match history and side-by-side detail |
| GET | `/api/exceptions` | Exception queue (`?reason=`, `?include_resolved=true`) |
| POST | `/api/matches/{id}/actions` | `ACCEPT_OVERRIDE` (reason required), `CREDIT_NOTE`, `HOLD`, `REJECT` |
| GET | `/api/approvals`, POST `/api/matches/{id}/approve` | Approval queue + decisions |
| GET | `/api/payments/export.csv`, `/api/payments/tally.xml` | Payment-ready export |
| GET/POST | `/api/grns/entry/{po_number}`, `/api/grns/manual`, `/api/grns/manual/import` | **Manual receipts** for customers without an ERP: load PO lines, enter quantities, or upload a sheet |
| GET | `/api/review/queue`, `/api/review/queue/{id}/validation` | Everything sent to a human, with the system's recommendation and the full check list |
| POST | `/api/review/queue/{id}/decision` | `APPROVE` / `REVIEW` / `REJECT` by a named actor; recorded in the shadow ledger |
| GET | `/api/templates/candidates`, POST `/api/templates/{id}/confirm`, `/api/templates/proposals/{id}/confirm` | Candidate layouts and AI proposals awaiting a human |
| GET | `/api/shadow/agreement`, `/api/vendors/{id}/agreement` | Observed agreement per vendor (the auto-approval evidence) |
| GET | `/api/jobs` | Queue depth, pending by kind, recent jobs |
| GET | `/api/metrics` | STP %, exception rate by reason, cycle time, template hit rate, AI fallback share, validation failure rate |
| GET | `/api/threeway/config` | Active thresholds, tolerances and approval rules |
| POST | `/api/threeway/seed-demo` | Load a demo vendor/PO/GRN + one clean and one exception invoice |

Every PO/GRN import, match run, exception action and approval writes to `audit_logs` (who / what / when / before / after).

## Frontend

Five sections:

- **Documents** — a single drop zone for any mix of PO / GRN / invoice files. Each file is OCR'd (AI only when local confidence is low), classified from its own content, then imported into the PO/GRN ledger or matched as an invoice. The response is shown per file (detected type, confidence, action, verdict), followed by the exception queue, matched invoices, and the side-by-side PO/GRN/invoice detail with mismatched fields highlighted, override/credit-note/hold/reject actions, source document and advisory LLM explain/email/mapping help.
- **Review** — the human queue: the system's recommendation beside your decision, the full validation check list, decision reasons, the side-by-side comparison, and the layouts waiting to be confirmed.
- **Receipts** — record a goods receipt by hand against a PO (or upload a sheet). This replaces scanning GRNs for customers without an ERP.
- **Approvals** — pending approvals with required roles, segregation of duties, and payment CSV/Tally export.
- **Metrics** — straight-through rate, exception rate by reason, cycle time, value processed, template hit rate, AI fallback share, validation failure rate, live policy.

There is no separate invoice-OCR screen: extraction is an implementation detail of the ingest step.

## Tests

```bash
pytest tests -q
```

`tests/test_matching_engine.py` holds the ~30 hand-built scenarios (partial delivery, tax mismatch, over-billing, tolerance edges, mapping ladder); `tests/test_matching_service.py` covers balances, exception/approval workflow, exports and metrics on an in-memory database. `tests/test_governance.py` covers the GSTIN checksum, every gate check (on deliberately broken arithmetic), the approve/review/reject rules, template trust promotion, shadow agreement and manual receipt intake; `tests/test_review_api.py` exercises the review and receipt endpoints over HTTP.

## Pilot path

1. Import the vendor master and one or two vendors' real POs (`/api/purchase-orders/import`), and record their receipts either by OCR (`/api/documents/ingest`) or by hand (`/api/grns/manual`) — prefer the manual path where the customer has no ERP.
2. Drop their invoices into the Documents page; extraction is validated and matched automatically.
3. Work the **Review** queue. Keep `SHADOW_MODE=shadow` for the first weeks and watch `/api/shadow/agreement`: the recommendation and the human decision should agree per vendor before you switch any vendor to live auto-approval.
4. Confirm layout proposals in the Review queue; a layout becomes trusted only after repeated clean confirmations.
5. Use `/api/metrics` (or the Metrics tab) to tune `TOLERANCE_OVERRIDES` / per-vendor tolerances with real exception data, and watch template hit rate + AI fallback share to see extraction improving.

## Evaluation on the AP sample set

`tests/ap_samples/` is an AP 3-way-match eval kit: three scenarios (S1 matched, S2 exception, S3 exception) as clean PDFs, degraded scans, ground-truth JSON and `manifest.json` with the expected outcome.

```bash
python tests/ap_samples/run_eval.py --mode truth      # matching engine on ground truth (no OCR)
python tests/ap_samples/run_eval.py --mode pdf        # digital-PDF extraction accuracy vs truth
python tests/ap_samples/run_eval.py --mode scanned    # scanned-image accuracy through the router (sidecars recommended)
python tests/ap_samples/run_eval.py --mode all -v
pytest tests/test_ap_samples_eval.py -v               # same scenarios as regression tests
```

Expected outcomes encoded in the harness (from `manifest.json`):

| Scenario | Expected | What is asserted |
| --- | --- | --- |
| S1 | `MATCHED` | no reason codes; then approvals → payment export |
| S2 | `EXCEPTION` | `RATE_TOLERANCE_EXCEEDED` on line 1 (partial delivery is valid), `QTY_EXCEEDS_GRN` on line 3 |
| S3 | `EXCEPTION` | `QTY_EXCEEDS_GRN` on line 3 |

GRN quantities use the **accepted** quantity (`accepted_qty`), so goods rejected at the gate never become payable.

Current measured accuracy:

| Mode | Score | Notes |
| --- | --- | --- |
| truth | 3/3 scenarios | the match engine itself |
| pdf | 25/40 fields (62.5%) | this repo's extractor |
| scanned | 2/40 fields (5.0%) | via the sidecar router; 0/40 with the local engine only |

The scanned number is low because the sample scans are deliberately degraded. It is reported so the ceiling is
visible, not to look good: the validation gate exists precisely because extraction will miss fields.
