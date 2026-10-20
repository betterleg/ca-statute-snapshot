#!/usr/bin/env python3
"""Offline test for scripts/pubinfo_bills.py: builds a small fake pubinfo_daily zip (bill tables, one .lob XML file per
bill version), runs twice and checks the bill files and the change report. Run: python tests/test_bills.py"""
import json, pathlib, shutil, subprocess, sys, zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "tests" / "out-bills"

VERSION_LOAD = """LOAD DATA LOCAL INFILE 'BILL_VERSION_TBL.dat'
INTO TABLE bill_version_tbl
FIELDS TERMINATED BY '\\t' OPTIONALLY ENCLOSED BY '`' ESCAPED BY '\\\\'
LINES TERMINATED BY '\\n'
(bill_version_id, bill_id, version_num, bill_version_action_date, bill_version_action, request_num, subject, vote_required, appropriation, fiscal_committee, local_program, substantive_changes, urgency, taxlevy, @var1, active_flg, trans_uid, trans_update)
SET bill_xml=LOAD_FILE(concat(@path, @var1));
"""

def bill_xml(title, sections, digest="Existing law requires a general plan."):
    secs = "".join(
        f'<caml:BillSection id="s{i}"><caml:Num>SECTION {i}.</caml:Num><caml:ActionLine>{action}</caml:ActionLine>'
        f'<caml:Fragment><caml:LawSection><caml:Num>{num}.</caml:Num><caml:LawSectionVersion><caml:Content>'
        + "".join(f"<p>{p}</p>" for p in paras) + "</caml:Content></caml:LawSectionVersion></caml:LawSection></caml:Fragment></caml:BillSection>"
        for i, (action, num, paras) in enumerate(sections, 1))
    return (f'<?xml version="1.0"?><caml:MeasureDoc xmlns:caml="http://lc.ca.gov/legalservices/schemas/caml.1#"><caml:Description>'
            f'<caml:Title>{title}</caml:Title><caml:DigestText><p>{digest}</p></caml:DigestText></caml:Description><caml:Bill>{secs}</caml:Bill></caml:MeasureDoc>')

def q(x): return f"`{x}`" if (" " in x or "(" in x or ":" in x) else x

def make_zip(path, variant):
    bills = [  # bill_id, type, num, chapter, status, versions [(vid, num, date, action, xml)]
        ("20252026AB1457", "AB", "1457", "517" if variant == 2 else "", "Chaptered" if variant == 2 else "Enrolled", [
            ("v1", "99", "2025-02-21", "Introduced", bill_xml("An act to amend Section 65302 of the Government Code, relating to land use.",
             [("Section 65302 of the Government Code is amended to read:", "65302", ["The general plan shall include:", "(h) An environmental justice element."])])),
        ] + ([("v2", "95", "2026-09-27", "Chaptered by Secretary of State", bill_xml("An act to amend Section 65302 of the Government Code, relating to land use.",
             [("Section 65302 of the Government Code is amended to read:", "65302", ["The general plan shall include:", "(h) An environmental justice element.", "(2) Meaningful involvement."])]))] if variant == 2 else [])),
        ("20252026SB131", "SB", "131", "24", "Chaptered", [
            ("v3", "95", "2025-06-30", "Chaptered by Secretary of State", bill_xml("An act to amend Sections 21080.1 and 21080.47 of the Public Resources Code, and to amend Section 1940 of the Civil Code.",
             [("Sections 21080.1 and 21080.47 of the Public Resources Code are amended to read:", "21080.1", ["(a) The lead agency shall determine."]),
              ("Section 1940 of the Civil Code is amended to read:", "1940", ["Civil Code text, not tracked."])]))]),
        ("20252026AB9999", "AB", "9999", "", "In Committee", [
            ("v4", "99", "2026-01-05", "Introduced", bill_xml("An act to amend Section 1940 of the Civil Code.", [("Section 1940 of the Civil Code is amended to read:", "1940", ["Not tracked."])]))]),
    ]
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("bill_version_tbl.sql", VERSION_LOAD)
        bt, vt, ht, at = [], [], [], []
        n = 0
        for bid, mt, num, ch, status, vers in bills:
            bt.append("\t".join(q(x) for x in [bid, "20252026", "0", mt, num, status, "2026" if ch else "", "", "", ch, vers[-1][0], "Y", "u", "2026-01-01 00:00:00", "Assembly Floor", "", "A", status, ""]))
            for vid, vnum, date, action, xml in vers:
                n += 1; lob = f"BILL_VERSION_TBL_{n}.lob"; z.writestr(lob, xml)
                vt.append("\t".join(q(x) for x in [vid, bid, vnum, date + " 00:00:00", action, "", "Subject", "Majority", "No", "No", "Yes", "No", "No", "No", lob, "Y", "u", "2026-01-01 00:00:00"]))
                at.append("\t".join(q(x) for x in [vid, "LEAD_AUTHOR", "ASSEMBLY", "Bryan" if num == "1457" else "Committee", "", "", "Y", "u", "2026-01-01 00:00:00", "Y"]))
                ht.append("\t".join(q(x) for x in [bid, f"h{n}", date + " 00:00:00", action, "u", "2026-01-01 00:00:00", str(n), "", "", "", "", "", ""]))
        z.writestr("BILL_TBL.dat", "\n".join(bt) + "\n"); z.writestr("BILL_VERSION_TBL.dat", "\n".join(vt) + "\n")
        z.writestr("BILL_HISTORY_TBL.dat", "\n".join(ht) + "\n"); z.writestr("BILL_VERSION_AUTHORS_TBL.dat", "\n".join(at) + "\n")

def run(zp):
    subprocess.run([sys.executable, str(ROOT / "scripts" / "pubinfo_bills.py"), "--config", str(ROOT / "config.json"), "--out", str(OUT), "--source-zip", str(zp)], check=True)

def main():
    shutil.rmtree(OUT, ignore_errors=True); OUT.mkdir(parents=True)
    z1, z2 = OUT / "daily1.zip", OUT / "daily2.zip"
    make_zip(z1, 1); make_zip(z2, 2)
    run(z1)
    idx = json.loads((OUT / "index.json").read_text())
    assert set(idx) == {"20252026 AB1457", "20252026 SB131"}, idx.keys()
    assert idx["20252026 SB131"]["sections"] == ["PRC 21080.1", "PRC 21080.47"], idx["20252026 SB131"]
    sb = json.loads((OUT / "20252026" / "SB131.json").read_text())
    assert sb["chapter"] == {"year": "2026", "num": "24"} and sb["sections"][0]["paras"] == ["(a) The lead agency shall determine."], sb["sections"]
    assert json.loads((OUT / "changes" / "latest.json").read_text())["first_run"] is True
    run(z2)
    ch = json.loads((OUT / "changes" / "latest.json").read_text())
    assert ch["changed"] == ["20252026 AB1457"] and not ch["new"] and not ch["dropped"], ch
    ab = json.loads((OUT / "20252026" / "AB1457.json").read_text())
    assert ab["status"] == "Chaptered" and ab["chapter"]["num"] == "517" and ab["authors"][0]["name"] == "Bryan", ab
    assert ab["sections"][0]["paras"][-1] == "(2) Meaningful involvement." and ab["latest_version"]["id"] == "v2", ab["sections"]
    assert ab["digest"] == ["Existing law requires a general plan."], ab["digest"]
    print("bills test passed:", len(idx), "bills; change report", ch["count"])
    shutil.rmtree(OUT)

if __name__ == "__main__":
    main()
