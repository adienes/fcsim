"""Reproduce the reference TXT from the preserved official DOCX, using stdlib.

No wording is rewritten. Paragraphs and explicit line breaks are retained; Word
formatting and pagination are omitted. The DOCX remains the authoritative source.
"""

import argparse
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def extract(source: Path) -> bytes:
    with ZipFile(source) as archive:
        document = ET.fromstring(archive.read("word/document.xml"))
    body = document.find(W + "body")
    if body is None:
        raise ValueError("No Word document body found.")
    paragraphs = []
    for paragraph in body:
        if paragraph.tag in {W + "sectPr", W + "bookmarkStart", W + "bookmarkEnd"}:
            continue
        if paragraph.tag != W + "p":
            raise ValueError("Unsupported document structure; review the extraction before importing.")
        parts = []
        for element in paragraph.iter():
            if element.tag == W + "t":
                parts.append(element.text or "")
            elif element.tag == W + "tab":
                parts.append("\t")
            elif element.tag in {W + "br", W + "cr"} and element.get(W + "type", "textWrapping") == "textWrapping":
                parts.append("\n")
        paragraphs.append("".join(parts))
    return ("\n".join(paragraphs) + "\n").encode("utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    raw = extract(args.source)
    # Never overwrite a previously pinned snapshot by accident.
    with args.destination.open("xb") as output:
        output.write(raw)
