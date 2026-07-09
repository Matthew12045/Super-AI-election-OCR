#!/usr/bin/env python3


import argparse
import csv
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import pandas as pd


# ─────────────────────────────────────────────────────────────
# Thai digit → Arabic digit translation table
# ─────────────────────────────────────────────────────────────
THAI_TO_ARABIC = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")


# ─────────────────────────────────────────────────────────────
# MD-side party name variants → canonical template name
# Handles OCR errors, tone-mark variants, and prefix differences.
# ─────────────────────────────────────────────────────────────
MD_VARIANTS = {
    # ฟิวชัน variants
    "ฟิวชั่น":                   "ฟิวชัน",
    "ฟิวชน":                     "ฟิวชัน",
    "ฟิวเจอร์":                  "ฟิวชัน",
    # ไทรวมพลัง variants
    "ไทยรวมพลัง":                "ไทรวมพลัง",
    # รวมพลังประชาชน OCR error (ขน → ชน)
    "รวมพลังประชาขน":            "รวมพลังประชาชน",
    # สังคมประชาธิปไตยไทย truncation
    "สังคมประชาธิปไตย":          "สังคมประชาธิปไตยไทย",
    # เศรษฐกิจ regional suffix
    "เศรษฐกิจไทย":               "เศรษฐกิจ",
    # ครูไทยเพื่อประชาชน missing ค
    "รูไทยเพื่อประชาชน":         "ครูไทยเพื่อประชาชน",
    # สร้างอนาคตไทย tone-mark swap
    "สร่างอนาคตไทย":             "สร้างอนาคตไทย",
    # วิชชั่นใหม่ variants
    "วิชชันใหม่":                 "วิชชั่นใหม่",
    "วิชั่นใหม่":                 "วิชชั่นใหม่",
    "วิชันใหม่":                  "วิชชั่นใหม่",
}


# ─────────────────────────────────────────────────────────────
# Template-side typos → corrected lookup key
# These are errors in the template itself; we fix them only for
# the purpose of looking up the value in vote_data, but write
# the original (typo'd) name back to the output to preserve
# template fidelity.
# ─────────────────────────────────────────────────────────────
TEMPLATE_FIXES = {
    "ไทยก้าวใหม":               "ไทยก้าวใหม่",    # truncated sara-mai ek
    "กลาธรรม":                   "กล้าธรรม",       # missing mai tho
    "รวมไทยสร้างชา":             "รวมไทยสร้างชาติ", # truncated
}


# ============================================================
# Stage 1 — OCR images to Markdown
# ============================================================

def extract_text_with_gemini(client, image_path, model):
    """Reads the image and sends it to Gemini for OCR."""
    # google-genai is only needed for this stage; imported lazily so Stages
    # 2/3 can run without it installed.
    from google.genai import types

    # Read the image file as raw bytes
    with open(image_path, "rb") as image_file:
        image_bytes = image_file.read()

    prompt = (
        "You are a highly accurate OCR system. Extract all text from this image. "
        "Preserve the layout, tables, and structure using Markdown. Output ONLY "
        "the extracted markdown text. Do not add any conversational filler or "
        "pleasantries."
    )

    # Send both the prompt and the image bytes to Gemini
    response = client.models.generate_content(
        model=model,  # Flash is extremely fast and natively multimodal
        contents=[
            prompt,
            types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
        ],
    )
    return response.text


def ocr_images_to_markdown(
    client,
    directory_path,
    output_filename,
    error_log_filename,
    model="gemini-3-flash-preview",
    max_retries=3,
    request_delay=4.5,
    retry_delay=10.0,
):
    """OCRs every .png in `directory_path` and appends the Markdown output to
    `output_filename`. Files that fail after `max_retries` attempts are
    recorded in `error_log_filename`.
    """
    folder = Path(directory_path)

    # Filter specifically for .png files
    files_to_process = [f for f in folder.iterdir() if f.is_file() and f.suffix.lower() == ".png"]

    total_files = len(files_to_process)
    print(f"Found {total_files} PNG files to process. Starting Google Gemini OCR...\n")

    failed_files = []

    with open(output_filename, "a", encoding="utf-8") as output_file:

        for index, file_path in enumerate(files_to_process, start=1):
            print(f"[{index}/{total_files}] Processing {file_path.name}...")

            success = False

            for attempt in range(1, max_retries + 1):
                try:
                    # 1. Extract text using Gemini
                    markdown_text = extract_text_with_gemini(client, file_path, model)

                    # 2. Write separator and content
                    output_file.write(f"\n\n# --- Source Document: {file_path.name} ---\n\n")
                    output_file.write(markdown_text)

                    print("  -> Success! Text saved.")
                    success = True

                    # 3. CRITICAL: Pause to respect the free-tier RPM limit
                    time.sleep(request_delay)
                    break  # Break out of retry loop on success

                except Exception as e:
                    print(f"  -> Error on attempt {attempt}: {e}")
                    if attempt < max_retries:
                        print("  -> Waiting before retrying...")
                        time.sleep(retry_delay)  # Longer wait on failure to let the rate limit reset

            if not success:
                print(f"  -> Giving up on {file_path.name} after {max_retries} attempts.")
                failed_files.append(file_path.name)

    print("\n--- Batch OCR Complete ---")
    print(f"Successfully processed {total_files - len(failed_files)}/{total_files} files.")

    if failed_files:
        print(f"\nWarning: {len(failed_files)} files failed completely. Saving list to {error_log_filename}")
        with open(error_log_filename, "w", encoding="utf-8") as error_file:
            for failed_doc in failed_files:
                error_file.write(f"{failed_doc}\n")


# ============================================================
# Stage 2 — Extract votes from Markdown into CSV
# ============================================================

def parse_vote_number(raw: str) -> int:
    """
    Extract the first integer from a Thai/Arabic mixed string.

    Examples:
        "๔๑,๘๐๔ (สี่หมื่น...)"     → 41804
        "--๐-- บัตร (ศูนย์)"        → 0
        "...๑๐......."              → 10  (dotted OCR noise)
        "6,372"                     → 6372
    """
    s = raw.strip()
    if re.match(r"^[-\s]*[๐0][-\s]*$", s):
        return 0
    s = s.translate(THAI_TO_ARABIC)
    s = re.sub(r"-+0-+", "0", s)     # "--0--" style zeros
    s = re.sub(r"[.\s]+", "", s)     # remove OCR noise dots/spaces
    m = re.search(r"\d[\d,]*", s)
    return int(m.group().replace(",", "")) if m else 0


def normalize_md_party(raw: str) -> str:
    """Normalize a party name extracted from the Markdown."""
    # 1. Strip strikethrough markdown: ~~text~~ → text
    name = re.sub(r"~~(.+?)~~", r"\1", raw)
    # 2. Strip leading "พรรค" prefix (very common in Google-Vision output)
    name = re.sub(r"^พรรค", "", name)
    # 3. Apply known OCR/variant fixes
    return MD_VARIANTS.get(name, name)


def is_separator_row(cells: list) -> bool:
    return all(re.match(r"^[-:]+$", c) for c in cells)


def is_header_row(cells: list) -> bool:
    kw = ["ชื่อ", "สังกัด", "พรรคการเมือง", "หมายเลข",
          "ผู้สมัคร", "ได้คะแนน", "หมายเหตุ"]
    return any(k in "".join(cells) for k in kw)


def is_total_row(cells: list) -> bool:
    return "รวมคะแนน" in "".join(cells)


def parse_markdown(md_path: Path) -> dict:
    """
    Parse the Markdown and return:
        { doc_base: { party_name: votes } }

    Multiple pages of the same document are merged.
    A positive vote count from any page will not be overwritten by zero.
    """
    text = md_path.read_text(encoding="utf-8")
    parts = re.split(r"#\s+---\s+Source Document:\s+(.+?)\s+---", text)

    vote_data = {}
    empty_docs = []

    for i in range(1, len(parts), 2):
        filename = parts[i].strip()
        body = parts[i + 1] if i + 1 < len(parts) else ""

        # constituency_10_10_page2.png  →  constituency_10_10
        base = re.sub(r"\.png$", "", filename, flags=re.IGNORECASE)
        base = re.sub(r"_page\d+$", "", base)

        if base not in vote_data:
            vote_data[base] = {}

        is_party_list = base.startswith("party_list")
        found_any = False

        for line in body.splitlines():
            line = line.strip()
            if not line.startswith("|"):
                continue

            cells = [
                re.sub(r"\*+", "", c).strip()
                for c in line.split("|")
                if c.strip()
            ]

            if not cells:
                continue
            if is_separator_row(cells):
                continue
            if is_header_row(cells):
                continue
            if is_total_row(cells):
                continue

            try:
                if is_party_list:
                    # | party_number | party_name | votes | [notes] |
                    if len(cells) < 3:
                        continue
                    party_raw = cells[1]
                    votes = parse_vote_number(cells[2])
                else:
                    # | cand_number | cand_name | party | votes | [extra…] |
                    if len(cells) < 4:
                        continue
                    party_raw = cells[2]
                    # Find the first non-empty cell at index ≥ 3 for votes
                    votes_raw = next(
                        (cells[j] for j in range(3, len(cells)) if cells[j]),
                        "0"
                    )
                    votes = parse_vote_number(votes_raw)
            except Exception:
                continue

            party = normalize_md_party(party_raw)
            if not party:
                continue

            # Keep non-zero value; never overwrite a known value with zero
            if party not in vote_data[base] or votes > 0:
                vote_data[base][party] = votes
            found_any = True

        if not found_any:
            empty_docs.append(filename)

    if empty_docs:
        print(
            f"[INFO] {len(empty_docs)} source page(s) had no vote table "
            "(typically cover/summary pages).",
            file=sys.stderr,
        )

    return vote_data


def fill_template(template_path: Path, vote_data: dict, output_path: Path):
    """
    Read template CSV, look up votes, and write output.
    Template-side typos are corrected only for lookup; original names
    are preserved in the output.
    """
    with template_path.open(encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = list(reader)

    total = filled = still_zero = 0
    output = [header]
    zero_report = defaultdict(list)

    for row in rows:
        if len(row) < 3:
            output.append(row)
            continue

        row_id, party_name = row[0], row[1]
        doc_base = re.sub(r"_\d+$", "", row_id)

        # Fix template-side typos only for lookup
        lookup_name = TEMPLATE_FIXES.get(party_name, party_name)
        votes = vote_data.get(doc_base, {}).get(lookup_name, 0)

        total += 1
        if votes != 0:
            filled += 1
        else:
            still_zero += 1
            zero_report[doc_base].append(party_name)

        output.append([row_id, party_name, str(votes)])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(output)

    print(f"[DONE] {total:,} rows written → {output_path}")
    print(f"       Filled (non-zero) : {filled:,}")
    print(f"       Still zero        : {still_zero:,}")

    if still_zero:
        print(f"\n[ZERO REPORT] These {still_zero} rows could not be filled:")
        for base in sorted(zero_report):
            print(f"  [{base}]")
            for p in zero_report[base]:
                label = "(template typo)" if not p else "(absent from MD)"
                print(f"    '{p or '<blank>'}' {label}")


def extract_votes_to_csv(md_path: Path, template_path: Path, output_path: Path):
    """Parses `md_path` and fills `template_path` with vote counts, writing
    the result to `output_path`.
    """
    print(f"[1/2] Parsing: {md_path}")
    vote_data = parse_markdown(md_path)
    print(f"      {len(vote_data):,} source documents parsed.")

    print(f"[2/2] Filling: {template_path}")
    fill_template(template_path, vote_data, output_path)


# ============================================================
# Stage 3 — Drop unused column
# ============================================================

def drop_unused_column(csv_path, column_name):
    """Drops `column_name` from `csv_path` in place, if present."""
    df = pd.read_csv(csv_path)

    if column_name not in df.columns:
        print(f"Column '{column_name}' not found in '{csv_path}' — skipping.")
        return

    df = df.drop(columns=[column_name])
    df.to_csv(csv_path, index=False)
    print(f"Dropped column '{column_name}'. File saved as '{csv_path}'")


# ============================================================
# CLI
# ============================================================

def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Thai election results pipeline: OCR ballot images, "
                     "extract vote counts into a CSV template, and clean up the result."
    )
    parser.add_argument(
        "--images-dir", default="./drive/MyDrive/final_data/images",
        help="Folder of .png ballot images to OCR (default: %(default)s)",
    )
    parser.add_argument(
        "--combined-md", default="combined_output_google.md",
        help="Path to write/read the combined OCR Markdown file (default: %(default)s)",
    )
    parser.add_argument(
        "--error-log", default="failed_files_google.txt",
        help="Path to write the list of files that failed OCR (default: %(default)s)",
    )
    parser.add_argument(
        "--template-csv", default="./drive/MyDrive/final_data/submission_template_v3.csv",
        help="Submission template CSV to fill in (default: %(default)s)",
    )
    parser.add_argument(
        "--output-csv", default="final_submission_filled.csv",
        help="Path to write the filled-in CSV (default: %(default)s)",
    )
    parser.add_argument(
        "--unused-column", default="party_name (????????????????????????)",
        help="Mangled-encoding column to drop from the final CSV (default: %(default)s)",
    )
    parser.add_argument(
        "--model", default="gemini-3-flash-preview",
        help="Gemini model used for OCR (default: %(default)s)",
    )
    parser.add_argument(
        "--max-retries", type=int, default=3,
        help="Retry attempts per image on OCR failure (default: %(default)s)",
    )
    parser.add_argument(
        "--skip-ocr", action="store_true",
        help="Skip Stage 1 (OCR) and reuse an existing --combined-md file",
    )
    parser.add_argument(
        "--skip-cleanup", action="store_true",
        help="Skip Stage 3 (dropping the unused column) after extraction",
    )
    return parser


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    # ---------------- Stage 1 ----------------
    if not args.skip_ocr:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            sys.exit(
                "ERROR: GEMINI_API_KEY environment variable is not set.\n"
                "       Set it with: export GEMINI_API_KEY=\"your-key-here\"\n"
                "       or pass --skip-ocr to reuse an existing --combined-md file."
            )

        try:
            from google import genai
        except ImportError:
            sys.exit("ERROR: google-genai is not installed. Run: pip install google-genai")

        client = genai.Client()
        ocr_images_to_markdown(
            client,
            args.images_dir,
            args.combined_md,
            args.error_log,
            model=args.model,
            max_retries=args.max_retries,
        )

    # ---------------- Stage 2 ----------------
    md_path = Path(args.combined_md)
    template_path = Path(args.template_csv)
    output_path = Path(args.output_csv)

    for p in (md_path, template_path):
        if not p.exists():
            sys.exit(f"ERROR: File not found: {p}")

    extract_votes_to_csv(md_path, template_path, output_path)

    # ---------------- Stage 3 ----------------
    if not args.skip_cleanup:
        drop_unused_column(output_path, args.unused_column)


if __name__ == "__main__":
    main()
