import json, os, hashlib, copy
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import ParagraphStyle

OUT = "/mnt/user-data/outputs/ap_samples"
os.makedirs(f"{OUT}/pdf", exist_ok=True)
os.makedirs(f"{OUT}/truth", exist_ok=True)

# ---------------------------------------------------------------- helpers
CH = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

def make_gstin(state, pan, entity="1"):
    g = f"{state}{pan}{entity}Z"
    total = 0
    for i, c in enumerate(g):
        v = CH.index(c) * (1 if i % 2 == 0 else 2)
        total += v // 36 + v % 36
    return g + CH[(36 - total % 36) % 36]

def inr(x):
    s = f"{x:.2f}"
    ip, dp = s.split(".")
    neg = ip.startswith("-")
    ip = ip.lstrip("-")
    if len(ip) > 3:
        head, tail = ip[:-3], ip[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:]); head = head[:-2]
        if head: parts.insert(0, head)
        ip = ",".join(parts + [tail])
    return ("-" if neg else "") + ip + "." + dp

ONES = "Zero One Two Three Four Five Six Seven Eight Nine Ten Eleven Twelve Thirteen Fourteen Fifteen Sixteen Seventeen Eighteen Nineteen".split()
TENS = "_ _ Twenty Thirty Forty Fifty Sixty Seventy Eighty Ninety".split()

def _w(n):
    if n < 20: return ONES[n]
    if n < 100: return TENS[n // 10] + (" " + ONES[n % 10] if n % 10 else "")
    if n < 1000: return ONES[n // 100] + " Hundred" + (" " + _w(n % 100) if n % 100 else "")
    if n < 100000: return _w(n // 1000) + " Thousand" + (" " + _w(n % 1000) if n % 1000 else "")
    if n < 10000000: return _w(n // 100000) + " Lakh" + (" " + _w(n % 100000) if n % 100000 else "")
    return _w(n // 10000000) + " Crore" + (" " + _w(n % 10000000) if n % 10000000 else "")

def words(amount):
    r = int(amount); p = int(round((amount - r) * 100))
    s = "INR " + _w(r)
    if p: s += " and " + _w(p) + " Paise"
    return s + " Only"

def P(t, size=8, bold=False, align=0, color=colors.black):
    return Paragraph(str(t), ParagraphStyle("p", fontName="Helvetica-Bold" if bold else "Helvetica",
                     fontSize=size, leading=size + 2.2, alignment=align, textColor=color))

def kv(label, value, size=8):
    return P(f"<font size={size-2} color='#555555'>{label}</font><br/><b>{value}</b>", size)

def fdate(d):  # d = (dd, mm, yyyy)
    mons = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    return f"{d[0]:02d}-{mons[d[1]-1]}-{d[2]}"

def sdate(d):
    return f"{d[0]:02d}.{d[1]:02d}.{str(d[2])[2:]}"

def idate(d):
    return f"{d[0]:02d}/{d[1]:02d}/{d[2]}"

def iso(d):
    return f"{d[2]}-{d[1]:02d}-{d[0]:02d}"

def party_block(p, gst=True):
    s = f"<b>{p['name']}</b><br/>" + "<br/>".join(p["addr"])
    if gst: s += f"<br/>GSTIN/UIN: {p['gstin']}<br/>State Name: {p['state']}, Code: {p['code']}"
    return s

# ---------------------------------------------------------------- parties
def party(name, addr, state, code, pan, phone, email, bank=None):
    return dict(name=name, addr=addr, state=state, code=code, pan=pan, phone=phone, email=email,
                gstin=make_gstin(code, pan), bank=bank)

BUYER1 = party("Sri Venkateswara Auto Components Pvt Ltd",
               ["Plot No. 14, SIDCO Industrial Estate", "Ambattur, Chennai - 600 098"],
               "Tamil Nadu", "33", "AAECS4821K", "044-2625 1180", "purchase@svautocomp.example")
BUYER2 = party("Lakshmi Precision Castings",
               ["No. 7/112, Kurichi Industrial Park", "Coimbatore - 641 021"],
               "Tamil Nadu", "33", "AAFFL7733C", "0422-267 4410", "stores@lakshmicastings.example")
V1 = party("Kaveri Fasteners & Tools", ["22/3, Mint Street, Sowcarpet", "Chennai - 600 079"],
           "Tamil Nadu", "33", "AAHFK3390P", "044-2538 7712", "sales@kaverifasteners.example",
           bank=("Indian Bank", "Sowcarpet", "6012345678", "IDIB000S045"))
V2 = party("Shree Ganesh Packaging Industries", ["Gat No. 212, Chakan MIDC, Phase II", "Pune - 410 501"],
           "Maharashtra", "27", "AAMFS9054Q", "020-2712 3344", "accounts@sgpackaging.example",
           bank=("HDFC Bank", "Chakan", "50200012345678", "HDFC0001234"))
V3 = party("Annapoorna Traders", ["45, Avinashi Road", "Coimbatore - 641 018"],
           "Tamil Nadu", "33", "ACTPA6618L", "98430 11223", "annapoornatraders@example.in",
           bank=("Canara Bank", "Avinashi Rd", "0987101012345", "CNRB0000987"))

# ---------------------------------------------------------------- scenarios
def L(code, desc, hsn, qty, unit, rate, gst):
    return dict(code=code, desc=desc, hsn=hsn, qty=qty, unit=unit, rate=rate, gst=gst)

SCEN = {}

# S1: clean match
po1 = [L("BLT-M10-60", "Hex Bolt M10 x 60 mm, HT 8.8, Zinc Plated", "7318", 2000, "Nos", 6.50, 18),
       L("NUT-M10", "Hex Nut M10, Zinc Plated", "7318", 2000, "Nos", 2.10, 18),
       L("WSH-M10", "Spring Washer M10", "7318", 2000, "Nos", 0.95, 18),
       L("ALN-SET9", "Allen Key Set, 9 Pcs, Long Arm", "8204", 25, "Set", 185.00, 18)]
SCEN["S1"] = dict(
    buyer=BUYER1, vendor=V1, po_layout="A", grn_layout="A", inv_layout=1,
    po=dict(no="SVAC/PO/26-27/0412", date=(28, 10, 2026), delivery=(7, 11, 2026), terms="30 Days from date of invoice", lines=po1),
    grn=dict(no="GRN/26-27/0733", date=(7, 11, 2026), challan="DC-1187", challan_date=(6, 11, 2026), vehicle="TN 09 CE 4471",
             lines=[dict(l, ordered=l["qty"], challan=l["qty"], received=l["qty"], accepted=l["qty"], rejected=0, remarks="") for l in po1]),
    inv=dict(no="KFT/26-27/0891", date=(6, 11, 2026), lines=copy.deepcopy(po1), eway="241012398877"),
    expected=dict(status="MATCHED", reasons=[], note="Clean 3-way match. All qty/rate/tax agree. Use as the happy-path test."))

# S2: partial delivery + rate mismatch + over-billing vs GRN (inter-state IGST)
po2 = [L("CTN-5P-450", "Corrugated Box 5 Ply 450x300x250 mm, BF 18", "4819", 5000, "Nos", 38.50, 18),
       L("BUB-1M100", "Bubble Wrap Roll 1 m x 100 m", "3923", 40, "Roll", 920.00, 18),
       L("TAPE-B48", "BOPP Tape 48 mm x 65 m, Brown", "3919", 600, "Roll", 32.00, 18)]
inv2 = copy.deepcopy(po2)
inv2[0].update(qty=3000, rate=40.20)    # partial delivery billed, but rate +4.4% above PO
inv2[2].update(qty=600)                 # billed 600, GRN accepted only 540
grn2_rows = [dict(po2[0], ordered=5000, challan=3000, received=3000, accepted=3000, rejected=0, remarks="Balance 2000 to follow"),
             dict(po2[1], ordered=40, challan=40, received=40, accepted=40, rejected=0, remarks=""),
             dict(po2[2], ordered=600, challan=540, received=540, accepted=540, rejected=0, remarks="Short supply 60")]
SCEN["S2"] = dict(
    buyer=BUYER1, vendor=V2, po_layout="A", grn_layout="A", inv_layout=2,
    po=dict(no="SVAC/PO/26-27/0418", date=(30, 10, 2026), delivery=(10, 11, 2026), terms="45 Days from date of GRN", lines=po2),
    grn=dict(no="GRN/26-27/0741", date=(10, 11, 2026), challan="SGP/DC/2290", challan_date=(9, 11, 2026), vehicle="MH 14 HG 8820", lines=grn2_rows),
    inv=dict(no="SGP/2627/01184", date=(9, 11, 2026), lines=inv2, eway="271009884412"),
    expected=dict(status="EXCEPTION", reasons=["RATE_MISMATCH(line 1: invoice 40.20 vs PO 38.50, +4.4%)", "QTY_EXCEEDS_GRN(line 3: invoiced 600 vs accepted 540)"],
                  note="Line 1 partial delivery (3000 of 5000) is VALID; only the rate is wrong. IGST because vendor is Maharashtra and buyer is Tamil Nadu."))

# S3: small vendor, plain layout, over-billing (+ a reprinted copy for reference)
po3 = [L("LUB-SRV68", "Servo Hydraulic Oil 68, 210 L Drum", "2710", 4, "Drum", 21500.00, 18),
       L("GRS-EP2", "Lithium Grease EP2, 18 kg Pail", "2710", 6, "Pail", 3850.00, 18),
       L("CW-50", "Cotton Waste, 50 kg Bale", "6310", 10, "Bale", 1450.00, 5)]
grn3_rows = [dict(po3[0], ordered=4, challan=4, received=4, accepted=4, rejected=0, remarks=""),
             dict(po3[1], ordered=6, challan=6, received=6, accepted=6, rejected=0, remarks=""),
             dict(po3[2], ordered=10, challan=10, received=8, accepted=8, rejected=0, remarks="2 bales short at unloading")]
SCEN["S3"] = dict(
    buyer=BUYER2, vendor=V3, po_layout="B", grn_layout="B", inv_layout=3,
    po=dict(no="LPC/PO/0267", date=(3, 11, 2026), delivery=(12, 11, 2026), terms="Net 30 days", lines=po3),
    grn=dict(no="MRR-2026-0581", date=(12, 11, 2026), challan="AT/1042", challan_date=(12, 11, 2026), vehicle="TN 38 BQ 5109", lines=grn3_rows),
    inv=dict(no="AT/0376", date=(12, 11, 2026), lines=copy.deepcopy(po3), eway=None),
    expected=dict(status="EXCEPTION", reasons=["QTY_EXCEEDS_GRN(line 3: invoiced 10 vs accepted 8)"],
                  note="Mixed GST rates (18% and 5%). inv_S3_duplicate is the same invoice re-printed as 'Duplicate for Transporter' for reference; duplicate detection is not enabled."))

# ---------------------------------------------------------------- tax calc
def totals(lines, buyer, vendor):
    intra = buyer["code"] == vendor["code"]
    rows, groups = [], {}
    for i, l in enumerate(lines, 1):
        taxable = round(l["qty"] * l["rate"], 2)
        g = groups.setdefault((l["hsn"], l["gst"]), 0.0)
        groups[(l["hsn"], l["gst"])] = round(g + taxable, 2)
        rows.append(dict(l, sl=i, taxable=taxable))
    by_rate = {}
    for (hsn, r), t in groups.items():
        by_rate[r] = round(by_rate.get(r, 0) + t, 2)
    sub = round(sum(r["taxable"] for r in rows), 2)
    tax = {}
    for r, t in by_rate.items():
        if intra:
            h = round(t * r / 200, 2)
            tax[("CGST", r / 2)] = h; tax[("SGST", r / 2)] = h
        else:
            tax[("IGST", r)] = round(t * r / 100, 2)
    tax_total = round(sum(tax.values()), 2)
    gross = round(sub + tax_total, 2)
    total = float(round(gross))
    return dict(intra=intra, rows=rows, groups=groups, subtotal=sub, tax=tax, tax_total=tax_total,
                round_off=round(total - gross, 2), total=total)

# ---------------------------------------------------------------- generic doc builder
def build(path, story, title):
    SimpleDocTemplate(path, pagesize=A4, leftMargin=10*mm, rightMargin=10*mm, topMargin=10*mm,
                      bottomMargin=10*mm, title=title).build(story)

GRID = TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                   ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)])
BOX = TableStyle([("BOX", (0, 0), (-1, -1), 0.5, colors.black), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                  ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)])

def qty_s(q):
    return f"{q:,}" if isinstance(q, int) else f"{q}"

# ================================================================= INVOICE LAYOUT 1  (Tally-style, e-invoice)
def invoice_l1(path, s, label="Original for Recipient"):
    b, v, inv, po = s["buyer"], s["vendor"], s["inv"], s["po"]
    t = totals(inv["lines"], b, v)
    irn = hashlib.sha256(inv["no"].encode()).hexdigest()
    st = []
    st.append(P("<b>Tax Invoice</b>", 14, align=1))
    st.append(P(f"({label})", 7, align=1))
    st.append(Spacer(1, 2))
    st.append(P(f"IRN: {irn}<br/>Ack No.: {int(irn[:10],16)%10**15:015d} &nbsp;&nbsp; Ack Date: {fdate(inv['date'])}", 6.5))
    st.append(Spacer(1, 2))
    meta = Table([[kv("Invoice No.", inv["no"]), kv("Dated", fdate(inv["date"]))],
                  [kv("Delivery Note", s["grn"]["challan"]), kv("Mode/Terms of Payment", po["terms"])],
                  [kv("Buyer's Order No.", po["no"]), kv("Dated", fdate(po["date"]))],
                  [kv("Dispatched through", "Road"), kv("Destination", b["addr"][-1].split(" - ")[0])],
                  [kv("Motor Vehicle No.", s["grn"]["vehicle"]), kv("e-Way Bill No.", inv["eway"] or "-")]],
                 colWidths=[45*mm, 45*mm])
    meta.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.3, colors.grey), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    head = Table([[P(party_block(v), 8), meta],
                  [P("Consignee (Ship to)<br/>" + party_block(b), 8), ""],
                  [P("Buyer (Bill to)<br/>" + party_block(b), 8), ""]],
                 colWidths=[96*mm, 94*mm])
    head.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black), ("SPAN", (1, 0), (1, 2)), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    st.append(head)
    hdr = [P(x, 7.5, True, 1) for x in ["Sl No.", "Description of Goods", "HSN/SAC", "Quantity", "Rate", "per", "Amount"]]
    rows = [hdr]
    for r in t["rows"]:
        rows.append([P(r["sl"], 8, align=1), P(f"<b>{r['desc']}</b>"), P(r["hsn"], 8, align=1),
                     P(f"<b>{qty_s(r['qty'])} {r['unit']}</b>", 8, align=2), P(inr(r["rate"]), 8, align=2),
                     P(r["unit"], 8, align=1), P(f"<b>{inr(r['taxable'])}</b>", 8, align=2)])
    for (name, rate), amt in t["tax"].items():
        rows.append(["", P(f"<b><i>Output {name} @ {rate:g}%</i></b>", 8, align=2), "", "", P(f"{rate:g}", 8, align=2), P("%", 8, align=1), P(f"<b>{inr(amt)}</b>", 8, align=2)])
    if t["round_off"]:
        rows.append(["", P("<i>Round Off</i>", 8, align=2), "", "", "", "", P(inr(t["round_off"]), 8, align=2)])
    tq = sum(r["qty"] for r in t["rows"])
    rows.append(["", P("<b>Total</b>", 8, align=2), "", P(f"<b>{qty_s(tq)}</b>", 8, align=2), "", "", P(f"<b>Rs. {inr(t['total'])}</b>", 9, align=2)])
    items = Table(rows, colWidths=[10*mm, 76*mm, 18*mm, 26*mm, 22*mm, 10*mm, 28*mm], repeatRows=1)
    items.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, colors.black), ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.black),
                               ("LINEAFTER", (0, 0), (-2, -1), 0.3, colors.grey), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("LINEABOVE", (0, -1), (-1, -1), 0.5, colors.black)]))
    st.append(items)
    st.append(Table([[P(f"Amount Chargeable (in words)<br/><b>{words(t['total'])}</b>", 8), P("E. &amp; O.E.", 7, align=2)]],
                    colWidths=[160*mm, 30*mm], style=BOX))
    # HSN summary
    if t["intra"]:
        hh = ["HSN/SAC", "Taxable Value", "CGST Rate", "CGST Amount", "SGST/UTGST Rate", "SGST/UTGST Amount", "Total Tax Amount"]
    else:
        hh = ["HSN/SAC", "Taxable Value", "IGST Rate", "IGST Amount", "Total Tax Amount"]
    hs = [[P(x, 7, True, 1) for x in hh]]
    tt = 0
    for (hsn, r), tv in sorted(t["groups"].items()):
        if t["intra"]:
            a = round(tv * r / 200, 2); tt += 2 * a
            hs.append([P(hsn, 7.5), P(inr(tv), 7.5, align=2), P(f"{r/2:g}%", 7.5, align=2), P(inr(a), 7.5, align=2),
                       P(f"{r/2:g}%", 7.5, align=2), P(inr(a), 7.5, align=2), P(inr(2 * a), 7.5, align=2)])
        else:
            a = round(tv * r / 100, 2); tt += a
            hs.append([P(hsn, 7.5), P(inr(tv), 7.5, align=2), P(f"{r:g}%", 7.5, align=2), P(inr(a), 7.5, align=2), P(inr(a), 7.5, align=2)])
    n = len(hh)
    hs.append([P("<b>Total</b>", 7.5, align=2), P(f"<b>{inr(t['subtotal'])}</b>", 7.5, align=2)] + [""] * (n - 3) + [P(f"<b>{inr(round(tt, 2))}</b>", 7.5, align=2)])
    cw = [190 / n * mm] * n
    hst = Table(hs, colWidths=cw); hst.setStyle(GRID)
    st.append(hst)
    st.append(P(f"Tax Amount (in words): <b>{words(t['tax_total'])}</b>", 8))
    bank = v["bank"]
    foot = Table([[P(f"Company's PAN: <b>{v['pan']}</b><br/><br/><u>Declaration</u><br/>We declare that this invoice shows the actual price of the goods described and that all particulars are true and correct.", 7.5),
                   P(f"Company's Bank Details<br/>Bank Name: <b>{bank[0]}</b><br/>A/c No.: <b>{bank[2]}</b><br/>Branch &amp; IFS Code: <b>{bank[1]} &amp; {bank[3]}</b><br/><br/><b>for {v['name']}</b><br/><br/><br/>Authorised Signatory", 7.5)]],
                 colWidths=[95*mm, 95*mm], style=BOX)
    st.append(foot)
    st.append(P("This is a Computer Generated Invoice", 7, align=1))
    build(path, st, f"Invoice {inv['no']}")

# ================================================================= INVOICE LAYOUT 2 (modern, IGST)
def invoice_l2(path, s, label=None):
    b, v, inv, po = s["buyer"], s["vendor"], s["inv"], s["po"]
    t = totals(inv["lines"], b, v)
    teal = colors.HexColor("#0F6B6B")
    st = []
    band = Table([[P(f"<font color='white' size=15><b>{v['name'].upper()}</b></font><br/><font color='white' size=7.5>{', '.join(v['addr'])}<br/>GSTIN: {v['gstin']} | PAN: {v['pan']}<br/>{v['phone']} | {v['email']}</font>", 8),
                   P("<font color='white' size=22><b>INVOICE</b></font>", 8, align=2)]], colWidths=[130*mm, 60*mm])
    band.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), teal), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                              ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    st += [band, Spacer(1, 6)]
    metaR = P(f"<b>Invoice #:</b> {inv['no']}<br/><b>Date:</b> {idate(inv['date'])}<br/><b>PO Ref:</b> {po['no']}<br/><b>PO Date:</b> {idate(po['date'])}<br/>"
              f"<b>Challan No:</b> {s['grn']['challan']}<br/><b>Payment Terms:</b> {po['terms']}<br/><b>Place of Supply:</b> {b['state']} ({b['code']})", 8)
    bill = P(f"<font color='#0F6B6B'><b>BILL TO</b></font><br/><b>{b['name']}</b><br/>{'<br/>'.join(b['addr'])}<br/>GSTIN: {b['gstin']}<br/>State: {b['state']} ({b['code']})", 8)
    ship = P(f"<font color='#0F6B6B'><b>SHIP TO</b></font><br/><b>{b['name']}</b><br/>{'<br/>'.join(b['addr'])}", 8)
    top = Table([[bill, ship, metaR]], colWidths=[62*mm, 52*mm, 76*mm])
    top.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    st += [top, Spacer(1, 8)]
    hdr = [P(f"<font color='white'><b>{x}</b></font>", 7, align=1) for x in
           ["#", "Item &amp; Description", "HSN", "Qty", "Unit", "Rate", "Taxable Value", "GST %", "Tax Amt", "Line Total"]]
    rows = [hdr]
    for r in t["rows"]:
        ta = round(r["taxable"] * r["gst"] / 100, 2)
        rows.append([P(r["sl"], 7.5, align=1), P(f"<b>{r['desc']}</b><br/><font size=6.5 color='#555555'>Code: {r['code']}</font>", 7.5), P(r["hsn"], 7.5, align=1),
                     P(qty_s(r["qty"]), 7.5, align=2), P(r["unit"], 7.5, align=1), P(inr(r["rate"]), 7.5, align=2), P(inr(r["taxable"]), 7.5, align=2),
                     P(f"{r['gst']}%", 7.5, align=1), P(inr(ta), 7.5, align=2), P(inr(round(r['taxable'] + ta, 2)), 7.5, align=2)])
    it = Table(rows, colWidths=[7*mm, 55*mm, 12*mm, 14*mm, 11*mm, 18*mm, 24*mm, 11*mm, 18*mm, 20*mm], repeatRows=1)
    it.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), teal), ("LINEBELOW", (0, 1), (-1, -1), 0.3, colors.lightgrey),
                            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    st += [it, Spacer(1, 6)]
    trows = [[P("Sub Total", 8, align=2), P(inr(t["subtotal"]), 8, align=2)]]
    for (name, rate), amt in t["tax"].items():
        trows.append([P(f"{name} @ {rate:g}%", 8, align=2), P(inr(amt), 8, align=2)])
    trows.append([P("Round Off", 8, align=2), P(inr(t["round_off"]), 8, align=2)])
    trows.append([P("<b>GRAND TOTAL (INR)</b>", 9, align=2), P(f"<b>{inr(t['total'])}</b>", 9, align=2)])
    tb = Table(trows, colWidths=[45*mm, 30*mm])
    tb.setStyle(TableStyle([("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#DDEFEF")), ("LINEABOVE", (0, -1), (-1, -1), 0.8, teal)]))
    wrap = Table([["", tb]], colWidths=[115*mm, 75*mm]); st += [wrap, Spacer(1, 4)]
    st.append(P(f"<b>Amount in words:</b> {words(t['total'])}", 8))
    bank = v["bank"]
    st += [Spacer(1, 8), P(f"<b>Bank Details</b> &nbsp; {bank[0]}, {bank[1]} | A/c: {bank[2]} | IFSC: {bank[3]}", 7.5),
           P("<b>Terms:</b> Interest @18% p.a. on overdue payments. Subject to Pune jurisdiction. Goods once sold will not be taken back.", 7.5),
           Spacer(1, 18), P(f"For <b>{v['name']}</b><br/><br/>Authorised Signatory", 8, align=2)]
    build(path, st, f"Invoice {inv['no']}")

# ================================================================= INVOICE LAYOUT 3 (small vendor, minimal)
def invoice_l3(path, s, label=None):
    b, v, inv, po = s["buyer"], s["vendor"], s["inv"], s["po"]
    t = totals(inv["lines"], b, v)
    st = [P(f"<b>{v['name'].upper()}</b>", 16, align=1),
          P(f"{', '.join(v['addr'])} &nbsp;|&nbsp; Ph: {v['phone']}", 8, align=1),
          P(f"GSTIN: {v['gstin']}", 8, align=1), Spacer(1, 4),
          P(f"<b>BILL / CASH-CREDIT MEMO</b>{'  -  ' + label if label else ''}", 10, align=1), Spacer(1, 4)]
    m = Table([[P(f"M/s. <b>{b['name']}</b><br/>{'<br/>'.join(b['addr'])}<br/>GSTIN: {b['gstin']}", 8.5),
                P(f"Bill No.: <b>{inv['no']}</b><br/>Date: <b>{sdate(inv['date'])}</b><br/>Your Order No.: <b>{po['no']} dt {sdate(po['date'])}</b><br/>Our DC No.: {s['grn']['challan']}<br/>Vehicle: {s['grn']['vehicle']}", 8.5)]],
              colWidths=[100*mm, 90*mm]); m.setStyle(BOX); st.append(m)
    rows = [[P(x, 8, True, 1) for x in ["S.No", "Particulars", "HSN", "Qty", "Rate", "GST", "Amount"]]]
    for r in t["rows"]:
        rows.append([P(r["sl"], 8.5, align=1), P(r["desc"], 8.5), P(r["hsn"], 8.5, align=1), P(f"{qty_s(r['qty'])} {r['unit']}", 8.5, align=2),
                     P(inr(r["rate"]), 8.5, align=2), P(f"{r['gst']}%", 8.5, align=1), P(inr(r["taxable"]), 8.5, align=2)])
    rows += [[""] * 7 for _ in range(3)]
    it = Table(rows, colWidths=[13*mm, 77*mm, 16*mm, 24*mm, 22*mm, 12*mm, 26*mm]); it.setStyle(GRID); st.append(it)
    trows = [[P("Total (before tax)", 8.5, align=2), P(inr(t["subtotal"]), 8.5, align=2)]]
    for (name, rate), amt in sorted(t["tax"].items(), key=lambda x: (x[0][1], x[0][0])):
        trows.append([P(f"{name} {rate:g}%", 8.5, align=2), P(inr(amt), 8.5, align=2)])
    trows.append([P("Round off", 8.5, align=2), P(inr(t["round_off"]), 8.5, align=2)])
    trows.append([P("<b>NET AMOUNT</b>", 9, align=2), P(f"<b>{inr(t['total'])}</b>", 9, align=2)])
    tb = Table(trows, colWidths=[40*mm, 30*mm]); tb.setStyle(GRID)
    bank = v["bank"]
    bt = Table([[P(f"Rupees: <b>{words(t['total']).replace('INR ', '')}</b><br/><br/>Bank: {bank[0]} {bank[1]}<br/>A/c No. {bank[2]} &nbsp; IFSC {bank[3]}<br/><br/>E.&amp;O.E. &nbsp; Goods once sold cannot be returned.", 8), tb]],
               colWidths=[120*mm, 70*mm]); bt.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    st += [Spacer(1, 4), bt, Spacer(1, 20), P(f"For <b>{v['name']}</b><br/><br/>Proprietor", 8.5, align=2)]
    build(path, st, f"Bill {inv['no']}")

# ================================================================= PURCHASE ORDER LAYOUT A (formal, bordered)
def po_a(path, s):
    b, v, po = s["buyer"], s["vendor"], s["po"]
    t = totals(po["lines"], b, v)
    st = [P(f"<b>{b['name'].upper()}</b>", 14, align=1), P(f"{', '.join(b['addr'])}<br/>GSTIN: {b['gstin']} | Ph: {b['phone']} | {b['email']}", 8, align=1),
          Spacer(1, 4), P("<b>PURCHASE ORDER</b>", 13, align=1), Spacer(1, 4)]
    top = Table([[P(f"<b>To (Supplier):</b><br/><b>{v['name']}</b><br/>{'<br/>'.join(v['addr'])}<br/>GSTIN: {v['gstin']}<br/>Ph: {v['phone']}", 8.5),
                  P(f"<b>PO No.:</b> {po['no']}<br/><b>PO Date:</b> {fdate(po['date'])}<br/><b>Delivery Date:</b> {fdate(po['delivery'])}<br/><b>Payment Terms:</b> {po['terms']}<br/><b>Delivery At:</b> {b['name']}, {b['addr'][0]}", 8.5)]],
                colWidths=[95*mm, 95*mm]); top.setStyle(GRID); st.append(top)
    rows = [[P(x, 7.5, True, 1) for x in ["Sl", "Item Code", "Description", "HSN", "Qty", "UoM", "Rate (Rs.)", "GST %", "Amount (Rs.)"]]]
    for r in t["rows"]:
        rows.append([P(r["sl"], 8, align=1), P(r["code"], 8), P(r["desc"], 8), P(r["hsn"], 8, align=1), P(qty_s(r["qty"]), 8, align=2),
                     P(r["unit"], 8, align=1), P(inr(r["rate"]), 8, align=2), P(f"{r['gst']}", 8, align=1), P(inr(r["taxable"]), 8, align=2)])
    it = Table(rows, colWidths=[8*mm, 24*mm, 56*mm, 14*mm, 16*mm, 12*mm, 22*mm, 11*mm, 27*mm]); it.setStyle(GRID); st.append(it)
    tr = [[P("Basic Value", 8, align=2), P(inr(t["subtotal"]), 8, align=2)]]
    for (n, r), a in t["tax"].items(): tr.append([P(f"{n} @ {r:g}%", 8, align=2), P(inr(a), 8, align=2)])
    tr.append([P("<b>Total Order Value</b>", 8.5, align=2), P(f"<b>{inr(t['total'])}</b>", 8.5, align=2)])
    tb = Table(tr, colWidths=[45*mm, 30*mm]); tb.setStyle(GRID)
    st += [Table([["", tb]], colWidths=[115*mm, 75*mm]), Spacer(1, 4), P(f"Amount in words: <b>{words(t['total'])}</b>", 8), Spacer(1, 6),
           P("<b>Terms &amp; Conditions</b><br/>1. Please mention PO number on all challans and invoices.<br/>2. Material must be accompanied by delivery challan and test certificate where applicable.<br/>3. Goods are subject to our inspection and acceptance.<br/>4. Quantity / rate deviation requires prior written approval.", 7.5),
           Spacer(1, 25), Table([[P("Prepared By", 8, align=1), P("Checked By", 8, align=1), P(f"For <b>{b['name']}</b><br/><br/>Authorised Signatory", 8, align=1)]], colWidths=[50*mm, 50*mm, 90*mm])]
    build(path, st, f"PO {po['no']}")

# ================================================================= PURCHASE ORDER LAYOUT B (compact, label:value)
def po_b(path, s):
    b, v, po = s["buyer"], s["vendor"], s["po"]
    t = totals(po["lines"], b, v)
    st = [P(f"<b>{b['name']}</b>", 15), P(f"{', '.join(b['addr'])} | GST: {b['gstin']}", 8), Spacer(1, 6),
          P("<b>P.O.</b>", 20, color=colors.HexColor("#444444")), Spacer(1, 4)]
    top = Table([[P(f"<b>Supplier</b><br/>{v['name']}<br/>{', '.join(v['addr'])}<br/>GST No: {v['gstin']}", 8.5),
                  P(f"<b>P.O. No.:</b> {po['no']}<br/><b>P.O. Date:</b> {idate(po['date'])}<br/><b>Delivery Schedule:</b> By {idate(po['delivery'])}<br/><b>Terms:</b> {po['terms']}<br/><b>Ship To:</b> Stores, {b['addr'][0]}", 8.5)]],
                colWidths=[95*mm, 95*mm])
    top.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.6, colors.black), ("VALIGN", (0, 0), (-1, -1), "TOP")])); st += [top, Spacer(1, 6)]
    rows = [[P(x, 8, True) for x in ["No.", "Material Description", "HSN", "Qty", "Unit", "Unit Price", "GST", "Net Value"]]]
    for r in t["rows"]:
        rows.append([P(r["sl"], 8.5), P(f"{r['desc']}<br/><font size=6.5 color='#666666'>({r['code']})</font>", 8.5), P(r["hsn"], 8.5), P(qty_s(r["qty"]), 8.5, align=2),
                     P(r["unit"], 8.5), P(inr(r["rate"]), 8.5, align=2), P(f"{r['gst']}%", 8.5), P(inr(r["taxable"]), 8.5, align=2)])
    it = Table(rows, colWidths=[10*mm, 68*mm, 15*mm, 17*mm, 14*mm, 24*mm, 12*mm, 30*mm])
    it.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.black), ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.lightgrey), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    st += [it, Spacer(1, 6)]
    tax_txt = " &nbsp;|&nbsp; ".join(f"{n} {r:g}%: {inr(a)}" for (n, r), a in t["tax"].items())
    st += [P(f"<b>Net Total:</b> {inr(t['subtotal'])} &nbsp;&nbsp; <b>Taxes:</b> {tax_txt} &nbsp;&nbsp; <b>PO Value: Rs. {inr(t['total'])}</b>", 8.5),
           Spacer(1, 8), P("Please quote P.O. No. on delivery challan &amp; invoice. Deliver within schedule. Any change in rate/qty requires written consent.", 7.5),
           Spacer(1, 30), P("Purchase Manager &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; Authorised Signatory", 8.5, align=2)]
    build(path, st, f"PO {po['no']}")

# ================================================================= GRN LAYOUT A
def grn_a(path, s):
    b, v, g, po = s["buyer"], s["vendor"], s["grn"], s["po"]
    st = [P(f"<b>{b['name'].upper()}</b>", 13, align=1), P(", ".join(b["addr"]), 8, align=1), Spacer(1, 3),
          P("<b>GOODS RECEIPT NOTE</b>", 13, align=1), Spacer(1, 4)]
    top = Table([[P(f"<b>GRN No.:</b> {g['no']}<br/><b>GRN Date:</b> {fdate(g['date'])}<br/><b>PO No.:</b> {po['no']}<br/><b>PO Date:</b> {fdate(po['date'])}", 8.5),
                  P(f"<b>Supplier:</b> {v['name']}<br/><b>Supplier DC/Challan No.:</b> {g['challan']} dt {fdate(g['challan_date'])}<br/><b>Vehicle No.:</b> {g['vehicle']}<br/><b>Received at:</b> Main Stores", 8.5)]],
                colWidths=[75*mm, 115*mm]); top.setStyle(GRID); st.append(top)
    rows = [[P(x, 7, True, 1) for x in ["Sl", "Item Code", "Description", "UoM", "PO Qty", "Challan Qty", "Received Qty", "Accepted Qty", "Rejected Qty", "Remarks"]]]
    for i, r in enumerate(g["lines"], 1):
        rows.append([P(i, 8, align=1), P(r["code"], 7.5), P(r["desc"], 7.5), P(r["unit"], 7.5, align=1), P(qty_s(r["ordered"]), 8, align=2),
                     P(qty_s(r["challan"]), 8, align=2), P(qty_s(r["received"]), 8, align=2), P(qty_s(r["accepted"]), 8, align=2),
                     P(qty_s(r["rejected"]), 8, align=2), P(r["remarks"], 7)])
    it = Table(rows, colWidths=[7*mm, 23*mm, 45*mm, 11*mm, 15*mm, 17*mm, 17*mm, 17*mm, 16*mm, 22*mm]); it.setStyle(GRID); st.append(it)
    st += [Spacer(1, 6), P("Inspection: Visual inspection done. Quantity verified against challan.", 8), Spacer(1, 30),
           Table([[P("Store In-charge", 8, align=1), P("Quality Control", 8, align=1), P("Purchase Dept.", 8, align=1), P("Accounts", 8, align=1)]], colWidths=[47.5*mm]*4)]
    build(path, st, f"GRN {g['no']}")

# ================================================================= GRN LAYOUT B
def grn_b(path, s):
    b, v, g, po = s["buyer"], s["vendor"], s["grn"], s["po"]
    st = [P(f"<b>{b['name']}</b>", 14), P("<b>MATERIAL RECEIPT REPORT</b>", 11, color=colors.HexColor("#333333")), Spacer(1, 6)]
    top = Table([[P(f"MRR No: <b>{g['no']}</b>", 9), P(f"Date: <b>{sdate(g['date'])}</b>", 9), P(f"Against P.O.: <b>{po['no']}</b>", 9)],
                 [P(f"Party: <b>{v['name']}</b>", 9), P(f"DC / Inv No: <b>{g['challan']}</b>", 9), P(f"Vehicle: <b>{g['vehicle']}</b>", 9)]],
                colWidths=[75*mm, 55*mm, 60*mm]); top.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, colors.black), ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.grey)]))
    st += [top, Spacer(1, 6)]
    rows = [[P(x, 8, True) for x in ["S.No", "Item", "Qty as per DC", "Qty Received", "Qty Accepted", "Condition / Remarks"]]]
    for i, r in enumerate(g["lines"], 1):
        rows.append([P(i, 9), P(r["desc"], 9), P(f"{qty_s(r['challan'])} {r['unit']}", 9), P(f"{qty_s(r['received'])} {r['unit']}", 9),
                     P(f"{qty_s(r['accepted'])} {r['unit']}", 9), P(r["remarks"] or "OK", 8)])
    it = Table(rows, colWidths=[12*mm, 66*mm, 27*mm, 27*mm, 27*mm, 31*mm]); it.setStyle(GRID); st.append(it)
    st += [Spacer(1, 40), Table([[P("Received by (Stores)", 8.5), P("Verified (Purchase)", 8.5), P("Approved", 8.5)]], colWidths=[63*mm]*3)]
    build(path, st, f"MRR {g['no']}")

# ---------------------------------------------------------------- truth json
def truth_doc(kind, s, lines_key):
    b, v = s["buyer"], s["vendor"]
    d = s[kind]
    base = dict(doc_type={"po": "PURCHASE_ORDER", "grn": "GRN", "inv": "INVOICE"}[kind],
                number=d["no"], date=iso(d["date"]),
                buyer=dict(name=b["name"], gstin=b["gstin"]), vendor=dict(name=v["name"], gstin=v["gstin"]))
    if kind == "po":
        base["delivery_date"] = iso(d["delivery"]); base["payment_terms"] = d["terms"]
    if kind == "grn":
        base["po_ref"] = s["po"]["no"]; base["challan_no"] = d["challan"]; base["challan_date"] = iso(d["challan_date"]); base["vehicle_no"] = d["vehicle"]
        base["lines"] = [dict(line_no=i, item_code=r["code"], description=r["desc"], uom=r["unit"], po_qty=r["ordered"], challan_qty=r["challan"],
                              received_qty=r["received"], accepted_qty=r["accepted"], rejected_qty=r["rejected"], remarks=r["remarks"]) for i, r in enumerate(d["lines"], 1)]
        return base
    t = totals(d["lines"], b, v)
    if kind == "inv":
        base["po_ref"] = s["po"]["no"]; base["challan_no"] = s["grn"]["challan"]; base["eway_bill"] = d["eway"]
    base["lines"] = [dict(line_no=r["sl"], item_code=r["code"], description=r["desc"], hsn=r["hsn"], qty=r["qty"], uom=r["unit"],
                          rate=r["rate"], gst_rate=r["gst"], taxable_value=r["taxable"]) for r in t["rows"]]
    base["tax_type"] = "CGST+SGST" if t["intra"] else "IGST"
    base["tax_breakup"] = [dict(type=n, rate=r, amount=a) for (n, r), a in t["tax"].items()]
    base["subtotal"] = t["subtotal"]; base["round_off"] = t["round_off"]; base["total"] = t["total"]
    return base

# ---------------------------------------------------------------- run
INV = {1: invoice_l1, 2: invoice_l2, 3: invoice_l3}
PO = {"A": po_a, "B": po_b}
GRN = {"A": grn_a, "B": grn_b}
manifest = []
for sid, s in SCEN.items():
    files = {}
    p = f"{OUT}/pdf/po_{sid}.pdf"; PO[s["po_layout"]](p, s); files["po"] = p
    p = f"{OUT}/pdf/grn_{sid}.pdf"; GRN[s["grn_layout"]](p, s); files["grn"] = p
    p = f"{OUT}/pdf/inv_{sid}.pdf"; INV[s["inv_layout"]](p, s); files["inv"] = p
    for k in ("po", "grn", "inv"):
        json.dump(truth_doc(k, s, k), open(f"{OUT}/truth/{k}_{sid}.json", "w"), indent=2)
    entry = dict(scenario=sid, vendor=s["vendor"]["name"], buyer=s["buyer"]["name"],
                 layouts=dict(po=f"PO-{s['po_layout']}", grn=f"GRN-{s['grn_layout']}", invoice=f"INV-{s['inv_layout']}"),
                 files={k: os.path.basename(v) for k, v in files.items()}, expected=s["expected"])
    if sid == "S3":
        p = f"{OUT}/pdf/inv_S3_duplicate.pdf"; invoice_l3(p, s, label="DUPLICATE FOR TRANSPORTER")
        json.dump(truth_doc("inv", s, "inv"), open(f"{OUT}/truth/inv_S3_duplicate.json", "w"), indent=2)
        entry["files"]["inv_duplicate"] = "inv_S3_duplicate.pdf"
    manifest.append(entry)
json.dump(manifest, open(f"{OUT}/manifest.json", "w"), indent=2)
print("done", len(os.listdir(f"{OUT}/pdf")), "pdfs")
