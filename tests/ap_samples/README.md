# AP Automation sample set (Indian MSME, 3-way match)

All parties, GSTINs, PANs, bank accounts and phone numbers are FICTIONAL.
GSTINs carry a valid checksum so format validators pass. GST rates are illustrative.
HSN/GST classification is for test purposes, not tax advice.

## Folders
- pdf/       clean digital PDFs (text layer present)
- scanned/   same documents degraded to look like phone/scanner images (skew, blur, noise, JPEG)
- truth/     ground-truth JSON for every document (use to score OCR field accuracy)
- manifest.json  which documents belong together and the EXPECTED match result

## Template layouts (what to register as templates in your OCR)
| Layout   | Looks like                          | Used by         |
|----------|-------------------------------------|-----------------|
| INV-1    | Tally-style bordered, with IRN, HSN summary, CGST+SGST | Kaveri Fasteners (S1) |
| INV-2    | Modern, teal header, per-line GST, IGST | Shree Ganesh Packaging (S2) |
| INV-3    | Small-vendor minimal bill, mixed GST rates | Annapoorna Traders (S3) |
| PO-A     | Formal bordered PO                  | Sri Venkateswara Auto (S1, S2) |
| PO-B     | Compact "P.O." label:value style    | Lakshmi Castings (S3) |
| GRN-A    | Full GRN with PO/challan/received/accepted/rejected | S1, S2 |
| GRN-B    | "Material Receipt Report", minimal  | S3 |

Note: PO and GRN are buyer-specific, invoices are vendor-specific. In production
you need one template per (buyer, doc type) and per (vendor, invoice).

## Scenarios and expected outcome
- S1  MATCHED. Clean match, intra-state (CGST+SGST).
- S2  EXCEPTION. Partial delivery on line 1 (valid), rate +4.4% on line 1,
      invoice qty 600 vs GRN accepted 540 on line 3. Inter-state (IGST).
- S3  EXCEPTION. Invoice qty 10 vs GRN accepted 8 on line 3. Mixed 18%/5% GST.
      inv_S3_duplicate must be flagged DUPLICATE_INVOICE.

## Suggested tests
1. OCR accuracy: run pdf/ then scanned/, diff against truth/ per field.
2. Template reuse: register S1 docs as templates, confirm S2/S3 do NOT match them
   (different layouts), and that re-running S1 scans matches with high confidence.
3. Matching engine: feed truth/ JSON directly (skip OCR) and check manifest expected results.
4. Regenerate more variants by editing gen.py (vendors, rates, tolerances).
