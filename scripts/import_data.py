"""Import a Wahapedia snapshot as complete JSONB documents and generate embeddings."""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parents[1]
VERSION = "1"
TABLES = ("factions", "datasheets", "detachments", "shared_rules", "stratagems", "enhancements", "sources")
FILES = (
    "Abilities", "Datasheets", "Datasheets_abilities", "Datasheets_detachment_abilities",
    "Datasheets_enhancements", "Datasheets_keywords", "Datasheets_leader",
    "Datasheets_models", "Datasheets_models_cost", "Datasheets_options",
    "Datasheets_stratagems", "Datasheets_unit_composition", "Datasheets_wargear",
    "Detachments", "Detachments_chapter_dp", "Detachment_abilities", "Enhancements",
    "Enhancements_wargear", "Factions", "Last_update", "Source", "Stratagems",
)


def read_export(path: Path) -> list[dict]:
    # The export has unquoted multiline HTML and a trailing delimiter. Standard
    # csv.reader interprets HTML continuation lines as new records.
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    if not lines or not lines[0].endswith("|"):
        raise ValueError(f"{path.name}: missing header/trailing delimiter")
    fields = lines[0].split("|")[:-1]
    result, pending = [], []
    for number, line in enumerate(lines[1:], 2):
        if not pending and not line.strip():
            continue
        pending.append(line)
        record = "\n".join(pending)
        delimiters = record.count("|")
        if delimiters > len(fields):
            raise ValueError(f"{path.name}:{number}: too many delimiters")
        if delimiters == len(fields) and record.endswith("|"):
            values = record.split("|")[:-1]
            if len(values) != len(fields):
                raise ValueError(f"{path.name}:{number}: invalid field count")
            result.append(dict(zip(fields, values)))
            pending = []
    if pending:
        raise ValueError(f"{path.name}: incomplete final record")
    return result


class RuleHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.links = [], []

    def handle_starttag(self, tag, attrs):
        if tag in {"br", "p", "div", "ul", "ol", "li", "tr", "td", "th"}:
            self.parts.append("\n" if tag != "li" else "\n- ")
        for key, value in attrs:
            if tag == "a" and key == "href" and value:
                self.links.append(value)

    def handle_endtag(self, tag):
        if tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def clean_html(value):
    parser = RuleHTML()
    parser.feed(value)
    text = "\n".join(re.sub(r"[ \t]+", " ", x).strip() for x in "".join(parser.parts).splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip(), parser.links


def enrich(value):
    """Keep original HTML fields and add readable companions; never overwrite source."""
    if isinstance(value, list):
        return [enrich(x) for x in value]
    if isinstance(value, dict):
        result = {k: enrich(v) for k, v in value.items()}
        for key, item in value.items():
            if isinstance(item, str) and re.search(r"</?[a-zA-Z][^>]*>", item):
                result[key + "_text"] = clean_html(item)[0]
        return result
    return value


def collect_urls(value, base):
    urls = set()
    if isinstance(value, dict):
        for item in value.values():
            urls.update(collect_urls(item, base))
    elif isinstance(value, list):
        for item in value:
            urls.update(collect_urls(item, base))
    elif isinstance(value, str):
        if value.startswith(("https://", "http://")) and not re.search(r"\s", value):
            urls.add(value)
        if "<" in value:
            for link in clean_html(value)[1]:
                resolved = urljoin(base or "https://wahapedia.ru/", link)
                if resolved.startswith(("https://", "http://")):
                    urls.add(resolved)
    return urls


def index(rows, key="id"):
    result = {}
    for row in rows:
        ident = row[key]
        if ident in result:
            raise ValueError(f"Duplicate key {ident}")
        result[ident] = row
    return result


def group(rows, key):
    result = defaultdict(list)
    for row in rows:
        result[row[key]].append(row)
    return result


def build(data_dir):
    data, checksums = {}, {}
    for name in FILES:
        path = data_dir / (name + ".csv")
        checksums[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        data[name] = read_export(path)
    snapshot = hashlib.sha256(json.dumps({"schema": VERSION, "files": checksums}, sort_keys=True).encode()).hexdigest()
    warnings = []
    faction_rows = index(data["Factions"])
    source_rows = index(data["Source"])
    unit_rows = index(data["Datasheets"])
    det_rows = index(data["Detachments"])

    # Validate references even for relations that will be embedded in documents.
    targets = {"datasheet_id": unit_rows, "leader_id": unit_rows, "attached_id": unit_rows,
               "faction_id": faction_rows, "source_id": source_rows,
               "detachment_id": det_rows, "stratagem_id": index(data["Stratagems"]),
               "enhancement_id": index(data["Enhancements"]),
               "ability_id": {r["id"] for r in data["Abilities"]},
               "detachment_ability_id": index(data["Detachment_abilities"])}
    for name, rows in data.items():
        for row in rows:
            for field, valid in targets.items():
                if row.get(field) and row[field] not in valid:
                    issue = {"file": name + ".csv", "field": field, "value": row[field], "record": row}
                    if field in {"detachment_id", "datasheet_id", "leader_id", "attached_id"}:
                        warnings.append(issue)  # Preserve orphan rows in the report for review.
                    else:
                        raise ValueError(f"Unresolved reference: {issue}")

    def document(kind, ident, row, source_url=None, **extra):
        content = enrich({**row, **extra})
        return {"schema_version": VERSION, "document_id": f"{kind}:{ident}",
                "entity_id": ident, "entity_type": kind, "snapshot_id": snapshot,
                "name": row.get("name", ""), "source_url": source_url,
                "source_urls": sorted(collect_urls(content, source_url) | ({source_url} if source_url else set())),
                "content": content}

    docs = {name: {} for name in TABLES}
    abilities = group(data["Abilities"], "id")
    for ident, rows in abilities.items():
        variants = {json.dumps({k: v for k, v in r.items() if k != "faction_id"}, sort_keys=True) for r in rows}
        if len(variants) != 1:
            raise ValueError(f"Ability {ident} has conflicting definitions across factions")
        canonical = {k: v for k, v in rows[0].items() if k != "faction_id"}
        docs["shared_rules"][ident] = document("shared_rule", ident, canonical,
            associated_faction_ids=sorted({r["faction_id"] for r in rows if r["faction_id"]}),
            source_records=rows)

    for ident, row in source_rows.items():
        docs["sources"][ident] = document("source", ident, row, row.get("errata_link") or None)
    enhancement_weapons = group(data["Enhancements_wargear"], "enhancement_id")
    for table, filename in [("stratagems", "Stratagems"), ("enhancements", "Enhancements")]:
        for row in data[filename]:
            ident = row["id"]
            det = row.get("detachment_id")
            faction_url = faction_rows.get(row.get("faction_id"), {}).get("link") or None
            # This is page-level provenance, not an invented section anchor.
            extra = {"source_url_kind": "faction_page" if faction_url else "unavailable",
                     "scope_status": "detachment" if det in det_rows else "unresolved" if det else "not_specified"}
            if table == "enhancements":
                extra["weapon_profiles"] = enhancement_weapons.get(ident, [])
            docs[table][ident] = document(table[:-1], ident, row, faction_url, **extra)

    det_abilities = group(data["Detachment_abilities"], "detachment_id")
    strat_by_det = defaultdict(list)
    enh_by_det = defaultdict(list)
    for doc in docs["stratagems"].values():
        strat_by_det[doc["content"].get("detachment_id")].append(doc)
    for doc in docs["enhancements"].values():
        enh_by_det[doc["content"].get("detachment_id")].append(doc)
    chapter = group(data["Detachments_chapter_dp"], "detachment_id")
    for ident, row in det_rows.items():
        url = faction_rows.get(row["faction_id"], {}).get("link") or None
        rules = [dict(r, source_url=url, source_url_kind="faction_page" if url else "unavailable") for r in det_abilities.get(ident, [])]
        docs["detachments"][ident] = document("detachment", ident, row, url,
            rules=rules, enhancements=enh_by_det[ident], stratagems=strat_by_det[ident],
            chapter_detachment_points=chapter.get(ident, []), source_url_kind="faction_page" if url else "unavailable")

    children = {name: group(data[name], "datasheet_id") for name in FILES if name.startswith("Datasheets_") and name != "Datasheets_leader"}
    valid_leaders = [r for r in data["Datasheets_leader"] if r["leader_id"] in unit_rows and r["attached_id"] in unit_rows]
    leader_out = group(valid_leaders, "leader_id")
    leader_in = group(valid_leaders, "attached_id")
    def unit_reference(ident):
        row = unit_rows[ident]
        return {"datasheet_id": ident, "name": row["name"], "source_url": row["link"]}
    child_fields = {"models": "model_profiles", "models_cost": "points",
                    "unit_composition": "unit_composition", "wargear": "weapon_profiles",
                    "options": "equipment_options", "keywords": "keywords"}
    for ident, row in unit_rows.items():
        extra = {out: children["Datasheets_" + suffix].get(ident, []) for suffix, out in child_fields.items()}
        expanded = []
        for ability in children["Datasheets_abilities"].get(ident, []):
            item = dict(ability)
            if ability["ability_id"]:
                item["definition"] = docs["shared_rules"][ability["ability_id"]]
            item["context_url"] = row["link"]
            expanded.append(item)
        extra.update(abilities=expanded,
            can_lead=[unit_reference(r["attached_id"]) for r in leader_out.get(ident, [])],
            can_be_led_by=[unit_reference(r["leader_id"]) for r in leader_in.get(ident, [])],
            source_publication=source_rows.get(row["source_id"]),
            exported_associations={
                "stratagem_ids": [r["stratagem_id"] for r in children["Datasheets_stratagems"].get(ident, [])],
                "enhancement_ids": [r["enhancement_id"] for r in children["Datasheets_enhancements"].get(ident, [])],
                "detachment_ability_ids": [r["detachment_ability_id"] for r in children["Datasheets_detachment_abilities"].get(ident, [])]})
        docs["datasheets"][ident] = document("datasheet", ident, row, row["link"], **extra)
    for ident, row in faction_rows.items():
        docs["factions"][ident] = document("faction", ident, row, row["link"],
            associated_army_rules=[d for d in docs["shared_rules"].values() if ident in d["content"]["associated_faction_ids"]],
            association_note="Export associations do not establish rules eligibility or ownership.",
            detachment_ids=[r["id"] for r in data["Detachments"] if r["faction_id"] == ident])
    report = {"snapshot_id": snapshot, "schema_version": VERSION,
              "export_updated_at_raw": data["Last_update"][0]["last_update"],
              "file_checksums": checksums, "input_counts": {k: len(v) for k, v in data.items()},
              "document_counts": {k: len(v) for k, v in docs.items()}, "warnings": warnings,
              "vectorized": False}
    return docs, report


def load_env(path):
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def save_database(docs, report):
    import psycopg
    from psycopg.types.json import Jsonb
    load_env(ROOT / ".env")
    url = os.getenv("DATABASE_URL")
    options = {} if url else {"host": os.getenv("POSTGRES_HOST", "127.0.0.1"),
        "port": os.getenv("POSTGRES_PORT", "5434"), "dbname": os.getenv("POSTGRES_DB", "40kbot"),
        "user": os.getenv("POSTGRES_USER", "40kbot"), "password": os.getenv("POSTGRES_PASSWORD")}
    with psycopg.connect(url or "", connect_timeout=10, **options) as conn:
        # One transaction: failures leave the previous active snapshot intact.
        conn.execute("SELECT pg_advisory_xact_lock(400002)")
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute("""CREATE TABLE IF NOT EXISTS snapshots (
            snapshot_id text PRIMARY KEY, imported_at timestamptz NOT NULL DEFAULT now(),
            is_active boolean NOT NULL DEFAULT false, manifest jsonb NOT NULL)""")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_active_snapshot ON snapshots (is_active) WHERE is_active")
        conn.execute("INSERT INTO snapshots(snapshot_id, manifest) VALUES (%s,%s) ON CONFLICT(snapshot_id) DO UPDATE SET manifest=EXCLUDED.manifest",
                     (report["snapshot_id"], Jsonb(report)))
        for table in TABLES:  # Names are fixed constants, never user input.
            conn.execute(f"""CREATE TABLE IF NOT EXISTS {table} (
                snapshot_id text NOT NULL REFERENCES snapshots(snapshot_id),
                entity_id text NOT NULL, name text NOT NULL, faction_id text,
                source_url text, content jsonb NOT NULL, content_hash text NOT NULL,
                PRIMARY KEY(snapshot_id, entity_id))""")
            conn.execute(f"""ALTER TABLE {table}
                ADD COLUMN IF NOT EXISTS embedding vector,
                ADD COLUMN IF NOT EXISTS embedding_model text,
                ADD COLUMN IF NOT EXISTS embedding_config_id text,
                ADD COLUMN IF NOT EXISTS embedded_at timestamptz""")
            conn.execute(f"CREATE INDEX IF NOT EXISTS {table}_faction_idx ON {table}(snapshot_id, faction_id)")
            values = []
            for doc in docs[table].values():
                digest = hashlib.sha256(json.dumps(doc, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                values.append((report["snapshot_id"], doc["entity_id"], doc["name"], doc["content"].get("faction_id"), doc["source_url"], Jsonb(doc), digest))
            with conn.cursor() as cur:
                cur.executemany(f"""INSERT INTO {table}(snapshot_id,entity_id,name,faction_id,source_url,content,content_hash)
                    VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(snapshot_id,entity_id) DO UPDATE SET
                    name=EXCLUDED.name, faction_id=EXCLUDED.faction_id, source_url=EXCLUDED.source_url,
                    content=EXCLUDED.content, content_hash=EXCLUDED.content_hash,
                    embedding=CASE WHEN {table}.content_hash=EXCLUDED.content_hash THEN {table}.embedding ELSE NULL END,
                    embedding_model=CASE WHEN {table}.content_hash=EXCLUDED.content_hash THEN {table}.embedding_model ELSE NULL END,
                    embedding_config_id=CASE WHEN {table}.content_hash=EXCLUDED.content_hash THEN {table}.embedding_config_id ELSE NULL END,
                    embedded_at=CASE WHEN {table}.content_hash=EXCLUDED.content_hash THEN {table}.embedded_at ELSE NULL END""", values)
            actual = conn.execute(f"SELECT count(*) FROM {table} WHERE snapshot_id=%s", (report["snapshot_id"],)).fetchone()[0]
            if actual != len(values):
                raise ValueError(f"{table}: database count mismatch")
        conn.execute("UPDATE snapshots SET is_active=false WHERE is_active")
        conn.execute("UPDATE snapshots SET is_active=true WHERE snapshot_id=%s", (report["snapshot_id"],))


def run_embeddings(snapshot_id, model, dimensions):
    try:
        from .embed_data import main as embed_main
    except ImportError:
        from embed_data import main as embed_main
    return embed_main(["--snapshot-id", snapshot_id, "--model", model, "--dimensions", str(dimensions)])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "Data")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "generated")
    parser.add_argument("--dry-run", action="store_true", help="Validate and export JSON without connecting to PostgreSQL")
    parser.add_argument("--skip-embeddings", action="store_true", help="Import documents without calling the embedding API")
    parser.add_argument("--embedding-model", choices=["text-embedding-3-small", "text-embedding-3-large"], default="text-embedding-3-small")
    parser.add_argument("--embedding-dimensions", type=int, default=1536)
    args = parser.parse_args(argv)
    maximum = 1536 if args.embedding_model.endswith("small") else 3072
    if not 1 <= args.embedding_dimensions <= maximum:
        parser.error("Invalid embedding dimensions for the selected model")
    docs, report = build(args.data_dir)
    output = args.output_dir / report["snapshot_id"]
    output.mkdir(parents=True, exist_ok=True)
    for table, entries in docs.items():
        folder = output / table
        folder.mkdir(exist_ok=True)
        for ident, doc in entries.items():
            (folder / f"{ident}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not args.dry_run:
        save_database(docs, report)
    report["database_imported"] = not args.dry_run
    report["embedding_status"] = "skipped" if args.skip_embeddings or args.dry_run else "pending"
    report_path = output / "validation-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not args.dry_run and not args.skip_embeddings:
        print("Database import committed. Generating embeddings (rerunnable to resume).", flush=True)
        try:
            report["embedding_result"] = run_embeddings(report["snapshot_id"], args.embedding_model, args.embedding_dimensions)
            report["embedding_status"] = report["embedding_result"]["status"]
            report["vectorized"] = report["embedding_status"] == "complete"
        except (Exception, KeyboardInterrupt):
            report["embedding_status"] = "incomplete"
            print("Documents are imported. Embeddings are incomplete; rerun the import to resume.", file=sys.stderr)
            raise
        finally:
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"snapshot_id": report["snapshot_id"], "counts": report["document_counts"],
        "warnings": len(report["warnings"]), "database_imported": not args.dry_run,
        "output_dir": str(output), "vectorized": report["vectorized"],
        "embedding_status": report["embedding_status"]}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Avoid logging connection strings or credentials in driver exceptions.
        if type(exc).__module__.startswith(("psycopg", "openai")):
            print(f"Import failed ({type(exc).__name__}). Check database availability and .env credentials.", file=sys.stderr)
        else:
            print(f"Import failed: {exc}", file=sys.stderr)
        sys.exit(1)
