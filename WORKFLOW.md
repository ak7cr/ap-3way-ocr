# How the AP 3-Way Matching Workflow Works

This document describes the workflow currently implemented in this repository. It
covers what happens after a user uploads documents, from OCR through review and
payment export. The matching decision itself is deterministic; AI is used for
document extraction or classification when needed, not to decide whether an
invoice should be paid.

## At a glance

```text
Upload PO, GRN and/or invoice
        |
        v
Read document -> classify its type -> validate extraction
        |
        +-- PO --------------------> import PO and its lines
        |
        +-- GRN -------------------> record receipt quantities against a PO
        |
        +-- Invoice --------------> validation gate
                                     |
                                     +-- fails -> hold for review; do not match
                                     |
                                     +-- passes -> normalize -> deterministic match
                                                    |
                                                    +-- matched / exception / reject
                                                            |
                                                            v
                                                   decision + approval workflow
                                                            |
                                                            v
                                                   approved payment export
```

## 1. Upload and extraction

The **Documents** page accepts one or more PDFs or images. Files can be added
one at a time or all at once; they wait in an on-page queue and are sent
together only when the user presses **Process all**. A user can upload a PO, a
goods receipt note (GRN), an invoice, or a mix of them. The browser then sends
the queued files to `POST /api/documents/ingest`, which processes them one by
one.

For image uploads, the backend first runs a document-agnostic Tesseract text pass
so document type can be recognized independently of invoice-field extraction.
It then runs the structured extraction pipeline and records extraction
metadata, including each OCR attempt, engine, confidence, and extraction log
where available. In automatic mode, the extraction engine may use AI fallback
when its local result is weak. AI-assisted and local-only modes are also
available in the UI.

For scanned images, the document-text pass uses the installed Tesseract
executable when available. The structured OCR router then tries the configured
external OCR sidecars, chooses the best usable result, and falls back to this
repository's local image extractor. If that result still needs help and the
extraction mode permits it, the normal AI fallback can run. When AI-only mode is
selected, the vision model runs directly; its result still carries the page's OCR
text, preview and an extraction-log row, so it is audited like every other
extraction. For PDFs the document-text pass uses the embedded text layer, and a
PDF with no usable text layer (a scan saved as PDF) has its first page
rasterised and OCR'd, so it is never treated as empty. Digital PDFs stay on this
repository's PDF extraction path. Tesseract and the sidecars are optional; their
successes or failures are included in the document result and extraction log.

The original upload is written to the configured object-storage backend when
available. The database stores its storage key rather than the image bytes.

## 2. Document-type recognition

The backend classifies the extracted text as a purchase order, GRN, invoice, or
unknown document. It uses document wording and structure, and extracts
identifiers such as the PO number, invoice/GRN number, dates, vendor details and
GSTINs where possible. If the classification is weak or ambiguous, the backend
can ask the LLM classification helper for a type.

If a document remains unclassified, it is returned for the user to check; it is
not imported or matched.

## 3. PO and GRN intake

### Purchase orders

A recognized PO with readable lines is converted to the internal PO/line
records. The PO carries its vendor, currency, dates, item codes, quantities,
units, rates and tax information where extraction supplied them. The supplier is
read from the line after its label rather than from the label cell, and an HSN
number is stored as HSN, not as the item code. POs can also be imported from CSV
or JSON.

### Goods receipts

A recognized GRN must reference a PO and have readable lines. Its received
quantities are recorded against that PO. For supported scanned GRNs, the
backend attempts to read the accepted-quantity column rather than assuming the
first quantity column is the received amount.

For GRN images, and for PDF GRNs whose first page is rasterised, the backend
first runs a table-aware reader (`grn_table_reader.py`). It deskews the page using the table's horizontal rules,
removes the rules so they do not cut through the digits during OCR, reads the
header row to learn which field each column holds, and then treats every box
between two rules as one cell. When the vertical rules are missing it falls back
to assigning numbers to the nearest header column. This handles both the full
"Goods Receipt Note" layout and the minimal "Material Receipt Report" layout in
`tests/ap_samples`.

The reader's output is then checked deterministically before it is booked:

- accepted plus rejected must equal received;
- cumulative received must not exceed what is still open on the PO line;
- every line must map to a PO line. Item codes are matched exactly first, then
  fuzzily (about 0.85 similarity) so OCR slips such as `ALN-SETS` for `ALN-SET9`
  still resolve; a near-tie between two PO lines is left unmapped.

If no lines are found, a check fails, the header row cannot be read, or a line
does not map, the document goes to the review queue, and the vision model may be
asked to extract the table when AI is configured. The same checks run on the
model's output. A single misread digit in the Accepted column is re-read on its
own and kept only if accepted plus rejected still equals received.

Re-uploading a receipt replaces the earlier one: the GRN number is compared after
stripping punctuation, and PO line balances are rebuilt from the stored receipts
rather than adjusted incrementally. A second upload therefore cannot double-count
or appear to over-receive. Item codes are compared with look-alike tolerance
(I/1, O/0, S/5, B/8) and a PO line's item code is never an HSN number; the HSN is
kept in its own field. GRNs can also be entered manually or imported from a
spreadsheet, which is useful when receipts are handwritten or ERP data is
available.

PO/GRN extraction validation is reported to the user, but invoice arithmetic
checks such as grand-total reconciliation are not applied as hard blockers to
these document types.

## 4. Invoice validation gate

Invoices go through a deterministic validation gate before the matching engine.
The gate checks that the extraction has usable lines and evaluates applicable
format and arithmetic rules, including:

- GSTIN format and checksum;
- invoice number and date where required;
- PO reference format;
- line amounts against quantity and rate;
- line totals against taxable value;
- tax recomputation and CGST/SGST/IGST breakdown;
- taxable value plus tax against the invoice grand total, allowing configured
  rounding tolerances.

If the model returns an invoice number that does not appear in the page text,
the number read from the OCR text replaces it. A PO reference printed on its own
line (for example below "Buyer's Order No") is still found by a fallback token
match. The gate uses the normalized/canonical invoice data where available, so
obvious representation differences (for example tax-inclusive line amounts)
can be reconciled before the checks are applied. If the invoice fails the gate,
it is held in the review workflow and does **not** go through the 3-way match.

## 5. Normalization and deterministic matching

After validation passes, the invoice is converted to the canonical shape and
normalized. Vendor resolution prefers GSTIN, then uses vendor-name matching.
Line mapping uses item codes first, then descriptions and other deterministic
signals. Units, quantities, rates and amounts are normalized for comparison.

The match engine checks:

- that the vendor and PO are consistent and the PO is open;
- invoice lines against PO lines;
- invoice quantity against received GRN quantity and the remaining PO balance;
- rates and totals against configured tolerances;
- tax and total calculations.

The engine returns `MATCHED`, `EXCEPTION`, or `REJECT`, with reason codes and
line-level findings. It does not use an LLM to make this decision. The PO line
balance tracks received, invoiced and credited quantities so repeat/partial
deliveries can be considered. Duplicate-invoice detection is not enabled;
instead, the same invoice uploaded again is recognised by vendor, number and
date and updates the existing record, so a re-upload cannot bill the purchase
order twice.

## 6. Recommendation, review and exception handling

The system makes a separate `APPROVE`, `REVIEW`, or `REJECT` recommendation:

- **Approve** requires a valid extraction, known vendor, open PO and clean match.
  Automatic approval is additionally gated by shadow-mode policy and the
  vendor's observed agreement history.
- **Reject** is reserved for configured hard failures, such as an invalid GSTIN
  or a missing/closed PO.
- **Review** is the safe outcome for unresolved or uncertain cases.

In shadow mode, the recommendation is recorded beside the human decision, and
agreement is measured per vendor. Exceptions appear in the Review queue with
reason codes and a side-by-side PO/GRN/invoice view. The reviewer can accept an
override (with a required reason), request a credit-note resolution, hold, or
reject. Actions are recorded in the audit trail.

Approval rules can require additional approvers based on amount, vendor risk or
exception type. Approval decisions are separate from exception resolution, and
the payment export includes only invoices that have reached the approved state.

## 7. Export and metrics

The Approvals page provides payment CSV and Tally XML exports. Other ERP formats
are not currently generated by these exports.

The Metrics page reports operational measures such as straight-through rate,
exception/review outcomes, cycle time, template hit rate,
AI-fallback share, validation failures and shadow-mode agreement.

## 8. Supporting modules and current boundaries

- The application is organized as a modular backend, but document upload and
  extraction currently run inline in the ingest request. A durable job table
  and worker command exist for queued work; this upload endpoint does not itself
  enqueue each document for the worker.
- AI can assist with extraction, classification and explanatory tasks. The
  matching and approval policy remains deterministic.
- AI-extracted layouts are proposals for human confirmation. Local template
  learning is controlled by validation and template-trust settings; candidate
  templates do not qualify for the trusted-template fast path.
- Tenant identifiers exist on core records, but tenant-scoped authentication
  and isolation are not complete. The current default-tenant behavior should
  not be treated as production multi-tenant security.
- `tests/ap_samples` is the sample evaluation set used to compare extraction and
  matching behavior. The current Tesseract text-first pass classified all 10
  scanned sample documents correctly in a classification-only check; this is
  not a claim that every extracted field or table line is correct. Sample
  results do not guarantee accuracy on other vendors' documents.

## Main implementation locations

- Upload/classification/import flow: [`backend/services/ingest_service.py`](backend/services/ingest_service.py)
- Document classification: [`backend/services/doc_classifier.py`](backend/services/doc_classifier.py)
- OCR routing: [`backend/services/ocr_router.py`](backend/services/ocr_router.py)
- Scanned GRN table reader: [`backend/services/grn_table_reader.py`](backend/services/grn_table_reader.py), [`backend/services/grn_lines.py`](backend/services/grn_lines.py)
- Canonicalization and validation: [`backend/services/canonical.py`](backend/services/canonical.py), [`backend/services/validation_gate.py`](backend/services/validation_gate.py)
- Matching and workflow persistence: [`backend/services/matching_engine.py`](backend/services/matching_engine.py), [`backend/services/matching_service.py`](backend/services/matching_service.py)
- Decision and shadow-mode policy: [`backend/services/decision.py`](backend/services/decision.py), [`backend/services/shadow.py`](backend/services/shadow.py)
- Upload API: [`backend/api/threeway.py`](backend/api/threeway.py)
- Documents UI: [`client/src/components/DocumentsPage.jsx`](client/src/components/DocumentsPage.jsx)
