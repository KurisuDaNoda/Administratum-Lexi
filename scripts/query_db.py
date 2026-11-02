"""Search the active snapshot by meaning, exact name, or ID."""
from __future__ import annotations

import argparse
import json
import os
import sys

import psycopg
from psycopg import sql

try:
    from .import_data import ROOT, TABLES, load_env
    from .embed_data import combine_vectors, fetch_vectors, render_document
except ImportError:
    from import_data import ROOT, TABLES, load_env
    from embed_data import combine_vectors, fetch_vectors, render_document


def search(conn, tables, snapshot, *, name=None, entity_id=None, vector=None,
           config_id=None, faction=None, limit=5):
    results = []
    for table in tables:
        if table not in TABLES:
            raise ValueError("Unknown table")
        conditions = [sql.SQL("snapshot_id=%s")]
        params = [snapshot]
        score = sql.SQL("NULL::double precision")
        if name is not None:
            conditions.append(sql.SQL("lower(name)=lower(%s)"))
            params.append(name)
        if entity_id is not None:
            conditions.append(sql.SQL("entity_id=%s"))
            params.append(entity_id)
        if faction:
            conditions.append(sql.SQL("faction_id=%s"))
            params.append(faction)
        if vector is not None:
            score = sql.SQL("1 - (embedding <=> %s::vector)")
            params.insert(0, json.dumps(vector))
            conditions.extend([sql.SQL("embedding IS NOT NULL"), sql.SQL("embedding_config_id=%s")])
            params.append(config_id)
        query = sql.SQL("""SELECT entity_id,name,source_url,content,{} AS score
            FROM {} WHERE {} ORDER BY score DESC NULLS LAST, name, entity_id LIMIT %s""").format(
                score, sql.Identifier(table), sql.SQL(" AND ").join(conditions))
        params.append(limit)
        for ident, title, url, document, similarity in conn.execute(query, params):
            results.append({"table": table, "entity_id": ident, "name": title,
                "source_url": url, "score": similarity, "document": document})
    results.sort(key=lambda r: (-(r["score"] if r["score"] is not None else 0), r["name"], r["table"], r["entity_id"]))
    return results[:limit]


def embedding_configuration(conn, tables, snapshot, requested=None):
    configurations = set()
    for table in tables:
        rows = conn.execute(sql.SQL("""SELECT DISTINCT embedding_config_id, embedding_model,
            vector_dims(embedding) FROM {} WHERE snapshot_id=%s AND embedding IS NOT NULL""").format(sql.Identifier(table)), (snapshot,))
        configurations.update(tuple(row) for row in rows)
    if requested:
        configurations = {c for c in configurations if c[0] == requested}
    if not configurations:
        raise ValueError("No matching embeddings. Run import_data.py first, or use --name/--id without an API call.")
    if len(configurations) != 1:
        ids = ', '.join(str(c[0]) for c in sorted(configurations, key=str))
        raise ValueError(f"Multiple embedding configurations found. Select one with --embedding-config. Available: {ids}")
    config, model, dimensions = configurations.pop()
    if not config or not model or not dimensions:
        raise ValueError("Stored embedding metadata is incomplete; regenerate embeddings.")
    return config, model, dimensions


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", nargs="?", help="Natural-language semantic search")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--name", help="Exact, case-insensitive document name; no API call")
    mode.add_argument("--id", dest="entity_id", help="Exact source ID; no API call")
    parser.add_argument("--tables", nargs="+", choices=TABLES, default=list(TABLES))
    parser.add_argument("--faction", help="Exact exported faction ID, e.g. CSM")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--embedding-config", help="Select a configuration when multiple are stored")
    parser.add_argument("--full", action="store_true", help="Print complete rendered documents instead of previews")
    parser.add_argument("--json", action="store_true", help="Return complete documents as JSON")
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be positive")
    if bool(args.query and args.query.strip()) == bool(args.name is not None or args.entity_id is not None):
        parser.error("Provide either a query, --name, or --id")
    load_env(ROOT / '.env')
    url = os.getenv('DATABASE_URL')
    options = {} if url else {"host": os.getenv('POSTGRES_HOST', '127.0.0.1'),
        "port": os.getenv('POSTGRES_PORT', '5434'), "dbname": os.getenv('POSTGRES_DB', '40kbot'),
        "user": os.getenv('POSTGRES_USER', '40kbot'), "password": os.getenv('POSTGRES_PASSWORD')}
    with psycopg.connect(url or '', connect_timeout=10, autocommit=True, **options) as conn:
        conn.execute('SET default_transaction_read_only = on')
        active = conn.execute('SELECT snapshot_id FROM snapshots WHERE is_active').fetchone()
        if not active:
            raise ValueError('No active snapshot; import data first.')
        vector, config = None, None
        if args.query:
            config, model, dimensions = embedding_configuration(conn, args.tables, active[0], args.embedding_config)
            if not os.getenv('OPENAI_API_KEY'):
                raise ValueError('Set OPENAI_API_KEY in .env for semantic search, or use --name/--id.')
            from openai import OpenAI
            client = OpenAI(max_retries=3, timeout=30)
            vectors, _ = fetch_vectors(client, [args.query], dimensions, model)
            vector = combine_vectors(vectors, [1], dimensions)
        results = search(conn, args.tables, active[0], name=args.name, entity_id=args.entity_id,
            vector=vector, config_id=config, faction=args.faction, limit=args.limit)
    if args.json:
        print(json.dumps({"snapshot_id": active[0], "results": results}, ensure_ascii=False, indent=2))
    elif not results:
        print('No matching documents.')
    else:
        for position, result in enumerate(results, 1):
            similarity = f" | similarity {result['score']:.4f}" if result['score'] is not None else ''
            print(f"\n{position}. {result['name']} [{result['table']}:{result['entity_id']}]{similarity}")
            print(f"Source: {result['source_url'] or 'No direct source URL available'}")
            body = render_document(result['document'])[1]
            print(body if args.full else body[:1200] + ('\n[Use --full or --json for the complete document.]' if len(body)>1200 else ''))
    return results


if __name__ == '__main__':
    # Preserve Unicode rules text on Windows terminals.
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    try:
        main()
    except Exception as exc:
        if type(exc).__module__.startswith(('psycopg', 'openai')):
            print(f"Query failed ({type(exc).__name__}). Check database/API configuration and availability.", file=sys.stderr)
        else:
            print(f"Query failed: {exc}", file=sys.stderr)
        sys.exit(1)
