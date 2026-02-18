import re
import pdfplumber
import json
from datetime import datetime
from pathlib import Path

DATE_PARSE_FORMATS = (
    "%B %d, %Y %H:%M:%S UTC",
    "%B %d, %Y %H:%M UTC",
    "%b %d, %Y %H:%M:%S UTC",
    "%b %d, %Y %H:%M UTC",
    "%Y-%m-%d %H:%M:%S UTC",
    "%Y-%m-%d %H:%M UTC",
)
POOL_INDEX_CACHE = {}

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
        "date": None
    })

    line_regex = re.compile(r'\d+\.\s+([TGE]\d[A-Z]\d{2}):\s+([A-D])(?:\s+\(should be\s+([A-D])\))?')
    score_regex = re.compile(r'Test (Passed|Failed)\s*-\s*(\d+)\s+out of\s+(\d+)', re.IGNORECASE)
    name_regex = re.compile(r'^([A-Za-z\'\- ]+)\s+\(PIN:\s*\d{4}\)', re.IGNORECASE)
    date_regex = re.compile(r"Exam started at (.+? UTC) by")

    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages):
            score_match = None
            date_time_match = None

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

    # Post-process and sort results
    results = []
    for page_num in sorted(exams):
        exam = exams[page_num]
        if not exam["exam_designator"]:
            continue

        # Sort missed questions by designator
        exam["missed"].sort(key=lambda x: x['designator'])

        exam_date = parse_exam_date(exam["date"])
        pool = load_question_pool(exam["exam_designator"], exam_date)
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

    exam_date_text = raw_exam_date.strip()
    for fmt in DATE_PARSE_FORMATS:
        try:
            return datetime.strptime(exam_date_text, fmt).date()
        except ValueError:
            continue

    raise ValueError(f"Unrecognized exam date format: '{raw_exam_date}'")

def load_question_pool(first_designator, exam_date):
    """
    Loads the correct question pool JSON based on designator and exam date.

    Args:
        first_designator (str): e.g., 'T1A01', 'G4B02', 'E1A05'
        exam_date (datetime.date): Exam date from the report

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

    candidates = []
    for cutoff, file_path in get_pool_candidates(class_prefix):
        if cutoff >= exam_date:
            candidates.append((cutoff, file_path))

    if not candidates:
        raise FileNotFoundError(
            f"No eligible {class_prefix} question pool found for exam date {exam_date.isoformat()} "
            f"and designator '{first_designator}'."
        )

    _, pool_file = min(candidates, key=lambda item: item[0])

    with open(pool_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # Build a dict { 'T1A01': {...}, 'T1A02': {...}, ... }
    return { q['id']: q for q in data }

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
