# Administratum Lexi

## Local database

PostgreSQL 17 with pgvector, a persistent Docker volume, and a readiness check.
The database port is bound to localhost. The vector extension is enabled for
future use; startup does not import data or generate embeddings. Application
tables will be created alongside the import script in the next step.

From this directory in PowerShell:

```powershell
Copy-Item .env.example .env
# Set POSTGRES_PASSWORD in .env before starting.
docker compose up -d --wait
```

Connect from the host with the database name, user, password, and port in `.env`:

```text
Host: 127.0.0.1
Default port: 5434
Default database: 40kbot
Default user: 40kbot
```

Open an SQL shell without entering credentials on the command line:

```powershell
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

Useful commands:

```powershell
docker compose ps
docker compose logs db
docker compose down
```

`docker compose down` preserves the database volume. Adding `--volumes` deletes
the stored database. Initialization SQL runs only when the volume is first
created; changing `.env` credentials does not change an existing database user.

## Import the exports and generate embeddings

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python scripts/import_data.py --dry-run
# Add OPENAI_API_KEY to .env before the full import:
.venv\Scripts\python scripts/import_data.py
# To import documents only:
.venv\Scripts\python scripts/import_data.py --skip-embeddings
```

The importer reads `Data/` and uses the connection settings in `.env` (or
`DATABASE_URL`, when set). Existing environment variables take precedence.
Optional arguments: `--data-dir PATH`, `--output-dir PATH`,
`--embedding-model MODEL`, and `--embedding-dimensions N`.
Embedding generation runs by default after the database transaction commits,
and targets that exact imported snapshot. `--dry-run` performs neither database
writes nor embedding API calls. `--skip-embeddings` disables embedding generation.
If the API key is missing or the API fails, documents remain imported and the
command reports failure; rerunning resumes missing embeddings. The validation
report records whether embedding generation completed, was skipped, or failed.

It creates application tables and stores complete JSONB documents, preserving
IDs as strings, original HTML, readable text companions, and source URLs.
Weapons stay nested inside their datasheet. Detachment documents include all
their exported rules, enhancements, and stratagems. Those enhancements and
stratagems are also independently addressable in their own tables.
Shared ability definitions are expanded into datasheets; faction associations
are preserved without treating them as proof of eligibility. Edition fields
are retained as supplied, without assuming inconsistent sources agree.

`generated/<snapshot-id>/` contains readable JSON documents and a validation
report. Orphan datasheet relations and unresolved detachment references are
retained in the report with their original rows. Missing publication metadata
is represented as null. Faction-page URLs are labelled as such; the importer
does not invent section anchors or pretend they are exact rule links.

Imports use one transaction and activate the snapshot only after database count
checks pass. The snapshot ID includes source-file checksums and schema version;
rerunning the same import updates the same rows without creating duplicates.
Older snapshots remain available. Each document table has nullable `embedding`
(pgvector), `embedding_model`, `embedding_config_id`, and `embedded_at` columns.
Vector dimensions are intentionally unspecified until an embedding model is
chosen; no vector search index is created yet. Unchanged documents retain
existing embeddings on reimport; changed documents have their embeddings and
embedding metadata cleared to prevent stale search results. The original exports
in `Data/` remain unchanged. No rule-level chunks are generated yet.

Example SQL (the `content` column contains the full document envelope):

```sql
SELECT d.name, d.source_url, d.content
FROM datasheets d JOIN snapshots s USING (snapshot_id)
WHERE s.is_active AND d.name = 'Warboss';

SELECT d.name,
       d.content #> '{content,rules}' AS rules,
       d.content #> '{content,enhancements}' AS enhancements,
       d.content #> '{content,stratagems}' AS stratagems
FROM detachments d JOIN snapshots s USING (snapshot_id)
WHERE s.is_active;
```

## Generate embeddings

Add `OPENAI_API_KEY=...` to the local `.env` file, then run:

```powershell
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python scripts/embed_data.py --dry-run
.venv\Scripts\python scripts/embed_data.py
```

This sends rendered document text to the OpenAI embeddings API and incurs API
usage charges. Default: `text-embedding-3-small`, 1,536 dimensions. Source URLs
and source documents remain unchanged. Text includes profiles, equipment,
conditions, restrictions, and expanded rules; provenance, flavour text, duplicate
HTML, and exported eligibility associations are excluded from embedding input.

Long documents are split below the model input limit without truncation. Segment
vectors are combined using a token-weighted mean and normalized, producing one
document vector in the existing `embedding` column. Segment vectors are not
stored separately. These document vectors support initial retrieval; focused
rule-level chunk retrieval remains a future addition. Full detachments remain
available as their complete parent documents.

Each completed document is saved immediately. Rerunning skips vectors matching
the current model, dimensions, and rendering configuration. Changes to that
configuration replace incompatible document vectors. Concurrent content changes
are detected using the content hash; stale results are skipped. The script
operates on the active snapshot selected at startup.

Use `--limit 10` for a small run, `--tables datasheets detachments` to select
tables, or `--model text-embedding-3-large --dimensions 1536` to change the model.
Queries must use the same model and dimensions as their target documents.
No approximate vector index is created; exact cosine search can be used first.

Run tests:

```powershell
.venv\Scripts\python -m unittest discover -s tests -v
```

## Query the database

```powershell
# Semantic search: embeds the question, then ranks stored vectors by cosine similarity.
.venv\Scripts\python scripts/query_db.py "units with strong melee weapons" --tables datasheets --limit 5

# Exact lookup: does not use the embedding API.
.venv\Scripts\python scripts/query_db.py --name "Warboss" --tables datasheets --full
.venv\Scripts\python scripts/query_db.py --id 000000001 --tables datasheets --json

# Complete detachment, including rules, enhancements, and stratagems.
.venv\Scripts\python scripts/query_db.py --name "Gladius Task Force" --tables detachments --json

# Restrict by exported faction ID.
.venv\Scripts\python scripts/query_db.py "leadership tests" --faction CSM
```

The script reads only the active snapshot and never modifies the database.
Semantic search requires stored embeddings and `OPENAI_API_KEY`; it automatically
uses their model and dimensions, and refuses to mix embedding configurations.
Use `--embedding-config ID` if the selected tables contain multiple configurations.
Scores express vector similarity, not confidence or rules eligibility. Results
include source URLs; `--json` returns complete documents. This is retrieval only,
without an LLM-generated answer. Faction filtering uses the stored faction column;
it does not infer allied eligibility or expand shared-rule associations.

## Administratum Lexi — Discord bot

Configure `.env` with `DISCORD_BOT_TOKEN`, `DISCORD_GUILD_ID`,
`DISCORD_CHANNEL_ID`, and `OPENAI_API_KEY`. `APPLICATION_ID` and `PUBLIC_KEY`
are not needed for this Gateway-based bot. Enable **Message Content Intent**
in Developer Portal → Bot. Install the bot in your test server and grant View
Channel, Send Messages, Read Message History, Embed Links, and, if using threads,
Send Messages in Threads. No Administrator permission is needed.

```powershell
.venv\Scripts\python -m pip install -r requirements.txt
# Creates/validates chat storage; no Discord connection or paid API requests:
.venv\Scripts\python -m lexi.bot --check
# Starts the bot (will respond to incoming messages):
.venv\Scripts\python -m lexi.bot
```

Or run it in Docker (the optional profile leaves the database-only workflow intact):

```powershell
docker compose --profile bot up -d --build
docker compose logs -f bot
docker compose stop bot
```

Default answering model: `OPENAI_CHAT_MODEL=gpt-5-mini`. Override it in `.env`
for a compatible Responses API model. `OPENAI_WEB_MODEL` can separately select
the model used for hosted web research. No hard currency spending cap is
implemented: set project limits/alerts in your OpenAI account. Each question is
bounded to six tool rounds, two web-research requests (up to three hosted calls
each), and a four-minute overall timeout. `LEXI_MAX_TOOL_ROUNDS` supports 1–12.

Set `LEXI_WEB_SEARCH_ENABLED=false` in `.env` for database-only testing. The web
tool is removed from the model's available tools and blocked again at runtime.
The prompt requires freshly retrieved local evidence and an explicit evidence
gap when unavailable. Existing database source URLs and viewer links still work;
disabling web research does not block users opening citations. OpenAI model and
query-embedding API requests still run: this is not a fully offline mode.
Use a fresh conversation (or say “forget this conversation”) for a clean test.
To apply `.env` changes, recreate the container, not just restart it:

```powershell
docker compose --profile bot up -d --force-recreate bot
```

Set the flag back to `true` and recreate the container to enable web research.

Lexi responds to ordinary messages in the configured channel and its threads,
mentions/replies elsewhere in the configured server, and direct messages. Other
servers and bot messages are ignored. One status message updates during each tool
invocation. Responses include citations when provided by retrieved evidence.
The bot performs database search, full-document fetch, and hosted web research,
with repeated lookups to resolve missing evidence. It has no arbitrary SQL or
filesystem tool. Internet page reading is delegated to hosted web research;
a separate direct webpage/PDF reader is not yet implemented.

Chat history lives in a **separate PostgreSQL database**, `lexi_chat`, on the same
instance by default. The setup command creates it using the configured database
role. With managed/external databases, provision it separately and configure
`CHAT_DATABASE_URL`. Rules and chat connections can be overridden independently
using `DATABASE_URL` and `CHAT_DATABASE_URL` (these override individual settings).
When using Docker, connection URLs must point to a container-accessible host.

Conversations are isolated by user and channel/thread/DM, so a shared channel
does not automatically create shared memory across members. Recent 20 messages
are provided as context. Long-term summaries are not yet implemented. Tool
activity/evidence is logged separately. Retention defaults to 30 days, controlled
by `CHAT_RETENTION_DAYS`; cleanup runs on startup, daily, and before a question.
Say **forget this conversation** or **clear our conversation** to erase the saved
history for your current context. Discord messages are not removed by that action.
Message deletion/edit events remove stored copies while the bot is online;
changes made while it is offline are not reconciled automatically. User messages
and relevant retrieved evidence are sent to the model API when answering.

This is an initial implementation: ranking quality and rules rulings should be
tested with your question set before broad rollout. Database associations remain
export associations, not validated rules eligibility. Test and review source
citations, edition handling, and ambiguous questions in the private test server.

## Datasheet cards

Ask naturally, for example: “Show the datasheets for Celestian Sacresants and
Celestian Insidiants.” Lexi resolves IDs and uses `show_datasheets` to display an
interactive card instead of generating a prose dump. Overview and Weapons use
aligned monospace stat tables (Discord does not render HTML tables). Each unit has its own message with two sections: Main (overview, abilities, and
weapons) and Details (options, leaders, and shared rules), plus a Source button.
There is no unit switching. Overflow controls appear only when a section exceeds
Discord limits; rules are never silently truncated. Core/faction definitions are available in Shared rules;
datasheet and other abilities retain their complete conditions and restrictions.
Controls belong to the requester and expire after 15 minutes of inactivity;
ask for the card again after expiry or a bot restart. Source links remain usable.
Navigation uses the loaded snapshot and does not call the model API. Tables may
scroll or wrap on narrow mobile screens. The display tool logs complete documents
for debugging and saves a concise record in conversation history.

## PDF rules archive and page viewer

### Import the Data/Documents folder

```powershell
# Plan only (reads current DB to detect duplicate files):
.venv\Scripts\python -m scripts.import_documents --dry-run
# Import all PDFs recursively, with resumable embeddings:
.venv\Scripts\python -m scripts.import_documents --embed
# Store/extract without API calls:
.venv\Scripts\python -m scripts.import_documents
```

Identical files with known provenance reuse their existing document version even
if renamed. Explicit metadata overrides this lookup. Repeated runs do not duplicate
existing versions or vectors. New/unrecognized PDFs default to **edition 0
(unverified)** rather than being silently classified as 11th. No folder import
automatically marks extraction reviewed or activates new rules for Lexi.

For new files, add an optional `Data/Documents/manifest.json` mapping relative
filenames to publication metadata:

```json
{
  "subfolder/rules.pdf": {
    "title": "Publication title",
    "document_id": "publication-slug",
    "edition": 11,
    "edition_evidence": "Where the edition was verified",
    "revision": "Revision identifier",
    "published_date": "2026-06-01",
    "source_url": "https://example.com/original.pdf",
    "kind": "rules"
  }
}
```

Use `--folder PATH` and `--manifest PATH` for other locations. Non-PDF files are
ignored. Completed work is retained if another file fails; exit status is nonzero
when failures occur. The latest report, including version IDs and viewer links,
is saved to `generated/document-folder-import.json` (`--report PATH` overrides it).
This batch wrapper uses the single-PDF pipeline and its extraction limitations.

### Services and single-file import

The optional `documents` profile adds a dedicated MinIO bucket store and a local
source viewer. The existing MinIO on ports 9000/9001 is not used or modified.
New ports: 19000 (S3), 19001 (object console), 8081 (viewer), all bound to localhost.
The trial uses the already-cached `minio/minio:latest` image (`pull_policy: never`).
The upstream community repository is archived and its old images are not a
maintained deployment choice; select/build a maintained S3-compatible deployment
before public hosting. `MINIO_IMAGE` can override the image. Existing S3 services
can instead be configured using `MINIO_ENDPOINT`, credentials, and `MINIO_SECURE`.

Set the variables in `.env.example`, using random secrets (at least 32 characters
for `VIEWER_SIGNING_KEY`). Do not replace existing configured secrets on every run.

```powershell
docker compose --profile documents up -d --build
```

Import an authorized PDF from a URL, or use `--file PATH --source-url URL`:

```powershell
.venv\Scripts\python -m scripts.import_pdf --url "https://example.com/rules.pdf" --title "Rules" --document-id core-rules --edition 11 --edition-evidence "Verified publication information" --revision "Publication revision"
```

The importer stores the complete original in a private, versioned MinIO bucket,
and creates `documents`, `document_versions`, `document_sections`,
`document_chunks`, and `document_relations` in the existing rules database.
Checksums and immutable version IDs keep citations tied to exact bytes and an
extraction pipeline version. Source URLs and physical PDF pages are preserved;
printed page labels are recorded when actually available in the PDF metadata.
New imports are inactive by default. `generated/documents/<version>/pages.json`
and `extracted.md` allow extraction review, including tables and column order.
PDF extraction is not guaranteed to be perfect; scanned documents need a separate
OCR workflow. Page text is grouped by headings across page boundaries and chunked
at paragraph boundaries; complete parent sections remain retrievable.

Rerun the exact same command with `--reviewed --activate --embed` after checking
edition and extraction. `--embed` generates resumable 1,536-dimensional chunk
embeddings via `text-embedding-3-small`. This is independent of datasheet embeddings.
Activating a new revision deactivates the prior revision of that publication and
edition, without deleting old files, text, or embeddings. Downloading the same file
with the same metadata is idempotent. Extraction changes require incrementing the
pipeline version; old immutable versions must not be edited in place.

`search_rules` uses lexical plus vector ranking on reviewed, active versions of
the requested edition only (11 by default). `get_rule_section` fetches the full
parent section with page spans. Lexi must check applicable amendments separately;
an active base rulebook is not automatically a consolidated/latest rulebook.
`document_relations` stores manually verified amendment links; automatic errata
application and update discovery are not implemented yet.

Sources have both the official PDF URL with `#page=N` and a signed local viewer
URL for that immutable version/page. Cited PDF evidence adds source-page link
buttons to Discord answers. The hosted viewer renders the actual page as an
image with previous/next and page selection, so mobile browsers do not need to
honour PDF fragments. It also links to the complete original PDF and official
source. This initial viewer does not provide selectable text/search/highlights.
Image pages and original files are accessible only with a valid signed link;
the links are bearer capabilities and remain valid until the signing key changes.
Do not publish the signing key or MinIO credentials. Browser history/links can
contain these capabilities; the server disables access logs and referrers.

`VIEWER_PUBLIC_BASE_URL=http://localhost:8081` is for the host PC only. Discord
users on other devices need a reachable HTTPS hostname and appropriate access
controls; changing the URL does not create public hosting. MinIO remains private.
No public endpoint, tunnel, or domain is set up by this prototype.

## Debug a conversation sequentially

In the **lexi_chat** database, open the `debug_timeline` view:

```sql
SELECT step, event_time, user_id, event_type, text, tool_name, source_urls
FROM debug_timeline
ORDER BY step;
```

Each row is a question, completed tool call, or answer. For a focused investigation:

`conversations.user_id` is the conversation owner; `guild_id` and `channel_id`
identify its location (guild is null for DMs). These columns are backfilled for
existing Discord conversations and included in the timeline. `author_id` identifies
the event's author: user for questions, bot for answers, triggering user for tools.

```sql
SELECT * FROM debug_timeline
WHERE conversation_id = 'your-conversation-id'
ORDER BY step;

SELECT step, tool_name, arguments, source_urls, result
FROM debug_timeline
WHERE question_message_id = 'user-question-message-id'
ORDER BY step;
```

`arguments` shows the actual lookup; `result` is the complete evidence supplied to
the model. `source_urls` deduplicates structured URLs from that evidence; some
are cross-references, not sources actually cited in the final answer. Read the
answer text to compare its citations. Tool rows link directly to their triggering
question. Historical answer rows have no explicit triggering-question ID; their
position and conversation establish the visible sequence, without guessing a link.
Times are database save/completion times, not tool start times. The view does not
contain private reasoning or full model requests. It includes existing records,
and retention/deletion automatically affects it. Refresh the database client’s
Views list if it is not visible. Initialization recreates this view on bot startup.


## Slash commands and personal preferences

The bot registers eight commands in the configured test guild on startup:
`/help`, `/prompt`, `/personality`, `/forget`, `/settings`, `/sources`, `/context`,
and `/debug`. No `/feedback` or `/detail` is registered. Registration replaces
this application's existing guild command list. Commands currently appear in the
configured server, not DMs. Guild members need Use Application Commands permission.

All command responses are ephemeral. `/debug` checks the application owner
(or team owner) at execution time; it is not granted to arbitrary server admins.
An owner can supply a question message ID to inspect another recorded trace;
without one, the command selects their latest question in the current context.
The attachment includes recorded tool arguments/results and final text, not
private model reasoning or a full model request log. `/sources` is limited to the
calling user's current conversation and separates final-answer links from
retrieved evidence URLs, which may include uncited cross-references.

`/personality mode:neutral` disables roleplay in new answers and progress updates;
`mode:lexi` restores light archivist flavour. Omit the mode to view it.
`/prompt action:set text:...` stores up to 1,500 characters; `view` and `reset`
inspect/clear it. `/context action:set faction:... detachment:...` stores usual
army context; a faction change clears an old detachment unless a new one is
provided. These are user-supplied defaults, not verified army eligibility.
Current explicit questions take precedence. Personal prompts are passed as user
preferences below the fixed edition, evidence, and access-control instructions.

Preferences live in `lexi_chat.user_settings`, keyed by Discord user ID and shared
across that user's conversations. They persist across restarts until reset.
`/forget` clears only the user's history and tool activity in the current
channel/thread/DM, not personal settings or Discord messages. It refuses to clear
history while an answer is running. Natural-language conversation clearing remains
available in DMs, where guild slash commands are not currently registered.


## Numbered Core Rules PDF extraction

For the digital June 2026 Core Rules, use `--extractor numbered` with
`scripts.import_pdf` (or `"extractor": "numbered"` in the folder manifest).
This profile extracts numbered rule sections from native PDF text and recovers
vector dice faces used for numerical thresholds. It preserves full page text
in `document_pages` alongside sections and chunks, allowing rule retrieval to
include neighbouring page context. It does not perform OCR on scanned PDFs.

Review `generated/documents/<version>/quality-report.json`, the extracted text,
and the source PDF before using `--reviewed --activate`. Unresolved replacement
characters or unplaced dice block activation. Original PDFs remain unchanged in
MinIO; citations identify the version and PDF page. Embeddings can be resumed
with `--embed`. The active June 2026 Core Rules are the initial publication,
not an automatic consolidation of later amendments.


## Wahapedia rules snapshot trial

Run `.venv\Scripts\python -m scripts.scrape_rules` to fetch the 11th-edition
Core Rules page once and export numbered entries to `generated/wahapedia/<sha256>/`.
The export includes `source.html`, `rules.json`, and `rules.md`, with source anchors,
cross-references, retrieval time and a content hash. Use `--html <snapshot.html>`
to repeat extraction offline. CSS dice values are converted to numbers. FAQ text
is kept with its enclosing rule; combined Leader/Support cards retain both rule
numbers and repeated presentations remain available for review.

This is an extraction trial: it does not import, embed or activate database records.
Wahapedia is labelled a secondary compilation, and retrieval time is not presented
as an official publication or amendment date. Rules without individual anchors
link to the Core Abilities section. Review extraction before database integration.


## Generated Wahapedia PDF import

Generate a reading copy and page map with:

```powershell
.venv\Scripts\python -m scripts.build_rules_pdf generated/wahapedia/<snapshot-sha>/rules.json
```

Outputs are `output/pdf/wahapedia-core-rules.pdf` and its `.json` sidecar. The
builder currently uses Windows Arial fonts. Each numbered entry begins a fresh
page; long entries continue onto following pages. The sidecar preserves original
rule text and maps it to the generated PDF's page range. No OCR or PDF text
extraction is used to reconstruct rule chunks. Page context uses PDF native text.

Import with `scripts.import_pdf --file output/pdf/wahapedia-core-rules.pdf
--sections-json output/pdf/wahapedia-core-rules.json` plus the usual publication
metadata and `--embed --reviewed --activate` after review. The PDF SHA-256 must
match the sidecar. Use a distinct document ID such as `wahapedia-core-rules` and
kind `secondary-rules-snapshot`; leave publication date unset unless known.
Retrieval date belongs in the revision, not an invented official effective date.

The generated PDF is stored in MinIO and receives normal signed viewer/page
links. HTML source URLs remain HTML URLs, without fictitious PDF page fragments.
The official June PDF is retained as a separate source; Lexi must identify any
wording differences and label Wahapedia evidence as a secondary compilation.
