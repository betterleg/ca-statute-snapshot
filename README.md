# CA Statute Snapshot

A weekly, automatic copy of selected California Code sections, plus a daily list of the bills that change them, taken from the Legislature's official bulk download and kept in this repository as plain text files. Built for the Housing Laws Navigator, and usable by any other project that needs current California statute text.

- **Source:** the Legislature's bulk download at <https://downloads.leginfo.legislature.ca.gov/>. The Code tables are in the session file (`pubinfo_2025.zip` for the 2025–26 session, about 1.2 GB), reloaded every Sunday night.
- **Schedule:** a GitHub workflow runs every Monday morning (13:15 UTC). It downloads the file only if the Legislature has published a new one, keeps the codes and sections listed in `config.json`, and commits the result to `snapshot/`.
- **Cost:** none on a public repository (GitHub Actions minutes are free for public repositories).
- **Official text:** this is a convenience copy. The official publication is <https://leginfo.legislature.ca.gov/>.

## What you get

```
snapshot/
  manifest.json              when and from which file the snapshot was built; counts per code; any problems found
  sections/GOV.jsonl         one line per section version: paragraphs, headings, history note, dates, text hash
  sections/HSC.jsonl, ...    one file per configured code
  toc/GOV.jsonl              the code's table of contents (divisions, titles, parts, chapters, articles)
  index.json                 "GOV 65915" -> versions and hashes, for quick change checks
  changes/latest.json        what the latest run added, removed or changed
  changes/<date>.json        the same, kept for every week that had changes
  last-run.json              written every run, including runs with nothing new
```

Each line of `sections/<CODE>.jsonl` looks like:

```json
{"code": "GOV", "sec": "65915", "version": "…", "effective": "2026-01-01", "op": {"op_statues": "2025", "op_chapter": "…", "op_section": "…"},
 "heads": {"title": "7.", "division": "1.", "chapter": "4.3."}, "history": "(Amended by Stats. 2025, Ch. …)",
 "paras": ["(a) (1) When an applicant seeks a density bonus …", "…"], "hash": "…"}
```

Paragraphs keep the statute's wording; runs of spaces are collapsed. Table rows are one paragraph with cells separated by the character U+E010.

## Set up (in the GitHub website; no command line needed)

1. **Sign in to GitHub** (or create a free account). Decide who owns the repository: a personal account, or an organization account managed by IT.
2. **Create the repository.** Select **New repository**. Name it `ca-statute-snapshot`, choose **Public**, leave "Add a README" unchecked, and select **Create repository**.
3. **Upload the files.** On the new repository's page, select **uploading an existing file**. Unzip `ca-statute-snapshot.zip` on your computer, open the unzipped folder, select everything inside it (including the `.github` folder), and drag it onto the upload area. Check that the list shows `.github/workflows/weekly-snapshot.yml`, `scripts/pubinfo_snapshot.py`, `config.json` and `README.md`. Select **Commit changes**.
   - If the `.github` folder does not upload: select **Add file → Create new file**, type `.github/workflows/weekly-snapshot.yml` as the name (the slashes create the folders), paste the file's contents, and commit.
4. **Let the workflow save its results.** Go to **Settings → Actions → General → Workflow permissions**, choose **Read and write permissions**, and select **Save**.
5. **Run it once now.** Go to the **Actions** tab (enable workflows if GitHub asks), select **Weekly statute snapshot**, then **Run workflow**. Leave the boxes unchecked and select the green **Run workflow** button. The first run downloads about 1.2 GB and takes roughly 5–20 minutes. When it shows a green check, a `snapshot` folder appears in the repository.
   - If it fails, open the run and the **Build snapshot** step to read the message. Running it again with **inspect** checked prints the table layouts and sample rows without saving anything, which helps diagnose a change in the Legislature's file format.
6. **Share the repository address** (for example `https://github.com/your-name/ca-statute-snapshot`) with whoever maintains the projects that read it. Only the address is needed. Never share a GitHub password or token.

After that it runs by itself every Monday. GitHub turns off scheduled workflows in repositories with no activity for 60 days; the weekly `last-run.json` commit keeps it active.

## Daily bills (second workflow)

The Legislature has no public bill API. Its own bill data, the same database behind leginfo.legislature.ca.gov, is published every night in `pubinfo_daily_<Day>.zip` (about 1.1 GB: all current-session bills, every version's text, history, authors and votes; no Code tables). The **Daily bills** workflow (`.github/workflows/daily-bills.yml`, 13:45 UTC every day) reads the newest one and keeps only the bills whose text adds, amends or repeals a section in `config.json`:

```
bills/
  index.json                   "20252026 AB1457" -> status, chapter, latest action and date, sections, author, title, hash
  20252026/AB1457.json         status, location, chapter, authors, full history, every version (date, action),
                               digest, and the latest version's new text of each tracked section
  changes/latest.json          bills new to the list, changed (status, history, new version) or dropped since the last run
  changes/<date>.json          the same, kept for every day with changes
  last-run.json                written every run
```

Set up: nothing extra. It is uploaded with the other files; it appears in the **Actions** tab as **Daily bills**. Run it once by hand with **inspect** checked to confirm the bill table layouts, then once normally.

Read it: `python scripts/read_snapshot.py --dir bills --bills` lists the bills; any project can read `bills/index.json` and the per-bill files directly.

## Change what is kept

Edit `config.json` in the GitHub website (open it, select the pencil icon, edit, commit). Codes use the Legislature's abbreviations (GOV, HSC, PRC, CIV, EVID, …). For each code, list exact `sections`, `ranges` (from a section number up to, but not including, another), or `"all": true` for the whole code. Whole codes are large: the Government Code alone is many thousands of sections. The next run picks up the change; run the workflow by hand with **force** checked to rebuild right away.

## Use it from another project

- **Get the files:** `git clone --depth 1 https://github.com/<owner>/ca-statute-snapshot.git` (or download the repository as a ZIP from GitHub).
- **Read a section:** `python scripts/read_snapshot.py --dir snapshot GOV 65915`
- **See this week's changes:** `python scripts/read_snapshot.py --dir snapshot --changes`
- **In Python:** `from read_snapshot import Snapshot; Snapshot("snapshot").section("GOV", "65915")`
- **Check for changes cheaply:** compare `index.json` hashes with the ones you stored last time.

To start a separate snapshot for another project, make a copy of this repository (or upload the same files to a new one) and change `config.json`.

## Run or test it on a computer (optional)

```
python scripts/pubinfo_snapshot.py --config config.json --out snapshot                 # download and build
python scripts/pubinfo_snapshot.py --source-zip pubinfo_2025.zip --out snapshot        # use a file you already downloaded
python scripts/pubinfo_snapshot.py --inspect                                           # print table layouts and sample rows only
python tests/test_snapshot.py                                                          # offline test with a small fake download
```

Python 3.9 or later, standard library only.

## How it works

The Legislature's readme lists the Code tables (`CODES_TBL`, `LAW_TOC_TBL`, `LAW_TOC_SECTIONS_TBL`, `LAW_SECTION_TBL`) as tab-delimited `.dat` files, with each section's text in its own `.lob` file of XML, plus MySQL load scripts. It does not document the columns, so the script reads the column order and field format from the load scripts in the download (falling back to the published capublic schema and saying so in the log). It reads only the Code tables straight out of the zip (no full unpack), keeps active rows for the configured codes and ranges, turns each section's XML into paragraphs, and compares the result with the previous week to write the change report.

The first run against the real file is also the first real test of the parser; if the Legislature's columns differ from the expectations above, the run log (and an **inspect** run) shows what to adjust in `scripts/pubinfo_snapshot.py`.
