# Rules snapshot

The pinned default is **August 7, 2026**. The complete original document and
extracted text are checked in together:

- `sources/2026-08-07.docx`: unchanged bytes downloaded from
  [Wizards of the Coast](https://media.wizards.com/2026/downloads/MagicCompRules%2020260807.docx).
- `comprehensive/2026-08-07.txt`: UTF-8 extraction containing the introduction,
  contents, all numbered rules, glossary, credits, and attribution text.
- `manifest.json`: effective/download dates, provenance, and SHA-256 hashes for
  both files.

On September 20, 2026, the [official rules page](https://magic.wizards.com/en/rules)
linked to a September 25 release that was not yet effective. The August 7 TXT
URL returned HTTP 404; the matching official DOCX and PDF were available. We use
the DOCX because its paragraph boundaries avoid ambiguity from PDF soft wrapping.
The future release is not silently substituted for this pin.

The original source is preserved, including its copyright and attribution text.
The TXT is an extraction, not the official TXT download. The extractor retains
paragraphs, tabs, explicit text line breaks, and all `w:t` text. It omits Word
formatting, pagination, and non-text bookmarks. It does not rewrite wording.

Reproduce the text into a **new** file and compare it with the checked-in copy:

```sh
uv run python scripts/extract_rules.py rules/sources/2026-08-07.docx /tmp/fcsim-rules-check.txt
cmp rules/comprehensive/2026-08-07.txt /tmp/fcsim-rules-check.txt
```

The lookup parses numbered rules and glossary entries from this text, ignoring
the repeated headings in the table of contents. It retains examples and
multi-paragraph rules. The current corpus has 3,161 numbered rules in 147
sections, plus 739 glossary entries. An in-memory search index is derived on
demand; no search database is authoritative or checked in.

## Updating

1. Follow the official rules page and inspect the document's **effective date**.
   Prefer an official TXT when available. For DOCX, preserve the downloaded
   original and run `scripts/extract_rules.py` into a new dated TXT.
2. Add a new manifest entry with a unique version, effective date, UTC download
   time, source URL/format/path/hash, text path/hash/encoding, and extraction
   details. For a direct TXT download, source and text can point to the same file.
   Keep old files and entries unchanged.
3. Inspect changed rules, verify the extraction, and run the lookup tests against
   the new version. A search result's rule number is meaningful only together
   with its version.
4. Deliberately change `default_version` when adopting the release. The loader
   rejects future-effective versions unless the host explicitly opts in. It
   never advances the default just because a new date or download is available.

Game directories should receive `fcsim rules export <game_dir>/rules`. Their
manifest and bytes remain independent of subsequent updates to this directory.
Oracle card text, card-specific rulings, and bracket guidance remain separate
references; this snapshot contains the Comprehensive Rules only.
