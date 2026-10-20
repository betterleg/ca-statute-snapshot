#!/usr/bin/env python3
"""Read a statute snapshot from another project (no dependencies).

  python scripts/read_snapshot.py --dir snapshot GOV 65915        # print the current text of a section
  python scripts/read_snapshot.py --dir snapshot --changes        # what the latest weekly run changed
  python scripts/read_snapshot.py --dir bills --bills             # bills that touch tracked sections (daily workflow)

In Python:
  from read_snapshot import Snapshot
  s = Snapshot("snapshot"); s.section("GOV", "65915")  -> list of versions (dicts with "paras", "history", ...)
"""
import argparse, json, pathlib

class Snapshot:
    def __init__(self, d):
        self.dir = pathlib.Path(d); self._cache = {}
    def code(self, code):
        if code not in self._cache:
            f = self.dir / "sections" / f"{code}.jsonl"
            rows = [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []
            idx = {}
            for x in rows: idx.setdefault(x["sec"], []).append(x)
            self._cache[code] = idx
        return self._cache[code]
    def section(self, code, sec): return self.code(code).get(sec, [])
    def changes(self):
        f = self.dir / "changes" / "latest.json"
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
    def manifest(self): return json.loads((self.dir / "manifest.json").read_text(encoding="utf-8"))

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--dir", default="snapshot"); ap.add_argument("--changes", action="store_true"); ap.add_argument("--bills", action="store_true")
    ap.add_argument("code", nargs="?"); ap.add_argument("sec", nargs="?"); a = ap.parse_args()
    s = Snapshot(a.dir)
    if a.bills:
        idx = json.loads((pathlib.Path(a.dir) / "index.json").read_text(encoding="utf-8"))
        for k, b in sorted(idx.items(), key=lambda kv: kv[1]["last_action_date"], reverse=True):
            ch = f' (Ch. {b["chapter"]["num"]}, Stats. {b["chapter"]["year"]})' if b.get("chapter") else ""
            print(f'{k}{ch}  {b["status"]}  {b["last_action_date"]}  {", ".join(b["sections"])}  {"; ".join(b["authors"])}')
    elif a.changes: print(json.dumps(s.changes(), indent=1))
    elif a.code and a.sec:
        for v in s.section(a.code, a.sec):
            print(f"{a.code} § {a.sec}  (version {v['version']}, effective {v['effective'] or 'n/a'})"); print("\n".join(p.replace("\ue010", " | ") for p in v["paras"])); print(v["history"]); print()
    else: print(json.dumps(s.manifest(), indent=1)[:2000])
