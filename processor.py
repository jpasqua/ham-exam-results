import re
import pdfplumber
import json
import os
from datetime import datetime
from pathlib import Path

DATE_PARSE_FORMATS = (
    "%B %d, %Y %H:%M:%S UTC",
    "%B %d, %Y %H:%M UTC",
    "%B %d %Y, %H:%M:%S UTC",
    "%B %d %Y, %H:%M UTC",
    "%B %d, %Y %I:%M:%S %p UTC",
    "%B %d, %Y %I:%M %p UTC",
    "%B %d %Y, %I:%M:%S %p UTC",
    "%B %d %Y, %I:%M %p UTC",
    "%b %d, %Y %H:%M:%S UTC",
    "%b %d, %Y %H:%M UTC",
    "%b %d %Y, %H:%M:%S UTC",
    "%b %d %Y, %H:%M UTC",
    "%b %d, %Y %I:%M:%S %p UTC",
    "%b %d, %Y %I:%M %p UTC",
    "%b %d %Y, %I:%M:%S %p UTC",
    "%b %d %Y, %I:%M %p UTC",
    "%Y-%m-%d %H:%M:%S UTC",
    "%Y-%m-%d %H:%M UTC",
)
POOL_VALIDITY_DATE_FORMATS = (
    "%B %d, %Y",
    "%B %d %Y",
    "%b %d, %Y",
    "%b %d %Y",
)
POOL_INDEX_CACHE = {}

# ExamTools compatibility overrides for known, acknowledged validity-date bugs
# in generated Results PDFs.
#
# Key format: (exam class prefix used in filenames, parsed pool_valid_through date)
# Value: corrected cutoff date that matches our local question-pool file naming.
#
# Why this exists:
# ExamTools has produced some Technician Results PDFs that show:
#   "Technician exam valid Jul 1, 2022 — Jul 1, 2026"
# The correct end date for that pool is Jun 30, 2026.
# Many historical PDFs already contain the incorrect Jul 1, 2026 date, so we
# normalize it before resolving pool filenames.
POOL_VALIDITY_OVERRIDES = {
    ("technician", "2026-07-01"): "2026-06-30",
    ("extra", "2028-07-01"): "2028-06-30",
}

# Set HAM_RESULTS_DEBUG=1 to enable parser diagnostics.
DEBUG_PARSER = os.getenv("HAM_RESULTS_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")

def debug_log(message):
    if DEBUG_PARSER:
        print(f"[DEBUG] {message}")

def parse_exam_pdf(pdf_path):
    """
    Parses a multi-exam PDF and returns:
    - applicant_name (str or None)
    - results: list of exam results (one per element)
    Each exam result is a dict with:
      - exam_type (str)
      - score (dict)
      - report (list of dicts with missed question details)
    """
    import collections
    import re
    import pdfplumber

    applicant_name = None
    exams = collections.defaultdict(lambda: {
        "exam_designator": None,
        "designators": [],
        "missed": [],
        "score": None,
        "date": None,
        "pool_valid_from": None,
        "pool_valid_through": None,
        "pool_exam_type": None
    })

    line_regex = re.compile(r'\d+\.\s+([TGE]\d[A-Z]\d{2}):\s+([A-D])(?:\s+\(should be\s+([A-D])\))?')
    score_regex = re.compile(r'Test (Passed|Failed)\s*-\s*(\d+)\s+out of\s+(\d+)', re.IGNORECASE)
    name_regex = re.compile(r'^([A-Za-z\'\- ]+)\s+\(PIN:\s*\d{4}\)', re.IGNORECASE)
    date_regex = re.compile(r"Exam started at (.+?) by")
    validity_regex = re.compile(
        r"(?i)\b(Technician|General|Amateur Extra)\s+exam\s+valid\s+"
        r"([A-Za-z]{3,9}\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})\s*[—–-]\s*"
        r"([A-Za-z]{3,9}\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})"
    )

    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages):
            score_match = None
            date_time_match = None
            validity_match = None

            text = page.extract_text()
            if not text:
                continue

            for line in text.split('\n'):
                line = line.strip()

                # Extract applicant name (only from first page)
                if page_num == 0 and not applicant_name:
                    name_match = name_regex.match(line)
                    if name_match:
                        applicant_name = name_match.group(1).strip()

                # Extract score
                if not score_match:
                    score_match = score_regex.search(line)
                    if score_match:
                        status, correct_count, total = score_match.groups()
                        correct_count, total = int(correct_count), int(total)
                        exams[page_num]["score"] = {
                            "status": status.capitalize(),
                            "correct": correct_count,
                            "total": total,
                            "incorrect": total - correct_count
                        }

                # Extract date / time of exam
                if not date_time_match:
                    date_time_match = date_regex.search(line)
                    if date_time_match:
                        exams[page_num]["date"] = date_time_match.group(1)

                # Extract question pool validity window
                if not validity_match:
                    validity_match = validity_regex.search(line)
                    if validity_match:
                        pool_exam_type, valid_from_raw, valid_through_raw = validity_match.groups()
                        exams[page_num]["pool_exam_type"] = pool_exam_type
                        try:
                            exams[page_num]["pool_valid_from"] = parse_pool_validity_date(valid_from_raw)
                            exams[page_num]["pool_valid_through"] = parse_pool_validity_date(valid_through_raw)
                            debug_log(
                                f"Page {page_num + 1}: pool validity parsed: "
                                f"type='{pool_exam_type}', from='{valid_from_raw}', through='{valid_through_raw}'"
                            )
                        except ValueError as e:
                            # Do not fail the whole report if ExamTools changes validity date formatting.
                            # We intentionally fall back to exam-start-date selection later.
                            exams[page_num]["pool_valid_from"] = None
                            exams[page_num]["pool_valid_through"] = None
                            print(
                                f"[WARN] Page {page_num + 1}: could not parse pool validity dates "
                                f"from '{valid_from_raw}' - '{valid_through_raw}': {e}. "
                                f"Falling back to exam-date-based pool selection."
                            )

            # Process left and right columns
            width, height = page.width, page.height
            left_text = page.crop((0, 0, width / 2, height)).extract_text() or ''
            right_text = page.crop((width / 2, 0, width, height)).extract_text() or ''
            all_lines = (left_text + '\n' + right_text).split('\n')

            exam_designator = None
            for line in all_lines:
                line = line.strip()
                match = line_regex.search(line)
                if match:
                    designator, chosen, correct = match.groups()
                    if not exam_designator:
                        exam_designator = designator
                    correct = correct if correct else chosen
                    if chosen != correct:
                        exams[page_num]["missed"].append({
                            "designator": designator,
                            "chosen": chosen,
                            "correct": correct
                        })
                        exams[page_num]["designators"].append(designator)

            exams[page_num]["exam_designator"] = exam_designator
            debug_log(
                f"Page {page_num + 1}: "
                f"exam_designator='{exam_designator}', exam_started='{exams[page_num]['date']}', "
                f"pool_valid_through='{exams[page_num]['pool_valid_through']}'"
            )

    # Post-process and sort results
    results = []
    for page_num in sorted(exams):
        exam = exams[page_num]
        if not exam["exam_designator"]:
            continue

        # Sort missed questions by designator
        exam["missed"].sort(key=lambda x: x['designator'])

        exam_date = parse_exam_date(exam["date"]) if exam["date"] else None
        pool = load_question_pool(
            exam["exam_designator"],
            pool_valid_through=exam["pool_valid_through"],
            exam_date=exam_date
        )
        detailed_report = build_detailed_report(exam["missed"], pool)
        results.append({
            "exam_type": exam_type_from_designator(exam["exam_designator"]),
            "score": exam["score"],
            "date": exam["date"],
            "report": detailed_report
        })

    return applicant_name, results

def exam_type_from_designator(designator):
    """
    Given a question designator like 'T6B02', return the exam type.

    Args:
        designator (str): The question designator (e.g., 'T1A01', 'G4B02', 'E1A05')

    Returns:
        str: 'Technician', 'General', 'Amateur Extra', or 'Unknown' if the prefix is not recognized.
    """
    exam_type_map = {
        'T': 'Technician',
        'G': 'General',
        'E': 'Amateur Extra'
    }
    return exam_type_map.get(designator[0].upper(), 'Unknown')

def parse_exam_date(raw_exam_date):
    """
    Parse the exam date string from the PDF into a date object.

    Args:
        raw_exam_date (str | None): e.g. 'July 10, 2025 21:35 UTC'

    Returns:
        datetime.date
    """
    if not raw_exam_date:
        raise ValueError("Could not parse exam date from PDF.")

    exam_date_text = re.sub(r"\s+", " ", raw_exam_date.strip())
    # Normalize ordinal day values (e.g. "12th" -> "12") from ExamTools date text.
    exam_date_text = re.sub(r"(\d{1,2})(st|nd|rd|th)\b", r"\1", exam_date_text, flags=re.IGNORECASE)
    for fmt in DATE_PARSE_FORMATS:
        try:
            return datetime.strptime(exam_date_text, fmt).date()
        except ValueError:
            continue

    # Fallback: if we can recover just the calendar date, ignore time tokens.
    date_only_patterns = (
        (r"([A-Za-z]+ \d{1,2}, \d{4})", ("%B %d, %Y", "%b %d, %Y")),
        (r"(\d{4}-\d{2}-\d{2})", ("%Y-%m-%d",)),
    )
    for pattern, formats in date_only_patterns:
        match = re.search(pattern, exam_date_text)
        if not match:
            continue
        date_fragment = match.group(1)
        for fmt in formats:
            try:
                return datetime.strptime(date_fragment, fmt).date()
            except ValueError:
                continue

    raise ValueError(f"Unrecognized exam date format: '{raw_exam_date}'")

def parse_pool_validity_date(raw_date):
    """
    Parse a pool validity date text from the 'exam valid ...' line.
    """
    if not raw_date:
        raise ValueError("Could not parse pool validity date from PDF.")

    date_text = re.sub(r"\s+", " ", raw_date.strip())
    date_text = re.sub(r"(\d{1,2})(st|nd|rd|th)\b", r"\1", date_text, flags=re.IGNORECASE)

    for fmt in POOL_VALIDITY_DATE_FORMATS:
        try:
            return datetime.strptime(date_text, fmt).date()
        except ValueError:
            continue

    raise ValueError(f"Unrecognized pool validity date format: '{raw_date}'")

def load_question_pool(first_designator, pool_valid_through=None, exam_date=None):
    """
    Loads the correct question pool JSON based on exam pool validity date from PDF.
    Falls back to exam date only when validity date is unavailable.

    Args:
        first_designator (str): e.g., 'T1A01', 'G4B02', 'E1A05'
        pool_valid_through (datetime.date | None): Pool cutoff date parsed from PDF
        exam_date (datetime.date | None): Exam date from the report (fallback only)

    Returns:
        dict: Mapping of designator IDs to question data
    """
    class_prefix_map = {
        'T': 'technician',
        'G': 'general',
        'E': 'extra'
    }
    pool_key = first_designator[0].upper()
    class_prefix = class_prefix_map.get(pool_key)
    if not class_prefix:
        raise ValueError(f"Unknown exam designator '{first_designator}'")

    candidates = get_pool_candidates(class_prefix)
    if pool_valid_through is not None:
        # Normalize known upstream date bugs before exact file matching.
        # This keeps lookup deterministic and preserves filename-only pool selection.
        normalized_pool_valid_through = normalize_pool_valid_through(
            class_prefix, pool_valid_through
        )

        for cutoff, file_path in candidates:
            if cutoff == normalized_pool_valid_through:
                debug_log(
                    f"Pool selection for '{first_designator}': "
                    f"matched pool_valid_through={normalized_pool_valid_through.isoformat()} -> '{file_path.name}'"
                )
                return load_pool_file(file_path)
        raise FileNotFoundError(
            f"No {class_prefix} question pool file found with cutoff "
            f"{normalized_pool_valid_through.isoformat()} "
            f"for designator '{first_designator}'."
        )

    if exam_date is None:
        raise ValueError(
            f"Neither pool_valid_through nor exam_date is available for designator '{first_designator}'."
        )

    fallback_candidates = []
    for cutoff, file_path in candidates:
        if cutoff >= exam_date:
            fallback_candidates.append((cutoff, file_path))

    if not fallback_candidates:
        raise FileNotFoundError(
            f"No eligible {class_prefix} question pool found for exam date {exam_date.isoformat()} "
            f"and designator '{first_designator}'."
        )

    _, pool_file = min(fallback_candidates, key=lambda item: item[0])
    debug_log(
        f"Pool selection for '{first_designator}': "
        f"fallback by exam_date={exam_date.isoformat()} -> '{pool_file.name}'"
    )
    return load_pool_file(pool_file)

def load_pool_file(pool_file):
    """
    Load a pool file and return a dict keyed by question ID.
    """
    debug_log(f"Loading question pool file: {pool_file}")

    with open(pool_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # Build a dict { 'T1A01': {...}, 'T1A02': {...}, ... }
    return { q['id']: q for q in data }

def normalize_pool_valid_through(class_prefix, pool_valid_through):
    """
    Normalize known upstream validity-date issues from ExamTools PDFs.

    This function is intentionally explicit and table-driven:
    - easy to audit
    - easy to remove once upstream bugs no longer affect incoming PDFs
    - no fuzzy matching that could accidentally select the wrong pool
    """
    override_key = (class_prefix, pool_valid_through.isoformat())
    corrected = POOL_VALIDITY_OVERRIDES.get(override_key)
    if not corrected:
        return pool_valid_through

    corrected_date = datetime.strptime(corrected, "%Y-%m-%d").date()
    debug_log(
        f"Applying ExamTools validity override for '{class_prefix}': "
        f"{pool_valid_through.isoformat()} -> {corrected_date.isoformat()}"
    )
    return corrected_date

def get_pool_candidates(class_prefix):
    """
    Return cached list of (cutoff_date, file_path) for a given class prefix.
    """
    if class_prefix in POOL_INDEX_CACHE:
        return POOL_INDEX_CACHE[class_prefix]

    data_dir = Path(__file__).resolve().parent / "data"
    pattern = re.compile(rf"^{class_prefix}_(\d{{4}}-\d{{2}}-\d{{2}})\.json$")
    indexed = []

    for file_path in data_dir.glob(f"{class_prefix}_*.json"):
        match = pattern.match(file_path.name)
        if not match:
            continue
        cutoff = datetime.strptime(match.group(1), "%Y-%m-%d").date()
        indexed.append((cutoff, file_path))

    indexed.sort(key=lambda item: item[0])
    POOL_INDEX_CACHE[class_prefix] = indexed
    return indexed

def build_detailed_report(missed, question_pool):
    """
    Builds a detailed report for each missed question, combining:
    - Question text
    - Answer letter and full answer text (chosen and correct)

    Args:
        missed (list of dict): List from parse_exam_pdf()
        question_pool (dict): Mapping from load_question_pool()

    Returns:
        list of dict: Detailed report per missed question
    """
    detailed_report = []
    for m in missed:
        q = question_pool.get(m['designator'])
        if not q:
            # Question not found in pool — fallback text
            detailed_report.append({
                "designator": m['designator'],
                "question": "(Question text not found)",
                "your_answer_letter": m['chosen'],
                "your_answer_text": "(Unknown)",
                "correct_answer_letter": m['correct'],
                "correct_answer_text": "(Unknown)"
            })
            continue

        # Convert letter (A-D) to index (0-3) for answer text lookup
        your_index = 'ABCD'.index(m['chosen'])
        correct_index = 'ABCD'.index(m['correct'])

        detailed_report.append({
            "designator": m['designator'],
            "question": q['question'],
            "your_answer_letter": m['chosen'],
            "your_answer_text": q['answers'][your_index],
            "correct_answer_letter": m['correct'],
            "correct_answer_text": q['answers'][correct_index]
        })

    return detailed_report
