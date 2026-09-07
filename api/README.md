# api — FastAPI service: agentic Planner → SQL (generate-only)

Layanan REST **mandiri** yang membungkus pipeline agentic: **goal → retrieval+gate →
plan (agent) → SQL (agent) → validasi sqlglot**. Ditulis penuh di dalam folder ini —
**tidak meng-import** apa pun dari `planner_eval/` atau root repo (federasi OIDC,
client PG/embedding, provider Strands, agents, retrieval, validator semua di sini).

> **Generate-only.** Tidak ada evaluator LLM (plan/SQL judge) maupun table-metrics.
> `sql_validator` adalah **validasi statik deterministik** (sqlglot), bukan evaluasi.

## Arsitektur

```
POST /v1/generate-sql ──► JobRunner (sync; pipeline di worker thread) ──► response
                                                    │
   retrieval+gate ──► plan (gpt-oss/Strands) ──► SQL (gpt-oss/Strands) ──► sqlglot validate
   (hybrid+fallback)   tool: search_schema_tables    tool: lookup_columns     (statik, katalog)
   response ◄── {status_code, response_code, error_message, data:{request_id, started_at, finished_at, result}}
```

| Lapis | File |
|-------|------|
| Clients (OIDC Bedrock, PG pool, embedding) | `app/clients/{bedrock,postgres,embeddings}.py` |
| Agents (provider Strands, tools, planner, sql_writer) | `app/agents/*` |
| Services (fusion, retrieval+gate, semantic search, validator, schema harness) | `app/services/*` |
| App (pipeline, jobs, response, resources, errors, schemas, routers, main) | `app/*`, `app/routers/*` |

Fusion `0.3*zscore(dense bge-m3) + 0.7*zscore(sparse enriched-BM25)` didefinisikan
sekali di `app/services/fusion.py` dan dipakai bersama oleh retrieval tiket
(`retrieval.py`) maupun semantic search katalog (`schema_search.py`).

## Endpoint

| Method | Path | Fungsi |
|--------|------|--------|
| POST | `/v1/generate-sql` | jalankan pipeline sinkron; blok s.d. selesai (HTTP selalu 200) |
| POST | `/v1/search-tables` | semantic search tabel (fusion) → daftar tabel |
| POST | `/v1/search-columns` | semantic search kolom (fusion); bisa difilter `table_name` |
| GET | `/health` | liveness (selalu 200) |
| GET | `/ready` | cek PG/embedding/Bedrock → `ready\|degraded\|unavailable` |

Request body:
- `generate-sql`: `{ "goal": str (req), "k": 10 }`
- `search-tables`: `{ "query": str (req), "k": 10 }`
- `search-columns`: `{ "query": str (req), "k": 10, "table_name": str? }`

Ketiganya memakai **envelope response yang sama**:
```json
{
  "status_code": 200,                 // kode outcome HTTP-style (200 sukses, mis. 503/502/504 bila gagal)
  "response_code": "succeeded",       // succeeded|partial atau kode error (mis. retrieval_error)
  "error_message": null,              // pesan error bila gagal, else null
  "data": {
    "request_id": "…",
    "started_at": "…+07:00", "finished_at": "…+07:00",
    "result": "…"                     // generate-sql: string SQL; search-*: list hasil; null bila gagal
  }
}
```
- `generate-sql` → `data.result` = string SQL final.
- `search-tables` → `data.result` = `[{table_name, source_schema, domain_tags[], n_columns, table_description, score}, …]`.
- `search-columns` → `data.result` = `[{table_name, field_name, business_title, data_type, description, score}, …]`.

Untuk `generate-sql`, retrieval/plan/validasi/timing/warnings tetap jalan di internal
(+ log) tapi TIDAK diekspos — hanya SQL final yang dikembalikan. Semantic search
degradasi ke **sparse-only BM25** bila embedding mati (tetap mengembalikan hasil).

## Menjalankan

```bash
pip install -r api/requirements.txt          # + kredensial via .env (lihat .env.example)
.venv/Scripts/python.exe -m uvicorn api.app.main:app --port 8100 --workers 1

curl -s localhost:8100/ready
curl -s -X POST localhost:8100/v1/generate-sql -H "content-type: application/json" \
     -d '{"goal":"Berikan data rekening dormant saldo>0 per kode uker posisi 31 Desember 2025"}'
# blok sampai selesai → 200 dengan status succeeded/partial/failed

curl -s -X POST localhost:8100/v1/search-tables -H "content-type: application/json" \
     -d '{"query":"rekening tabungan dormant saldo","k":5}'
curl -s -X POST localhost:8100/v1/search-columns -H "content-type: application/json" \
     -d '{"query":"saldo rata-rata","k":5,"table_name":"product_holding_fact_cif_all"}'
```

Konfigurasi lewat env / `.env` (lihat `.env.example`): pool & konkurensi, timeout
per-stage, retry, kredensial Bedrock/OIDC, PG, embedding. `CONF_THRESHOLD=38` (gate).

## Docker

`Dockerfile` + `.dockerignore` ada di folder ini (`api/`). Build context = `api/`
(dump 684 MB di `../dumps/` otomatis tidak ikut). Image: `python:3.12-slim`, non-root,
uvicorn `app.main:app` di port `8000` (override via env `PORT`/`WORKERS`), plus
HEALTHCHECK ke `/health`.

**Postgres = Cloud SQL via Private IP.** Bukan `localhost`/docker-internal — set
`PG_HOST` ke **private IP** instance Cloud SQL (`10.x.x.x`). Container **wajib** jalan di
host/subnet yang punya akses VPC ke instance itu (mis. GKE/Compute Engine di VPC yang
sama, atau via VPC connector). Set `PG_SSLMODE=require` bila instance mewajibkan SSL.

```bash
cd api
docker build -t planner-api .

# jalankan di host yang punya akses VPC ke Cloud SQL (private IP)
docker run --rm -p 8100:8000 --env-file .env \
  -e PG_HOST=10.0.0.3 -e PG_PORT=5432 \
  -e PG_SSLMODE=require \
  planner-api

curl -s localhost:8100/health     # {"status":"ok",...}
curl -s localhost:8100/ready      # ready|degraded|unavailable
```

Di GKE, jalankan Pod di VPC ber-akses Cloud SQL dan suntik `PG_HOST` (private IP) +
kredensial via `Secret`/`env`. Tidak perlu Cloud SQL Auth Proxy untuk jalur private IP.

Catatan: `.env` **tidak** di-copy ke image (di-ignore) — kredensial diinjeksi saat
run via `--env-file`/`-e`/Secret. `/ready` akan `503` bila PG/embedding belum tersambung
(mis. container tidak berada di VPC yang bisa menjangkau private IP Cloud SQL).

## Error handling untuk agentic AI

- **Taxonomy** (`app/errors.py`): `bad_request`(400), `not_found`(404),
  `upstream_rate_limited`(429), `retrieval_error`/`dependency_unavailable`(503),
  `planner_error`/`sql_writer_error`/`upstream_auth_error`(502), `upstream_timeout`(504),
  generic(500). Respons seragam `{error:{code,message,stage,request_id,retryable}}`.
- **Retry berbackoff** hanya untuk error **transient** (`classify()`: 429/5xx/
  ConnectionError/Timeout/ExpiredToken); auth/4xx tidak diulang.
- **Timeout nyata**: per-stage via `future.result(timeout)`; client Bedrock dibangun
  dengan botocore `Config(read/connect timeout)`; PG `connect_timeout`; embedding timeout.
  Plus **wall-clock cap** per job (`JOB_MAX_SECONDS`) untuk batasi biaya runaway.
- **Degradasi anggun**: embedding mati → retrieval **sparse-only** (`degraded=true` +
  warning), job tetap jalan.
- **Partial result**: stage non-kritis (sql/validate) gagal → job `partial`, field null,
  `warnings[]`; stage kritis (retrieval, plan) gagal → job `failed` + `error`.
- **Khusus agentic**: guard **empty-turn** planner/SQL (retry + fallback agent tanpa
  tool → bila tetap kosong `PlannerError`/`SQLWriterError`); **tool call gagal** (PG)
  ditangkap di dalam loop (tak meledak); **declined/off-domain** = hasil sah
  (`declined=true`), bukan error.
- **Isolasi konkurensi**: `BedrockClient` **tidak** dibagi antar-request (pool
  memberi 1 client eksklusif per job; `invoke` juga thread-safe via lock); Strands
  `Agent` dibuat **fresh per request** (mencegah bocor history); PG lewat pool.
- **Observability**: middleware **request-id** (`X-Request-ID` in/echo), log timing
  per-stage + error dengan request_id.

## Uji

```bash
.venv/Scripts/python.exe -m pytest api/tests -q      # 19 tes (mock, tanpa jaringan)
```
Cakupan: routing, 422/404, health/ready, job lifecycle (succeeded/partial/failed),
error mapping, degradasi, dan validator sqlglot. Smoke live end-to-end terbukti:
retrieval(hybrid) → plan → SQL grounded → `validation.parses=true, static_score=1.0`.

## Batasan

- Job store **in-memory** → jalankan `--workers 1`; multi-worker butuh store bersama
  (Redis/DB).
- Timeout stage bersifat responsif (client tak menunggu); panggilan library yang sudah
  terlanjur berjalan diselesaikan di background, dibatasi timeout library.
- Membaca data KB dari Postgres (`era_corpus`, `schema_tables`, `schema_columns`) —
  data bersama, bukan kode.
