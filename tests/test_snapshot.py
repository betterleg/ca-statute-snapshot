#!/usr/bin/env python3
"""Offline test: builds a small fake pubinfo zip in the Legislature's layout (MySQL load scripts, tab-delimited .dat
files, one .lob XML file per section), runs the snapshot twice and checks the output and the change report.
Run: python tests/test_snapshot.py"""
import json, pathlib, shutil, subprocess, sys, zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "tests" / "out"

LOAD = """LOAD DATA LOCAL INFILE 'LAW_SECTION_TBL.dat'
INTO TABLE law_section_tbl
FIELDS TERMINATED BY '\\t' OPTIONALLY ENCLOSED BY '`' ESCAPED BY '\\\\'
LINES TERMINATED BY '\\n'
(id, law_code, section_num, op_statues, op_chapter, op_section, effective_date, law_section_version_id, division, title, part, chapter, article, history, @var1, active_flg, trans_uid, trans_update)
SET content_xml=LOAD_FILE(concat(@path, @var1));
"""
CAPUBLIC = """CREATE TABLE `law_toc_tbl` (
  `law_code` varchar(5) DEFAULT NULL,
  `division` varchar(100) DEFAULT NULL,
  `title` varchar(100) DEFAULT NULL,
  `part` varchar(100) DEFAULT NULL,
  `chapter` varchar(100) DEFAULT NULL,
  `article` varchar(100) DEFAULT NULL,
  `heading` varchar(2000) DEFAULT NULL,
  `active_flg` varchar(1) DEFAULT 'Y',
  `trans_uid` varchar(30) DEFAULT NULL,
  `trans_update` datetime DEFAULT NULL
) ENGINE=InnoDB;
CREATE TABLE `codes_tbl` (
  `code` varchar(5) DEFAULT NULL,
  `title` varchar(2000) DEFAULT NULL
) ENGINE=InnoDB;
CREATE TABLE `law_toc_sections_tbl` (
  `id` varchar(100) DEFAULT NULL,
  `law_code` varchar(5) DEFAULT NULL,
  `section_num` varchar(30) DEFAULT NULL
) ENGINE=InnoDB;
"""
def xml(paras, table=None):
    body = "".join(f"<p style=\"margin:0\">{p}</p>" for p in paras)
    if table: body += "<table>" + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in table) + "</table>"
    return f'<?xml version="1.0"?><caml:LawSection xmlns:caml="http://lc.ca.gov/legalservices/schemas/caml.1#"><caml:Num>X.</caml:Num><caml:Content>{body}</caml:Content></caml:LawSection>'

def row(i, code, sec, lob, hist="(Added by Stats. 2020, Ch. 1.)", active="Y"):
    f = [str(i), code, sec, "2020", "1", "1", "2021-01-01 00:00:00", f"v{i}", "1.", "7.", "", "3.", "", hist, lob, active, "u", "2026-01-01 00:00:00"]
    return "\t".join(f"`{x}`" if " " in x or "(" in x else x for x in f)

def make_zip(path, variant):
    secs = [("GOV", "65915", ["(a)A city shall grant a density bonus.", "(b) (1)Definitions apply."], [["Percent", "Bonus"], ["10", "20"]]),
            ("GOV", "65589.5", ["(a) The Legislature finds housing is critical.", "(b) More text."] + (["(c) New subdivision."] if variant == 2 else []), None),
            ("GOV", "12345", ["Outside the configured ranges."], None),
            ("PRC", "21080", ["(a) This division applies to discretionary projects."], None),
            ("CIV", "1940", ["Civil Code is not configured."], None)]
    if variant == 2: secs.append(("HSC", "17021.8", ["(a) Streamlined approval of agricultural employee housing."], None))
    with zipfile.ZipFile(path, "w") as z:
        dat = []
        for i, (code, sec, paras, tbl) in enumerate(secs, 1):
            lob = f"LAW_SECTION_TBL_{i}.lob"; z.writestr(lob, xml(paras, tbl)); dat.append(row(i, code, sec, lob))
        dat.append(row(99, "GOV", "65000", "LAW_SECTION_TBL_99.lob", active="N")); z.writestr("LAW_SECTION_TBL_99.lob", xml(["Inactive."]))
        z.writestr("LAW_SECTION_TBL.dat", "\n".join(dat) + "\n")
        z.writestr("law_section_tbl.sql", LOAD); z.writestr("capublic.sql", CAPUBLIC)
        z.writestr("LAW_TOC_TBL.dat", "GOV\t\t7.\t\t\t\tPLANNING AND LAND USE\tY\tu\t2026-01-01\nPRC\t13.\t\t\t\t\tENVIRONMENTAL QUALITY\tY\tu\t2026-01-01\n")
        z.writestr("LAW_TOC_SECTIONS_TBL.dat", "1\tGOV\t65915\n")
        z.writestr("CODES_TBL.dat", "GOV\tGovernment Code\n")

def run(zip_path):
    subprocess.run([sys.executable, str(ROOT / "scripts" / "pubinfo_snapshot.py"), "--config", str(ROOT / "config.json"), "--out", str(OUT / "snapshot"),
                    "--source-zip", str(zip_path)], check=True)

def main():
    shutil.rmtree(OUT, ignore_errors=True); OUT.mkdir(parents=True)
    make_zip(OUT / "pubinfo_2025.zip", 1); run(OUT / "pubinfo_2025.zip")
    s = OUT / "snapshot"
    gov = [json.loads(l) for l in (s / "sections" / "GOV.jsonl").read_text().splitlines()]
    assert [x["sec"] for x in gov] == ["65589.5", "65915"], [x["sec"] for x in gov]      # 12345 out of range, 65000 inactive
    g = next(x for x in gov if x["sec"] == "65915")
    assert g["paras"][:2] == ["(a) A city shall grant a density bonus.", "(b) (1) Definitions apply."], g["paras"]
    assert g["paras"][2] == "PercentBonus" and g["paras"][3] == "1020", g["paras"]
    assert g["history"] == "(Added by Stats. 2020, Ch. 1.)" and g["heads"]["title"] == "7.", g
    assert not (s / "sections" / "CIV.jsonl").exists()
    assert [json.loads(l)["sec"] for l in (s / "sections" / "PRC.jsonl").read_text().splitlines()] == ["21080"]
    assert (s / "toc" / "GOV.jsonl").read_text().count("PLANNING") == 1
    assert not (s / "changes" / "latest.json").exists()          # first run: nothing to compare with
    make_zip(OUT / "pubinfo_2025.zip", 2); run(OUT / "pubinfo_2025.zip")
    ch = json.loads((s / "changes" / "latest.json").read_text())
    assert ch["changed"] == ["GOV 65589.5"] and ch["added"] == ["HSC 17021.8"] and ch["removed"] == [], ch
    m = json.loads((s / "manifest.json").read_text()); assert m["codes"]["GOV"]["sections"] == 2 and m["problem_count"] == 0, m
    assert json.loads((s / "last-run.json").read_text())["result"] == "built"
    print("all snapshot tests passed")

if __name__ == "__main__":
    main()
