from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

from fcsim.cli import main
from fcsim.rules import RulesError, RulesReference


ROOT = Path(__file__).resolve().parents[1]
AS_OF = date(2026, 9, 20)


@pytest.fixture(scope="module")
def reference():
    return RulesReference(ROOT / "rules", as_of=AS_OF)


def test_complete_corpus_and_exact_rule_context(reference):
    info = reference.info()
    assert (info["rule_count"], info["section_count"], info["glossary_count"]) == (3161, 147, 739)
    result = reference.get_rule("Rule 117.3c.")
    rule = result["rule"]
    assert rule["text"] == (
        "117.3c If a player has priority when they cast a spell, activate an ability, "
        "or take a special action, that player receives priority afterward."
    )
    assert rule["parent"]["id"] == "117.3"
    assert rule["section"] == {"id": "117", "title": "Timing and Priority"}
    assert reference.get_rule("117.3")["rule"]["children"] == ["117.3a", "117.3b", "117.3c", "117.3d"]
    assert result["snapshot"]["effective_date"] == "2026-08-07"


def test_all_passages_match_the_snapshot_lines(reference):
    lines = (ROOT / "rules" / reference.snapshot["text_path"]).read_text().splitlines()
    for entry in [*reference.rules.values(), *reference.glossary.values()]:
        assert entry.text == "\n".join(lines[entry.line_start - 1:entry.line_end])
    example = reference.get_rule("101.4")["rule"]["text"]
    assert "Example:" in example
    assert "sacrifices a creature" in example
    assert "101.4a" not in example
    assert "2. Parts of a Card" not in reference.get_rule("122.8")["rule"]["text"]


def test_extraction_is_reproducible_and_preserves_all_source_text(reference, tmp_path):
    source = ROOT / "rules" / reference.snapshot["source_path"]
    output = tmp_path / "extracted.txt"
    subprocess.run([sys.executable, str(ROOT / "scripts/extract_rules.py"), str(source), str(output)], check=True)
    assert output.read_bytes() == (ROOT / "rules" / reference.snapshot["text_path"]).read_bytes()
    with ZipFile(source) as archive:
        xml = ET.fromstring(archive.read("word/document.xml"))
    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    original = "".join(node.text or "" for node in xml.iter(w + "t"))
    assert re.sub(r"\s", "", original) == re.sub(r"\s", "", output.read_text())
    ability = reference.get_glossary("Ability")["entry"]["text"]
    assert "\n2. An activated" in ability  # Word soft breaks must not merge sentences.


def test_search_finds_relevant_numbered_rules_and_glossary(reference):
    draws = reference.search_rules("replacement effects drawing cards", limit=5)["results"]
    assert any(e["id"] == "121.6" for e in draws)
    commanders = reference.search_rules("commander graveyard", limit=5)["results"]
    assert commanders[0]["id"] == "903.9a"
    sickness = reference.search_rules("summoning sickness", limit=5)["results"]
    assert any(e["kind"] == "glossary" and e["id"] == "Summoning Sickness Rule" for e in sickness)
    assert len(draws) <= 5
    assert reference.search_rules("zzzxxyyqq")["results"] == []


def test_search_treats_fts_syntax_as_plain_text(reference):
    result = reference.search_rules('"commander" OR (graveyard*) --', limit=3)
    assert len(result["results"]) == 3


def test_sections_paginate_without_dropping_or_repeating_rules(reference):
    gathered = []
    offset = 0
    while True:
        result = reference.get_section("117.", offset=offset, limit=3)
        gathered.extend(e["id"] for e in result["rules"])
        if result["next_offset"] is None:
            break
        offset = result["next_offset"]
    assert gathered == [e.key for e in reference.rules.values() if e.section == "117"]
    assert len(gathered) == result["total"]


def test_lookup_dispatch_and_case_insensitive_glossary(reference):
    assert reference.lookup_rule("117.3c")["rule"]["id"] == "117.3c"
    assert reference.lookup_rule("117")["section"]["id"] == "117"
    assert reference.lookup_rule(" sUmMoNiNg SiCkNeSs RuLe ")["entry"]["kind"] == "glossary"
    assert reference.lookup_rule("replacement effects drawing cards")["results"]


@pytest.mark.parametrize("number", ["999.999", "117", "117.3z", "garbage"])
def test_unknown_rule_is_an_explicit_error(reference, number):
    with pytest.raises(RulesError):
        reference.get_rule(number)


@pytest.mark.parametrize("query,limit", [("", 10), ("the and", 10), ("x" * 1001, 10), ("draw", 0), ("draw", 101)])
def test_invalid_search_requests_are_bounded(reference, query, limit):
    with pytest.raises(RulesError):
        reference.search_rules(query, limit=limit)


def test_future_effective_version_requires_explicit_opt_in():
    with pytest.raises(RulesError, match="take effect"):
        RulesReference(ROOT / "rules", as_of=date(2026, 8, 6))
    future = RulesReference(ROOT / "rules", as_of=date(2026, 8, 6), allow_future=True)
    assert future.info()["snapshot"]["is_effective"] is False


def test_export_is_independent_and_refuses_overwrites(reference, tmp_path):
    destination = tmp_path / "game" / "rules"
    reference.export_snapshot(destination)
    copy = RulesReference(destination, version="2026-08-07", as_of=AS_OF)
    assert copy.get_rule("903.9a") == reference.get_rule("903.9a")
    assert copy.search_rules("draw replacement") == reference.search_rules("draw replacement")
    with pytest.raises(FileExistsError):
        reference.export_snapshot(destination)
    with pytest.raises(RulesError, match="exactly one snapshot"):
        RulesReference(destination, version="2099-01-01", as_of=AS_OF)


@pytest.mark.parametrize("kind", ["source", "text"])
def test_hash_tampering_is_rejected_at_load_and_export(reference, tmp_path, kind):
    destination = tmp_path / "rules"
    reference.export_snapshot(destination)
    loaded = RulesReference(destination, as_of=AS_OF)
    file = destination / loaded.snapshot[f"{kind}_path"]
    file.write_bytes(file.read_bytes() + b"tampered")
    with pytest.raises(RulesError, match="SHA-256 mismatch"):
        RulesReference(destination, as_of=AS_OF)
    with pytest.raises(RulesError, match="SHA-256 mismatch"):
        loaded.export_snapshot(tmp_path / "second-copy")
    assert not (tmp_path / "second-copy").exists()


def test_effective_date_must_match_text(reference, tmp_path):
    destination = tmp_path / "rules"
    reference.export_snapshot(destination)
    manifest = json.loads((destination / "manifest.json").read_text())
    manifest["snapshots"][0]["effective_date"] = "2026-08-06"
    (destination / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(RulesError, match="effective date does not match"):
        RulesReference(destination, as_of=AS_OF)


def test_missing_glossary_or_credits_is_rejected_even_with_matching_hash(reference, tmp_path):
    destination = tmp_path / "rules"
    reference.export_snapshot(destination)
    manifest = json.loads((destination / "manifest.json").read_text())
    snapshot = manifest["snapshots"][0]
    path = destination / snapshot["text_path"]
    text = path.read_text().rsplit("\nCredits\n", 1)
    raw = text[0].encode()
    path.write_bytes(raw)
    snapshot["text_sha256"] = hashlib.sha256(raw).hexdigest()
    (destination / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(RulesError, match="credits boundary"):
        RulesReference(destination, as_of=AS_OF)


def test_manifest_paths_cannot_escape_export_directory(reference, tmp_path):
    destination = tmp_path / "rules"
    reference.export_snapshot(destination)
    manifest = json.loads((destination / "manifest.json").read_text())
    manifest["snapshots"][0]["source_path"] = "../rules/" + reference.snapshot["source_path"]
    (destination / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(RulesError, match="relative"):
        RulesReference(destination, as_of=AS_OF)


def test_cli_json_output_and_error_status(capsys):
    assert main(["rules", "--as-of", "2026-09-20", "get", "117.3c"]) == 0
    assert json.loads(capsys.readouterr().out)["rule"]["id"] == "117.3c"
    assert main(["rules", "--as-of", "2026-09-20", "get", "999.999"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"]["code"] == "rules_error"
