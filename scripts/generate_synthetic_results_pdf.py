#!/usr/bin/env python3
"""
Generate a minimal ExamTools-like Results PDF fixture.

The PDF is intentionally tiny and dependency-free so we can create parser
fixtures without relying on external PDF libraries. Its text lines are shaped
to match the regexes in processor.py.
"""

from pathlib import Path

PAGE_WIDTH = 612
PAGE_HEIGHT = 792
FONT_SIZE = 12
LEADING = 16
START_X = 36
START_Y = 756


def escape_pdf_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def build_content_stream(lines: list[str]) -> str:
    content = ["BT", f"/F1 {FONT_SIZE} Tf", f"1 0 0 1 {START_X} {START_Y} Tm"]
    first = True
    for line in lines:
        escaped = escape_pdf_text(line)
        if first:
            content.append(f"({escaped}) Tj")
            first = False
        else:
            content.append(f"0 -{LEADING} Td")
            content.append(f"({escaped}) Tj")
    content.append("ET")
    return "\n".join(content) + "\n"


def build_pdf(lines: list[str]) -> bytes:
    content_stream = build_content_stream(lines).encode("latin-1", errors="replace")
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
            "/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(content_stream)} >>\nstream\n{content_stream.decode('latin-1')}endstream",
    ]

    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{index} 0 obj\n{obj}\nendobj\n".encode("latin-1"))

    xref_start = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode("latin-1"))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("latin-1"))
    pdf.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_start}\n%%EOF\n"
        ).encode("latin-1")
    )
    return bytes(pdf)


def main() -> None:
    output_path = Path("fixtures/technician_new_pool_results.pdf")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "Taylor Example (PIN: 1234)",
        "Exam started at July 1, 2026 18:30 UTC by VE Team",
        "Technician exam valid Jul 1, 2026 - Jun 30, 2030",
        "Test Failed - 32 out of 35",
        "1. T1A04: B (should be A)",
        "2. T1A10: D (should be C)",
        "3. T1A11: A (should be B)",
    ]

    output_path.write_bytes(build_pdf(lines))
    print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
