#!/usr/bin/env python3
"""California bills that touch tracked Code sections, from the Legislature's daily bulk download (pubinfo).

The Legislature publishes no bill API; its own bill data (the same database behind leginfo.legislature.ca.gov) is in
the pubinfo download. pubinfo_daily_<Day>.zip (about 1.1 GB, every night) holds all current-session data except the
Code tables: bills, every version's XML text, history, authors and votes. This script reads the newest daily file,
keeps the bills whose text amends, adds or repeals a section listed in config.json (the same file the statute
snapshot uses), and writes:

  bills/index.json                       "<session> <BILL>" -> status, chapter, latest action, sections, hash
  bills/<session>/<BILL>.json            one file per bill: status, history, authors, versions, digest, and for the
                                         latest version every tracked section's new text (paragraphs)
  bills/changes/<date>.json, latest.json bills new to the list or changed since the previous run
  bills/last-run.json                    written every run

Standard library only (Python 3.9+). Usage:
  python scripts/pubinfo_bills.py --config config.json --out bills                 # nightly run
  python scripts/pubinfo_bills.py --source-zip pubinfo_daily_Mon.zip ...           # use a file already on disk
  python scripts/pubinfo_bills.py --inspect ...                                     # print layouts and sample rows only

Column names come from the load scripts in the download (as in pubinfo_snapshot.py); the fallback layouts below are
from the published capublic schema and are used only if the scripts are missing. The first --inspect run on GitHub
confirms them. The text is the Legislature's; leginfo.legislature.ca.gov is the official publication.
"""
import argparse, datetime as dt, hashlib, json, os, pathlib, re, sys, tempfile, zipfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from pubinfo_snapshot import BASE, http, download, find_member, table_layout, read_dat, xml_paras, in_ranges, norm, log, FALLBACK_COLUMNS

BILL_TABLES = ["BILL_TBL", "BILL_VERSION_TBL", "BILL_HISTORY_TBL", "BILL_VERSION_AUTHORS_TBL"]
FALLBACK_COLUMNS.update({
    "BILL_TBL": ["bill_id", "session_year", "session_num", "measure_type", "measure_num", "measure_state", "chapter_year",
                 "chapter_type", "chapter_session_num", "chapter_num", "latest_bill_version_id", "active_flg", "trans_uid",
                 "trans_update", "current_location", "current_secondary_loc", "current_house", "current_status", "days_31st_in_print"],
    "BILL_VERSION_TBL": ["bill_version_id", "bill_id", "version_num", "bill_version_action_date", "bill_version_action",
                         "request_num", "subject", "vote_required", "appropriation", "fiscal_committee", "local_program",
                         "substantive_changes", "urgency", "taxlevy", "bill_xml", "active_flg", "trans_uid", "trans_update"],
    "BILL_HISTORY_TBL": ["bill_id", "bill_history_id", "action_date", "action", "trans_uid", "trans_update_dt", "action_sequence",
                         "action_code", "action_status", "primary_location", "secondary_location", "ternary_location", "end_status"],
    "BILL_VERSION_AUTHORS_TBL": ["bill_version_id", "type", "house", "name", "contribution", "committee_members", "active_flg",
                                 "trans_uid", "trans_update", "primary_author_flg"],
})
CODE_NAMES = {"GOV": "Government Code", "HSC": "Health and Safety Code", "PRC": "Public Resources Code", "CIV": "Civil Code",
              "RTC": "Revenue and Taxation Code", "WIC": "Welfare and Institutions Code", "PUC": "Public Utilities Code",
              "WAT": "Water Code", "VEH": "Vehicle Code", "BPC": "Business and Professions Code", "EDC": "Education Code",
              "FGC": "Fish and Game Code", "CCP": "Code of Civil Procedure", "PEN": "Penal Code", "LAB": "Labor Code",
              "SHC": "Streets and Highways Code", "FAC": "Food and Agricultural Code", "PCC": "Public Contract Code"}
SECNUM = re.compile(r"\b\d{1,6}(?:\.\d+)*\b")


def sha(s): return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]
def strip_tags(s): return re.sub(r"\s+([,.;:)])", r"\1", norm(re.sub(r"<[^>]+>", " ", s)))
def nn(v):
    """Field value with the database's NULL marker removed."""
    v = (v or "").strip()
    return "" if v.upper() == "NULL" else v


def newest_daily():
    """The most recently modified pubinfo_daily_<Day>.zip."""
    best = None
    for day in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"):
        name = f"pubinfo_daily_{day}.zip"
        try:
            with http(BASE + name, "HEAD") as r: lm = r.headers.get("Last-Modified", "")
        except Exception as e:
            log("HEAD failed", name, e); continue
        when = dt.datetime.strptime(lm, "%a, %d %b %Y %H:%M:%S %Z") if lm else dt.datetime.min
        if best is None or when > best[1]: best = (name, when, lm)
    if not best: raise SystemExit("No pubinfo_daily_<Day>.zip found; the site layout may have changed.")
    return best[0], best[2]


def sections_named(action, codes):
    """[(code_abbr, section)] named by a bill section's action line, e.g. 'Section 65302 of the Government Code is
    amended to read:' or 'Sections 21080.1 and 21080.2 of the Public Resources Code are amended'."""
    out = []
    names = {v: k for k, v in CODE_NAMES.items() if k in codes}
    parts = re.split("(" + "|".join(re.escape(n) for n in names) + ")", action) if names else [action]
    for i in range(1, len(parts), 2):
        before = parts[i - 1]
        before = before[before.rfind(" Code") + 5:] if " Code" in before else before      # numbers since the previous code name only
        before = re.sub(r"\(commencing with Section ([\d.]+)\)", r"\1", before)
        for n in SECNUM.findall(before):
            if "." in n or len(n) >= 3: out.append((names[parts[i]], n.rstrip(".")))
    return out


def bill_sections(xml, codes, cfg_codes):
    """Bill sections of one version's XML that touch tracked Code sections: [{code, sec, action, verb, paras}]."""
    out = []
    for m in re.finditer(r"<(?:\w+:)?BillSection\b[^>]*>(.*?)</(?:\w+:)?BillSection>", xml, re.S):
        body = m.group(1)
        am = re.search(r"<(?:\w+:)?ActionLine\b[^>]*>(.*?)</(?:\w+:)?ActionLine>", body, re.S)
        if not am: continue
        action = strip_tags(am.group(1))
        named = [(c, s) for c, s in sections_named(action, codes) if in_ranges(s, cfg_codes[c])]
        if not named: continue
        verb = "repealed" if re.search(r"\bis repealed\b|\bare repealed\b", action) and "added" not in action else \
               "added" if re.search(r"\b(is|are) added\b|commencing with", action) else "amended"
        frag = body[am.end():]
        # one Fragment may hold several sections: split on the section number elements
        pieces = re.split(r"(<(?:\w+:)?(?:LawSectionNum|Num)\b[^>]*>.*?</(?:\w+:)?(?:LawSectionNum|Num)>)", frag, flags=re.S)
        cur, buf = None, {}
        for p in pieces:
            nm = re.match(r"<(?:\w+:)?(?:LawSectionNum|Num)\b[^>]*>(.*?)</", p, re.S)
            if nm:
                cur = strip_tags(nm.group(1)).rstrip("."); continue
            if cur: buf.setdefault(cur, []).append(p)
        for c, s in named:
            paras = xml_paras("".join(buf[s])) if s in buf else (xml_paras(frag) if len(named) == 1 and verb != "repealed" else [])
            x = {"code": c, "sec": s, "action": action, "verb": verb, "paras": paras}
            if not paras and verb != "repealed": x["note"] = "section text not found in this bill section"
            out.append(x)
    return out


def build(zpath, cfg, out, inspect=False, load_zip=None):
    zips = [zipfile.ZipFile(zpath)]
    if load_zip: zips.append(zipfile.ZipFile(load_zip))
    z = zips[0]
    lay = {t: table_layout(zips, t) for t in BILL_TABLES}
    for t, L in lay.items(): log(f"{t}: {len(L['columns'])} columns from {L['source']}")
    if inspect:
        for t in BILL_TABLES:
            print(f"\n== {t}  columns: {lay[t]['columns']}")
            for i, r in enumerate(read_dat(z, t, lay[t])):
                print(json.dumps(r, ensure_ascii=False)[:600])
                if i >= 2: break
        return None
    codes = cfg["codes"]
    names = {n.lower().rsplit("/", 1)[-1]: n for n in z.namelist()}
    def lob_text(v):
        n = names.get((v or "").lower().rsplit("/", 1)[-1])
        return z.read(n).decode("utf-8", "replace") if n and v.lower().endswith(".lob") else (v or "")
    # 1. every version; keep the bills with a version that touches a tracked section
    versions, hits = {}, {}
    for r in read_dat(z, "BILL_VERSION_TBL", lay["BILL_VERSION_TBL"]):
        if (r.get("active_flg") or "Y").strip().upper() not in ("Y", ""): continue
        bid = r["bill_id"].strip()
        x = {"id": r["bill_version_id"].strip(), "num": nn(r.get("version_num")), "date": nn(r.get("bill_version_action_date"))[:10],
             "action": norm(nn(r.get("bill_version_action"))), "subject": norm(nn(r.get("subject"))), "urgency": norm(nn(r.get("urgency")))}
        xml = lob_text(next((v for k, v in r.items() if k == "bill_xml" or (v or "").lower().endswith(".lob")), ""))
        secs = bill_sections(xml, set(codes), codes) if xml else []
        if secs:
            hits.setdefault(bid, {})[x["id"]] = {"sections": secs, "title": strip_tags((re.search(r"<(?:\w+:)?Title\b[^>]*>(.*?)</(?:\w+:)?Title>", xml, re.S) or [None, ""])[1]),
                                                 "digest": xml_paras((re.search(r"<(?:\w+:)?DigestText\b[^>]*>(.*?)</(?:\w+:)?DigestText>", xml, re.S) or [None, ""])[1])}
        versions.setdefault(bid, []).append(x)
    log(f"versions read for {len(versions)} bills; {len(hits)} touch tracked sections")
    bills = {}
    for r in read_dat(z, "BILL_TBL", lay["BILL_TBL"]):
        bid = r["bill_id"].strip()
        if bid in hits: bills[bid] = r
    hist, authors = {}, {}
    for r in read_dat(z, "BILL_HISTORY_TBL", lay["BILL_HISTORY_TBL"]):
        bid = r["bill_id"].strip()
        if bid in hits: hist.setdefault(bid, []).append({"date": (r.get("action_date") or "")[:10], "action": norm(r.get("action") or ""), "seq": r.get("action_sequence", "")})
    vids = {v["id"]: bid for bid in hits for v in versions[bid]}
    for r in read_dat(z, "BILL_VERSION_AUTHORS_TBL", lay["BILL_VERSION_AUTHORS_TBL"]):
        vid = r["bill_version_id"].strip()
        if vid in vids: authors.setdefault(vid, []).append({"name": norm(nn(r.get("name"))), "house": nn(r.get("house")), "contribution": norm(nn(r.get("contribution"))),
                                                             "primary": (r.get("primary_author_flg") or "").upper() == "Y"})
    # 2. one record per bill
    recs = {}
    for bid, b in bills.items():
        vs = sorted(versions[bid], key=lambda v: (v["date"], v["num"]))
        latest_id = (b.get("latest_bill_version_id") or "").strip() or vs[-1]["id"]
        lv = next((v for v in vs if v["id"] == latest_id), vs[-1])
        h = hits[bid].get(lv["id"]) or hits[bid][sorted(hits[bid])[-1]]
        session = (b.get("session_year") or bid[:8]).strip()
        num = f'{(b.get("measure_type") or "").strip()}{(b.get("measure_num") or "").strip()}' or bid[8:]
        hs = sorted(hist.get(bid, []), key=lambda x: (x["date"], int(x["seq"] or 0) if str(x["seq"]).isdigit() else 0))
        chapter = {"year": nn(b.get("chapter_year")), "num": nn(b.get("chapter_num"))} if nn(b.get("chapter_num")) else None
        rec = {"bill_id": bid, "session": session, "bill": num, "status": norm(nn(b.get("current_status")) or nn(b.get("measure_state"))),
               "location": norm(nn(b.get("current_location"))), "house": nn(b.get("current_house")), "chapter": chapter,
               "title": h["title"], "subject": lv["subject"], "urgency": lv["urgency"], "latest_version": {k: lv[k] for k in ("id", "num", "date", "action")},
               "authors": authors.get(lv["id"], []), "history": [{"date": x["date"], "action": x["action"]} for x in hs],
               "versions": [{k: v[k] for k in ("id", "num", "date", "action")} for v in vs], "digest": h["digest"], "sections": h["sections"]}
        rec["hash"] = sha(json.dumps({k: rec[k] for k in ("status", "chapter", "latest_version", "history", "sections")}, sort_keys=True))
        recs[f"{session} {num}"] = rec
    write(out, recs)
    return recs


def write(out, recs):
    out = pathlib.Path(out); (out / "changes").mkdir(parents=True, exist_ok=True)
    old = json.loads((out / "index.json").read_text(encoding="utf-8")) if (out / "index.json").exists() else None
    index = {}
    for key, r in sorted(recs.items()):
        d = out / r["session"]; d.mkdir(exist_ok=True)
        (d / f'{r["bill"]}.json').write_text(json.dumps(r, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        last = r["history"][-1] if r["history"] else {"date": "", "action": ""}
        index[key] = {"status": r["status"], "chapter": r["chapter"], "last_action": last["action"], "last_action_date": last["date"],
                      "sections": sorted({f'{s["code"]} {s["sec"]}' for s in r["sections"]}), "authors": [a["name"] for a in r["authors"] if a["primary"]] or [a["name"] for a in r["authors"]][:1],
                      "title": r["title"], "hash": r["hash"]}
    (out / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    today = dt.date.today().isoformat()
    ch = {"date": today, "new": sorted(k for k in index if old is not None and k not in old),
          "changed": sorted(k for k in index if old and k in old and old[k]["hash"] != index[k]["hash"]),
          "dropped": sorted(k for k in (old or {}) if k not in index), "first_run": old is None}
    ch["count"] = len(ch["new"]) + len(ch["changed"]) + len(ch["dropped"])
    if ch["count"]: (out / "changes" / f"{today}.json").write_text(json.dumps(ch, indent=1), encoding="utf-8")
    (out / "changes" / "latest.json").write_text(json.dumps(ch, indent=1), encoding="utf-8")
    log(f"bills written: {len(index)}; new {len(ch['new'])}, changed {len(ch['changed'])}, dropped {len(ch['dropped'])}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", default="config.json"); ap.add_argument("--out", default="bills")
    ap.add_argument("--source-zip"); ap.add_argument("--load-zip"); ap.add_argument("--inspect", action="store_true"); ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    cfg = json.loads(pathlib.Path(a.config).read_text(encoding="utf-8"))
    out = pathlib.Path(a.out); out.mkdir(parents=True, exist_ok=True)
    run = {"started": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "result": "running"}
    prev = json.loads((out / "last-run.json").read_text(encoding="utf-8")) if (out / "last-run.json").exists() else {}
    try:
        if a.source_zip: zpath, name, lm, loadz = a.source_zip, os.path.basename(a.source_zip), "local file", a.load_zip
        else:
            name, lm = newest_daily(); log("newest daily file:", name, lm)
            if not a.force and not a.inspect and prev.get("source") == name and prev.get("last_modified") == lm and prev.get("result") == "built":
                run.update(result="unchanged", source=name, last_modified=lm); return finish(out, run)
            tmp = pathlib.Path(tempfile.mkdtemp()); zpath = download(name, tmp / name); loadz = None
            try: loadz = download("pubinfo_load.zip", tmp / "pubinfo_load.zip")
            except Exception as e: log("pubinfo_load.zip not available:", e)
        recs = build(zpath, cfg, out, inspect=a.inspect, load_zip=loadz)
        if a.inspect: run["result"] = "inspected"; return
        run.update(result="built", source=name, last_modified=lm, bills=len(recs))
    except BaseException as e:
        run.update(result="failed", error=f"{type(e).__name__}: {e}"); raise
    finally:
        if not a.inspect: finish(out, run)


def finish(out, run):
    run["finished"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    (pathlib.Path(out) / "last-run.json").write_text(json.dumps(run, indent=1), encoding="utf-8")
    log("run:", run["result"])


if __name__ == "__main__":
    main()
