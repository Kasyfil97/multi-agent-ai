"""Configuration for the planner_api service.

Reads from the environment / a .env file (config, not code). Groups: service tuning,
resource pools, per-stage timeouts & retries, and upstream credentials (Bedrock/OIDC,
Postgres, embedding service). No Azure/evaluator settings — this service only
generates SQL.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore",
                                      case_sensitive=False)

    # --- service ---
    app_name: str = "planner-api"
    log_level: str = "INFO"
    log_json: bool = False

    # --- concurrency / pools ---
    max_concurrency: int = 4          # sizes stage thread pool / bedrock pool
    bedrock_pool_size: int = 4        # >= max_concurrency recommended
    pg_pool_min: int = 1
    pg_pool_max: int = 8

    # --- job lifecycle ---
    job_max_seconds: int = 240        # wall-clock cap per job (runaway guard)

    # --- per-stage timeouts (seconds) ---
    retrieval_timeout: int = 45
    plan_timeout: int = 150
    sql_timeout: int = 150
    validate_timeout: int = 20

    # --- retry (transient errors only) ---
    retries: int = 2
    backoff_base: float = 1.5         # seconds; exponential

    # --- retrieval ---
    retrieval_k: int = 5
    conf_threshold: float = 38.0      # gate: conf_sparse >= threshold => relevant

    # --- Bedrock / OIDC (gpt-oss-120b) ---
    aws_region: str = "ap-southeast-3"
    bedrock_model_id: str = "openai.gpt-oss-120b-1:0"
    bedrock_read_timeout: int = 140
    bedrock_connect_timeout: int = 15
    azure_tenant_id: str = ""
    azure_client_id: str = ""
    azure_client_secret: str = ""
    aws_role_arn_bridge: str = ""
    aws_role_arn_target: str = ""

    # --- Postgres (Cloud SQL via Private IP; container harus punya akses VPC) ---
    pg_host: str = "localhost"        # prod: private IP instance Cloud SQL (10.x.x.x)
    pg_port: int = 5432
    pg_dbname: str = "postgres"
    pg_user: str = "admin"
    pg_password: str = "admin123"
    pg_kb_schema: str = "public"
    pg_sslmode: str = ""              # kosong=default libpq; set 'require'/'verify-ca' bila SSL diwajibkan

    # --- embedding service (bge-m3) ---
    embed_url: str = ""
    embed_token: str = ""
    embed_model: str = "BAAI/bge-m3"
    embed_dim: int = 1024
    embed_timeout: int = 30


@lru_cache
def get_settings() -> Settings:
    return Settings()
