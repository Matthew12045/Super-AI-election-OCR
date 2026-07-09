# Thai Election Results — OCR & Extraction Pipeline

This project turns a folder of scanned Thai election result document images into a
filled-in CSV submission file. It runs in three stages: OCR the images to Markdown,
parse the Markdown into structured vote counts, and clean up the final CSV.

Two ways to run it are included:
- **`election.ipynb`** — the original notebook, designed for Google Colab (paths
  reference `./drive/MyDrive/...`)
- **`election_pipeline.py`** — a standalone command-line script with the same logic

## Pipeline Overview

```
images/*.png  ──(1. OCR)──▶  combined_output_google.md ──(2. Parse)──▶  final_submission_filled.csv ──(3. Clean)──▶  final_submission_filled.csv
```

| Stage | What it does |
|-------|---------------|
| 1. OCR | Sends each `.png` in a folder to Gemini for OCR, appends the extracted Markdown to a single combined file |
| 2. Extract | Parses the combined Markdown and fills vote counts into a submission template CSV |
| 3. Cleanup | Drops an unused, mangled-encoding column from the final CSV |

## How to Use

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Set your Gemini API key

Required for Stage 1 (OCR) only. Get one from
[Google AI Studio](https://aistudio.google.com/), then set it as an environment variable:

```bash
export GEMINI_API_KEY="your-key-here"
```

Stages 2 and 3 use only the standard library plus pandas — they don't need the API key
or the `google-genai` package. If you're only running those stages, you can skip this
step and use `--skip-ocr` below.

If running the notebook in Colab, mount Google Drive first so the `./drive/MyDrive/...`
paths resolve.

### 3. Run the pipeline

Run all three stages with the default paths (matches the notebook's paths):

```bash
python election_pipeline.py
```

Run with your own paths:

```bash
python election_pipeline.py \
    --images-dir ./data/images \
    --combined-md ocr_output.md \
    --template-csv ./data/submission_template_v3.csv \
    --output-csv final_submission_filled.csv
```

Skip stages — for example, to re-run extraction only after you already have a
combined Markdown file:

```bash
python election_pipeline.py --skip-ocr
python election_pipeline.py --skip-ocr --skip-cleanup
```

See every available option:

```bash
python election_pipeline.py --help
```

## Stage 1 — OCR Images to Markdown

Reads every `.png` in the images folder, sends each one to `gemini-3-flash-preview` with
an OCR prompt that preserves tables/layout as Markdown, and appends the result to the
combined output file under a `# --- Source Document: <filename> ---` header.

- **Input:** a folder of `.png` images
- **Output:** the combined Markdown file, plus a failed-files log if any files fail
- **Rate limiting:** sleeps 4.5s between requests (free-tier 15 RPM limit); retries each
  file up to 3 times with a 10s backoff on error

## Stage 2 — Extract Votes into CSV

Parses the combined Markdown and fills a submission template CSV with vote counts.

Handles several real-world messiness issues in the OCR output:
- **Thai ↔ Arabic numerals** — converts Thai digits before parsing vote counts
- **OCR noise** — strips stray dots/dashes/spaces around numbers (e.g. `--๐--`, `...๑๐...`)
- **Party name variants** — a lookup table (`MD_VARIANTS`) normalizes OCR misspellings and
  tone-mark inconsistencies in party names to a canonical form
- **Template typos** — a separate lookup (`TEMPLATE_FIXES`) corrects known typos in the
  template *only for matching purposes*, while preserving the original (typo'd) text in
  the output
- **Multi-page documents** — pages like `constituency_10_10_page2.png` are merged under a
  shared base ID (`constituency_10_10`); a non-zero vote value is never overwritten by a
  zero from another page
- **Party-list vs. constituency rows** — parsed with different column layouts
  (`party_number | party_name | votes` vs. `cand_number | cand_name | party | votes`)

- **Inputs:** the combined Markdown file, the template CSV
- **Output:** the filled-in CSV
- **Console output:** a fill-rate summary, plus a per-document report of any rows that
  couldn't be matched (template typo vs. genuinely absent from the OCR'd Markdown)

## Stage 3 — Drop Unused Column

Loads the filled-in CSV with pandas, drops the
`party_name (????????????????????????)` column (a mangled-encoding artifact from the
template), and re-saves the CSV in place.

- **Input / Output:** the filled-in CSV (overwritten)

## Known Limitations

- OCR accuracy depends entirely on Gemini's read of the source images — the party-name
  variant tables exist because OCR reliably mangles certain Thai party names in
  consistent ways, but new/unseen misreads will need to be added manually.
- Cover/summary pages with no vote table are logged but otherwise skipped silently.
- The notebook version still has the Gemini API key hardcoded as a placeholder string
  in Stage 1 — replace it before running that version, and avoid committing a real key
  to version control. The script version reads the key from `GEMINI_API_KEY` instead.
