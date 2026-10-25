#!/usr/bin/env python3
"""California statute snapshot from the Legislature's bulk download (pubinfo).

Downloads the current session file (pubinfo_<year>.zip, about 1.2 GB, reloaded every Sunday) from
https://downloads.leginfo.legislature.ca.gov/, reads only the Code tables (LAW_SECTION_TBL, LAW_TOC_TBL,
LAW_TOC_SECTIONS_TBL, CODES_TBL), keeps the codes and section ranges listed in config.json, and writes a compact,
diff-friendly snapshot:

  snapshot/manifest.json            what was read, when, from which file; counts and hashes per code
  snapshot/sections/<CODE>.jsonl    one line per active section version: text as paragraphs, headings, history, dates
  snapshot/toc/<CODE>.jsonl         the code's table of contents rows (divisions, parts, chapters, articles)
  snapshot/index.json               "<CODE> <section>" -> [{version, hash, effective}]
  snapshot/changes/<date>.json      what changed since the previous snapshot (also copied to changes/latest.json)
  snapshot/last-run.json            written every run (keeps a scheduled GitHub workflow active)

Standard library only (Python 3.9+). Usage:
  python scripts/pubinfo_snapshot.py --config config.json --out snapshot            # normal weekly run
  python scripts/pubinfo_snapshot.py --source-zip pubinfo_2025.zip ...              # use a file already on disk
  python scripts/pubinfo_snapshot.py --inspect ...                                  # print table layouts and sample rows, write nothing
  python scripts/pubinfo_snapshot.py --force ...                                    # rebuild even if the source file is unchanged

The pubinfo readme does not document columns, so column names and the field format are read from the SQL load
scripts in the download (capublic.sql / *_tbl.sql, also in pubinfo_load.zip). If they cannot be found, documented
fallbacks are used and the run says so. The text is the Legislature's; this snapshot is a convenience copy, not the
official publication (leginfo.legislature.ca.gov is).
"""
import argparse, csv, datetime as dt, hashlib, html.parser, io, json, os, pathlib, re, shutil, ssl, sys, tempfile, time, urllib.request, zipfile

SCHEMA_VERSION = 1
BASE = "https://downloads.leginfo.legislature.ca.gov/"
CODE_TABLES = ["CODES_TBL", "LAW_TOC_TBL", "LAW_TOC_SECTIONS_TBL", "LAW_SECTION_TBL"]
CELL = ""                      # separates table cells inside one table-row paragraph
WS = re.compile(r"[ \t\r\n ]+")
norm = lambda s: WS.sub(" ", s).strip()

# Fallback column orders (capublic schema), used only if the load scripts are missing from the download.
FALLBACK_COLUMNS = {
    "LAW_SECTION_TBL": ["id", "law_code", "section_num", "op_statues", "op_chapter", "op_section", "effective_date",
                        "law_section_version_id", "division", "title", "part", "chapter", "article", "history",
                        "content_xml", "active_flg", "trans_uid", "trans_update"],
    "LAW_TOC_TBL": ["law_code", "division", "title", "part", "chapter", "article", "heading", "active_flg", "trans_uid",
                    "trans_update", "node_sequence", "node_level", "node_position", "node_treepath", "contains_law_sections",
                    "history_note", "op_statues", "op_chapter", "op_section"],
    "LAW_TOC_SECTIONS_TBL": ["id", "law_code", "node_treepath", "section_num", "section_order", "title", "op_statues",
                             "op_chapter", "op_section", "trans_uid", "trans_update", "law_section_version_id", "seq_num"],
    "CODES_TBL": ["code", "title"],
}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- download
_TLS = {"ctx": None}                # set to the compatible context once the default one fails (see http)


def _compat_ctx():
    """Certificate checks unchanged; only accepts older cipher suites / renegotiation that some government servers still
    use. Needed when the runner's OpenSSL refuses the server's handshake (SSLV3_ALERT_HANDSHAKE_FAILURE)."""
    ctx = ssl.create_default_context()
    ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
    ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
    return ctx


def http(url, method="GET"):
    req = urllib.request.Request(url, method=method, headers={"User-Agent": "ca-statute-snapshot/1 (+weekly research snapshot)"})
    if _TLS["ctx"] is not None: return urllib.request.urlopen(req, timeout=120, context=_TLS["ctx"])
    try:
        return urllib.request.urlopen(req, timeout=120)
    except Exception as e:
        if "HANDSHAKE_FAILURE" not in str(e) and "UNSAFE_LEGACY_RENEGOTIATION" not in str(e): raise
        log(f"TLS handshake refused with {ssl.OPENSSL_VERSION} defaults; retrying with the compatible TLS settings")
        _TLS["ctx"] = _compat_ctx()
        return urllib.request.urlopen(req, timeout=120, context=_TLS["ctx"])


def latest_session_zip():
    """Newest pubinfo_<year>.zip in the download listing (the current session file)."""
    with http(BASE + "?C=N;O=D") as r:
        listing = r.read().decode("utf-8", "replace")
    years = sorted({int(y) for y in re.findall(r'href="pubinfo_(\d{4})\.zip"', listing)})
    if not years: raise SystemExit("No pubinfo_<year>.zip found in the download listing; the site layout may have changed.")
    return f"pubinfo_{years[-1]}.zip"


def head_info(name):
    with http(BASE + name, "HEAD") as r:
        return {"last_modified": r.headers.get("Last-Modified", ""), "length": int(r.headers.get("Content-Length") or 0), "etag": r.headers.get("ETag", "")}


def download(name, dest, tries=4):
    for k in range(1, tries + 1):
        try:
            log(f"downloading {name} (try {k})")
            with http(BASE + name) as r, open(dest, "wb") as f:
                shutil.copyfileobj(r, f, length=1 << 22)
            return dest
        except Exception as e:                      # network hiccups on a 1 GB file are normal: retry with backoff
            log("download failed:", e)
            if k == tries: raise
            time.sleep(30 * k)


# ---------------------------------------------------------------- table layout from the load scripts
def find_member(z, name):
    low = name.lower()
    for n in z.namelist():
        if n.lower().rsplit("/", 1)[-1] == low: return n
    return None


def load_script_text(zips, table):
    """The SQL that loads one table (e.g. law_section_tbl.sql), or the full schema (capublic.sql)."""
    out = {}
    for z in zips:
        for cand in (table.lower() + ".sql", "capublic.sql"):
            m = find_member(z, cand)
            if m and cand not in out: out[cand] = z.read(m).decode("utf-8", "replace")
    return out


def table_layout(zips, table):
    """Column names in .dat order, which columns hold a .lob file name, and the field format."""
    scripts = load_script_text(zips, table)
    fmt = {"delimiter": "\t", "quotechar": "`", "escapechar": "\\"}
    cols, lob_cols, source = None, set(), "fallback"
    load = scripts.get(table.lower() + ".sql", "")
    if load:
        m = re.search(r"TERMINATED\s+BY\s+'([^']*)'", load, re.I)
        if m: fmt["delimiter"] = m.group(1).encode().decode("unicode_escape")
        m = re.search(r"ENCLOSED\s+BY\s+'([^']*)'", load, re.I)
        if m and m.group(1): fmt["quotechar"] = m.group(1).encode().decode("unicode_escape")
        m = re.search(r"ESCAPED\s+BY\s+'([^']*)'", load, re.I)
        if m: fmt["escapechar"] = m.group(1).encode().decode("unicode_escape") or None
        m = re.search(r"LINES\s+TERMINATED\s+BY\s+'[^']*'\s*(?:IGNORE\s+\d+\s+LINES\s*)?\(([^)]*)\)", load, re.I | re.S)
        if m:
            raw = [c.strip().strip("`") for c in m.group(1).split(",") if c.strip()]
            sets = dict(re.findall(r"(\w+)\s*=\s*[^,;]*?(@\w+)", load.split(")", 1)[-1]))
            var2col = {v: c for c, v in sets.items()}
            cols = [var2col.get(c, c.lstrip("@")) for c in raw]
            lob_cols = {var2col[v] for v in var2col if re.search(r"LOAD_FILE", load, re.I)}
            source = table.lower() + ".sql"
    if cols is None and "capublic.sql" in scripts:
        m = re.search(r"CREATE\s+TABLE\s+`?" + table + r"`?\s*\((.*?)\)\s*(?:ENGINE|;)", scripts["capublic.sql"], re.I | re.S)
        if m:
            cols = [re.match(r"\s*`?(\w+)`?", ln).group(1) for ln in m.group(1).split("\n")
                    if re.match(r"\s*`?\w+`?\s+\w+", ln) and not re.match(r"\s*(PRIMARY|KEY|UNIQUE|INDEX|CONSTRAINT)\b", ln, re.I)]
            source = "capublic.sql"
    if cols is None: cols = FALLBACK_COLUMNS[table]
    return {"columns": [c.lower() for c in cols], "lob_columns": sorted(c.lower() for c in lob_cols), "format": fmt, "source": source}


def read_dat(z, table, layout):
    m = find_member(z, table + ".dat")
    if not m: raise SystemExit(f"{table}.dat is not in the download; the pubinfo layout may have changed.")
    raw = z.open(m)
    text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")
    f = layout["format"]
    rd = csv.reader(text, delimiter=f["delimiter"], quotechar=f["quotechar"], escapechar=f["escapechar"], doublequote=False, strict=False)
    cols = layout["columns"]
    for row in rd:
        if not row: continue
        if len(row) < len(cols): row = row + [""] * (len(cols) - len(row))
        yield dict(zip(cols, row))


# ---------------------------------------------------------------- section text from the content XML
class _Paras(html.parser.HTMLParser):
    """Paragraph texts from leginfo content XML: one entry per <p>, table rows as cells joined by CELL."""
    def __init__(self):
        super().__init__(convert_charrefs=True); self.out, self.buf, self.depth_p, self.row, self.cell, self.in_cell = [], [], 0, None, [], False
    def handle_starttag(self, tag, attrs):
        t = tag.split(":")[-1].lower()
        if t == "tr": self.row = []
        elif t in ("td", "th"): self.in_cell, self.cell = True, []
        elif t == "p" and self.row is None: self.depth_p += 1; self.buf = []
        elif t == "br": (self.cell if self.in_cell else self.buf).append(" ")
    def handle_endtag(self, tag):
        t = tag.split(":")[-1].lower()
        if t in ("td", "th") and self.row is not None: self.row.append(norm("".join(self.cell))); self.in_cell = False
        elif t == "tr" and self.row is not None:
            if any(self.row): self.out.append(CELL.join(self.row))
            self.row = None
        elif t == "p" and self.row is None and self.depth_p:
            self.depth_p -= 1; s = norm("".join(self.buf))
            if s: self.out.append(s)
            self.buf = []
    def handle_data(self, d):
        if self.in_cell: self.cell.append(d)
        elif self.depth_p: self.buf.append(d)


LABELS = re.compile(r"^((?:\([0-9A-Za-z.]{1,8}\)\s*)+)(?=\S)")


def space_labels(s):
    """leginfo XML separates a subdivision label from its text with an empty spacing element, which leaves
    "(a)For" in plain text; restore "(a) For" and "(b) (1) Except" (only at the start of a paragraph)."""
    return LABELS.sub(lambda m: " ".join(re.findall(r"\([0-9A-Za-z.]{1,8}\)", m.group(1))) + " ", s, count=1)


def xml_paras(xml):
    p = _Paras(); p.feed(xml); p.close()
    if not p.out:                                   # no <p> markup: fall back to the whole text
        s = norm(re.sub(r"<[^>]+>", " ", xml))
        return [s] if s else []
    return [space_labels(x) if CELL not in x else x for x in p.out]


# ---------------------------------------------------------------- selection
def num_key(sec):
    parts = re.findall(r"\d+", sec)
    return tuple(int(x) for x in parts) if parts else (float("inf"),)


def in_ranges(sec, spec):
    """spec: {"all": true} | {"sections": [...], "ranges": [[from, to_exclusive], ...]}"""
    if spec.get("all"): return True
    if sec in spec.get("sections", []): return True
    k = num_key(sec)
    for a, b in spec.get("ranges", []):
        if num_key(str(a)) <= k < num_key(str(b)): return True
    return False


def sha(s): return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- main build
def build(zpath, cfg, out, inspect=False, load_zip=None):
    zips = [zipfile.ZipFile(zpath)]
    if load_zip: zips.append(zipfile.ZipFile(load_zip))
    z = zips[0]
    layouts = {t: table_layout(zips, t) for t in CODE_TABLES}
    for t, L in layouts.items(): log(f"{t}: {len(L['columns'])} columns from {L['source']}; LOB columns {L['lob_columns'] or 'auto-detected'}")
    if inspect:
        for t in CODE_TABLES:
            print(f"\n== {t}  columns: {layouts[t]['columns']}")
            for i, r in enumerate(read_dat(z, t, layouts[t])):
                print(json.dumps(r, ensure_ascii=False)[:600])
                if i >= 2: break
        return None
    codes = cfg["codes"]
    names = {n.lower(): n for n in z.namelist()}
    sections, problems = {c: [] for c in codes}, []
    for r in read_dat(z, "LAW_SECTION_TBL", layouts["LAW_SECTION_TBL"]):
        code, sec = (r.get("law_code") or "").strip(), (r.get("section_num") or "").strip().rstrip(".")
        if code not in codes or not sec or not in_ranges(sec, codes[code]): continue
        if (r.get("active_flg") or "Y").strip().upper() not in ("Y", ""): continue
        lob = next((v for k, v in r.items() if (k in layouts["LAW_SECTION_TBL"]["lob_columns"] or k == "content_xml") and v.lower().endswith(".lob")), "")
        if not lob: lob = next((v for v in r.values() if v.lower().endswith(".lob")), "")
        xml = ""
        if lob:
            m = names.get(lob.lower().rsplit("/", 1)[-1]) or next((n for k, n in names.items() if k.endswith("/" + lob.lower())), None)
            if m: xml = z.read(m).decode("utf-8", "replace")
            else: problems.append(f"{code} {sec}: LOB file {lob} not found")
        elif r.get("content_xml"): xml = r["content_xml"]
        paras = xml_paras(xml) if xml else []
        if not paras: problems.append(f"{code} {sec}: no text")
        heads = {k: norm(r.get(k) or "") for k in ("division", "title", "part", "chapter", "article") if norm(r.get(k) or "") not in ("", "NULL")}
        sections[code].append({"code": code, "sec": sec, "version": (r.get("law_section_version_id") or r.get("id") or "").strip(),
                               "effective": (r.get("effective_date") or "")[:10], "op": {k: (r.get(k) or "").strip() for k in ("op_statues", "op_chapter", "op_section")},
                               "heads": heads, "history": norm(r.get("history") or ""), "paras": paras, "hash": sha("\n".join(paras))})
    toc = {c: [] for c in codes}
    for r in read_dat(z, "LAW_TOC_TBL", layouts["LAW_TOC_TBL"]):
        c = (r.get("law_code") or "").strip()
        if c in codes and (r.get("active_flg") or "Y").strip().upper() in ("Y", ""):
            toc[c].append({k: norm(v) for k, v in r.items() if k not in ("trans_uid", "trans_update") and norm(v or "") not in ("", "NULL")})
    write_snapshot(out, cfg, sections, toc, problems)
    return sections


def write_snapshot(out, cfg, sections, toc, problems):
    out = pathlib.Path(out); (out / "sections").mkdir(parents=True, exist_ok=True); (out / "toc").mkdir(exist_ok=True); (out / "changes").mkdir(exist_ok=True)
    old_index = json.loads((out / "index.json").read_text(encoding="utf-8")) if (out / "index.json").exists() else None
    index = {}
    for code, rows in sections.items():
        rows.sort(key=lambda x: (num_key(x["sec"]), x["version"]))
        with open(out / "sections" / f"{code}.jsonl", "w", encoding="utf-8", newline="\n") as f:
            for x in rows:
                f.write(json.dumps(x, ensure_ascii=False, sort_keys=True) + "\n")
                index.setdefault(f"{code} {x['sec']}", []).append({"version": x["version"], "hash": x["hash"], "effective": x["effective"]})
        with open(out / "toc" / f"{code}.jsonl", "w", encoding="utf-8", newline="\n") as f:
            for t in toc.get(code, []): f.write(json.dumps(t, ensure_ascii=False, sort_keys=True) + "\n")
    (out / "index.json").write_text(json.dumps(index, ensure_ascii=False, sort_keys=True, indent=0), encoding="utf-8")
    today = dt.date.today().isoformat()
    if old_index is not None:
        hs = lambda v: sorted(e["hash"] for e in v)
        order = lambda k: (k.split()[0], num_key(k.split()[1]))
        ch = {"date": today, "added": sorted((k for k in index if k not in old_index), key=order),
              "removed": sorted((k for k in old_index if k not in index), key=order),
              "changed": sorted((k for k in index if k in old_index and hs(index[k]) != hs(old_index[k])), key=order)}
        ch["count"] = len(ch["added"]) + len(ch["removed"]) + len(ch["changed"])
        if ch["count"]:
            (out / "changes" / f"{today}.json").write_text(json.dumps(ch, indent=1), encoding="utf-8")
        (out / "changes" / "latest.json").write_text(json.dumps(ch, indent=1), encoding="utf-8")
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8")) if (out / "manifest.json").exists() else {}
    manifest.update({"schema_version": SCHEMA_VERSION, "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                     "codes": {c: {"sections": len(index_c), "versions": len(rows), "hash": sha("".join(x["hash"] for x in rows))}
                               for c, rows in sections.items() for index_c in [[k for k in index if k.startswith(c + " ")]]},
                     "config": cfg.get("codes"), "problems": problems[:200], "problem_count": len(problems),
                     "note": "Convenience copy of California Code text from the Legislature's pubinfo download. The official text is on leginfo.legislature.ca.gov."})
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")
    log("snapshot written:", {c: len(v) for c, v in sections.items()}, f"problems: {len(problems)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", default="config.json"); ap.add_argument("--out", default="snapshot")
    ap.add_argument("--source-zip"); ap.add_argument("--load-zip"); ap.add_argument("--force", action="store_true"); ap.add_argument("--inspect", action="store_true")
    a = ap.parse_args()
    cfg = json.loads(pathlib.Path(a.config).read_text(encoding="utf-8"))
    out = pathlib.Path(a.out); out.mkdir(parents=True, exist_ok=True)
    run = {"started": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "result": "running"}
    man = json.loads((out / "manifest.json").read_text(encoding="utf-8")) if (out / "manifest.json").exists() else {}
    try:
        if a.source_zip:
            zpath, info, name, loadz = a.source_zip, {"last_modified": "local file"}, os.path.basename(a.source_zip), a.load_zip
        else:
            name = latest_session_zip(); info = head_info(name)
            log("current session file:", name, info)
            if not a.force and not a.inspect and man.get("source", {}).get("file") == name and man.get("source", {}).get("last_modified") == info["last_modified"]:
                run.update(result="unchanged", source=name, last_modified=info["last_modified"])
                log("source unchanged since the last snapshot; nothing to do (use --force to rebuild)")
                return finish(out, run)
            tmp = pathlib.Path(tempfile.mkdtemp()); zpath = download(name, tmp / name)
            loadz = None
            try: loadz = download("pubinfo_load.zip", tmp / "pubinfo_load.zip")
            except Exception as e: log("pubinfo_load.zip not available:", e)
        built = build(zpath, cfg, out, inspect=a.inspect, load_zip=loadz)
        if a.inspect: run["result"] = "inspected"; return
        m = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        m["source"] = {"file": name, "last_modified": info.get("last_modified", ""), "url": BASE + name}
        (out / "manifest.json").write_text(json.dumps(m, indent=1, ensure_ascii=False), encoding="utf-8")
        run.update(result="built", source=name, sections={c: len(v) for c, v in built.items()}, problems=m.get("problem_count", 0))
    except BaseException as e:
        run.update(result="failed", error=f"{type(e).__name__}: {e}")
        raise
    finally:
        if not a.inspect: finish(out, run)


def finish(out, run):
    run["finished"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    (pathlib.Path(out) / "last-run.json").write_text(json.dumps(run, indent=1), encoding="utf-8")
    log("run:", run["result"])


if __name__ == "__main__":
    main()
