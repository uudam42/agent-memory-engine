# Knowledge Pipeline

## Ingestion flow

```
Source file / content
      │
      ▼
 Redaction (redaction.py)
      │  8 regex patterns: API keys, tokens, passwords, private keys,
      │  connection strings, JWTs, SSH, cloud credentials
      │
      ▼
 Content hash check (SHA-256)
      │  Duplicate → update existing document
      │  New       → create KnowledgeDocument
      │
      ▼
 Source-type dispatch (chunkers.py)
      │
      ├─ Markdown   → chunk_markdown()   — heading-based sections ≤1200 tokens
      ├─ Code       → chunk_code()       — class/function blocks  ≤1000 tokens
      ├─ ADR        → chunk_markdown()   — same as markdown
      ├─ Test report→ chunk_test_report() — test-result windows
      ├─ Log        → chunk_log()        — sliding log windows    ≤600 tokens
      └─ Diff/Patch → chunk_diff()       — hunk-based chunks
      │
      ▼
 KnowledgeChunk rows (SQLite)
      │
      ├─ FTS5 insert  (fts_index.py)
      │    columns: content, heading_text, symbols_text, module_text, tags_text
      │    tokenizer: porter unicode61
      │
      └─ Vector upsert  (vector_index.py)
           InMemoryVectorIndex: bag-of-words cosine (ephemeral)
           Optional: QdrantVectorIndex (not installed by default)
      │
      ▼
 Cache invalidation
      └─ SimpleCache.invalidate_project(project_id)
```

## Source types

| SourceType | Example files |
|---|---|
| `readme` | README.md, README.rst |
| `architecture_doc` | ARCHITECTURE.md, docs/architecture.md |
| `adr` | docs/adr/001-*.md |
| `code_file` | *.py, *.ts, *.go, *.rs |
| `markdown` | *.md (general) |
| `manual_note` | .memory-engine/constraints.md |
| `api_spec` | *.yaml, *.toml, openapi.json |
| `test_report` | pytest output, JUnit XML |
| `runtime_log` | *.log |
| `git_diff` | *.diff, *.patch |

## Redaction patterns

Before any content is persisted or returned via MCP, `redact(content)` runs:

1. `OPENAI_API_KEY=sk-...` → `OPENAI_API_KEY=[REDACTED]`
2. `Bearer eyJ...` → `Bearer [REDACTED]`
3. `password: ...` → `password: [REDACTED]`
4. `-----BEGIN RSA PRIVATE KEY-----` → `[REDACTED_PRIVATE_KEY]`
5. `postgresql://user:pass@host/db` → `[REDACTED_URL]`
6. `AKIA...` (AWS key) → `[REDACTED_AWS_KEY]`
7. `ghp_...` (GitHub token) → `[REDACTED_GH_TOKEN]`
8. `xoxb-...` (Slack token) → `[REDACTED_SLACK_TOKEN]`

Redaction runs twice: before persistence, and before MCP output.

## KnowledgeDocument / KnowledgeChunk fields

**KnowledgeDocument** (one per source file):
- `document_id`, `project_id`, `source_type`, `title`, `content_hash`
- `status`: `pending | indexed | error`
- `source_path`, `source_url`, `version_tag`, `indexed_at`

**KnowledgeChunk** (many per document):
- `chunk_id`, `document_id`, `project_id`
- `content`, `content_hash`
- `heading_path` (list of section headers)
- `module_paths`, `related_symbols`
- `start_line`, `end_line`, `source_path`, `language`
- `index_status`: `pending | indexed | stale`
