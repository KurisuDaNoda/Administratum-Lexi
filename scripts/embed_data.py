"""Populate document vectors without changing source documents or citations."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sys

import psycopg
import tiktoken

try:
    from .import_data import ROOT, TABLES, clean_html, load_env
except ImportError:
    from import_data import ROOT, TABLES, clean_html, load_env

RENDER_VERSION = "rules-text-v1"
# IDs, provenance, flavour text, and exported associations are not rule content.
SKIP = {"id", "entity_id", "document_id", "schema_version", "snapshot_id", "content_hash",
        "source_url", "source_urls", "source_url_kind", "source_records", "source_publication",
        "associated_faction_ids", "exported_associations", "association_note", "scope_status",
        "detachment_ids", "link", "legend", "context_url", "errata_link", "faction_id",
        "datasheet_id", "ability_id", "enhancement_id", "stratagem_id", "detachment_id",
        "detachment_ability_id", "source_id", "line", "line_in_wargear", "virtual", "is_support"}


def render_fields(value, path=""):
    lines = []
    if isinstance(value, dict):
        # Nested document envelopes have the same canonical body as top-level docs.
        if "document_id" in value and "content" in value:
            return render_fields(value["content"], path)
        for key, item in value.items():
            if key in SKIP or key.endswith("_text"):
                continue
            label = f"{path} / {key}" if path else key
            lines.extend(render_fields(value.get(key + "_text", item), label))
    elif isinstance(value, list):
        for i, item in enumerate(value, 1):
            lines.extend(render_fields(item, f"{path} {i}"))
    elif value is not None and str(value).strip():
        text = clean_html(str(value))[0] if "<" in str(value) else str(value)
        if text.strip():
            lines.append(f"{path}: {text}")
    return lines


def render_document(doc):
    header = f"Entity: {doc['entity_type']}\nName: {doc['name']}"
    return header, "\n".join(render_fields(doc["content"]))


def make_segments(doc, encoding, max_tokens=7800):
    header, body = render_document(doc)
    prefix = header + "\n\n"
    budget = max_tokens - len(encoding.encode(prefix))
    if budget < 1:
        raise ValueError("Document header exceeds token budget")
    tokens = encoding.encode(body)
    if not tokens:
        raise ValueError(f"Empty document: {doc['document_id']}")
    segments = []
    # Split at Unicode character boundaries to avoid dropping/corrupting text.
    start = 0
    while start < len(tokens):
        end = min(start + budget, len(tokens))
        while end > start:
            try:
                text = encoding.decode_bytes(tokens[start:end]).decode("utf-8")
                break
            except UnicodeDecodeError:
                end -= 1
        if end == start:
            raise ValueError("Token budget is too small for a Unicode character")
        segments.append((prefix + text, end - start))
        start = end
    return segments


def combine_vectors(vectors, weights, dimensions):
    if not vectors or len(vectors) != len(weights):
        raise ValueError("Embedding response count mismatch")
    if any(len(v) != dimensions or not all(math.isfinite(x) for x in v) for v in vectors):
        raise ValueError("Invalid embedding dimensions or non-finite values")
    combined = [sum(v[i] * w for v, w in zip(vectors, weights)) for i in range(dimensions)]
    norm = math.sqrt(sum(x*x for x in combined))
    if not norm:
        raise ValueError("Zero-length embedding")
    return [x / norm for x in combined]


def fetch_vectors(client, texts, dimensions, model):
    response = client.embeddings.create(model=model, input=texts, dimensions=dimensions, encoding_format="float")
    items = sorted(response.data, key=lambda x: x.index)
    if [x.index for x in items] != list(range(len(texts))):
        raise ValueError("Embedding response indexes mismatch")
    return [x.embedding for x in items], response.usage.total_tokens


def main(argv=None):
    load_env(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=["text-embedding-3-small", "text-embedding-3-large"], default="text-embedding-3-small")
    parser.add_argument("--dimensions", type=int, default=1536)
    parser.add_argument("--tables", nargs="+", choices=TABLES, default=list(TABLES))
    parser.add_argument("--limit", type=int, help="Maximum documents across all selected tables")
    parser.add_argument("--dry-run", action="store_true", help="Count pending documents/tokens without calling the API")
    parser.add_argument("--snapshot-id", help="Embed this imported snapshot instead of the active snapshot")
    args = parser.parse_args(argv)
    maximum = 1536 if args.model.endswith("small") else 3072
    if not 1 <= args.dimensions <= maximum or (args.limit is not None and args.limit < 1):
        parser.error("Invalid dimensions or limit")
    config = {"renderer": RENDER_VERSION, "model": args.model, "dimensions": args.dimensions,
              "max_tokens": 7800, "aggregation": "token-weighted-normalized-mean-v1"}
    config_id = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    url = os.getenv("DATABASE_URL")
    options = {} if url else {"host": os.getenv("POSTGRES_HOST", "127.0.0.1"),
        "port": os.getenv("POSTGRES_PORT", "5434"), "dbname": os.getenv("POSTGRES_DB", "40kbot"),
        "user": os.getenv("POSTGRES_USER", "40kbot"), "password": os.getenv("POSTGRES_PASSWORD")}
    encoding = tiktoken.get_encoding("cl100k_base")
    with psycopg.connect(url or "", connect_timeout=10, autocommit=True, **options) as conn:
        active = (conn.execute("SELECT snapshot_id FROM snapshots WHERE snapshot_id=%s", (args.snapshot_id,)).fetchone()
                  if args.snapshot_id else conn.execute("SELECT snapshot_id FROM snapshots WHERE is_active").fetchone())
        if not active:
            raise ValueError("No active snapshot; import data first")
        snapshot = active[0]
        jobs = []
        for table in args.tables:
            rows = conn.execute(f"""SELECT entity_id,content,content_hash FROM {table}
                WHERE snapshot_id=%s AND (embedding IS NULL OR embedding_config_id IS DISTINCT FROM %s)
                ORDER BY entity_id""", (snapshot, config_id)).fetchall()
            for ident, doc, digest in rows:
                jobs.append((table, ident, digest, make_segments(doc, encoding)))
        if args.limit:
            jobs = jobs[:args.limit]
        estimate = sum(len(encoding.encode(text)) for _, _, _, parts in jobs for text, _ in parts)
        print(json.dumps({"snapshot_id": snapshot, "pending_documents": len(jobs),
            "segments": sum(len(j[3]) for j in jobs), "estimated_input_tokens": estimate,
            "configuration": config, "dry_run": args.dry_run}), flush=True)
        if args.dry_run or not jobs:
            return {"status": "dry_run" if args.dry_run else "complete", "saved_documents": 0,
                    "stale_documents_skipped": 0, "embedding_config_id": config_id}
        if not os.getenv("OPENAI_API_KEY"):
            raise ValueError("Set OPENAI_API_KEY in .env or your environment, then rerun. No API calls were made.")
        from openai import OpenAI
        client = OpenAI(max_retries=5, timeout=60)
        completed, stale, total_tokens = 0, 0, 0
        for table, ident, digest, parts in jobs:
            vectors = []
            # At most eight segments per call, keeping batch token usage bounded.
            for offset in range(0, len(parts), 8):
                results, used = fetch_vectors(client, [x[0] for x in parts[offset:offset+8]], args.dimensions, args.model)
                vectors.extend(results)
                total_tokens += used
            vector = combine_vectors(vectors, [x[1] for x in parts], args.dimensions)
            updated = conn.execute(f"""UPDATE {table} SET embedding=%s::vector,
                embedding_model=%s, embedding_config_id=%s, embedded_at=now()
                WHERE snapshot_id=%s AND entity_id=%s AND content_hash=%s""",
                (json.dumps(vector), args.model, config_id, snapshot, ident, digest)).rowcount
            completed += updated
            stale += 1 - updated
            if (completed + stale) % 25 == 0:
                print(f"Processed {completed+stale}/{len(jobs)} documents; saved {completed}; stale {stale}", flush=True)
        result = {"status": "complete" if not stale else "incomplete", "saved_documents": completed,
                  "stale_documents_skipped": stale, "api_input_tokens": total_tokens, "embedding_config_id": config_id}
        print(json.dumps(result))
        return result


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Stopped. Completed document embeddings are saved; rerun to resume.", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        if type(exc).__module__.startswith(("psycopg", "openai")):
            print(f"Embedding failed ({type(exc).__name__}). Check credentials, connectivity, quota, and rate limits. Rerun to resume.", file=sys.stderr)
        else:
            print(f"Embedding failed: {exc}", file=sys.stderr)
        sys.exit(1)
