"""planner_api — self-contained FastAPI service for the agentic Planner→SQL pipeline.

Everything (OIDC Bedrock client, Postgres/embedding clients, Strands agents, retrieval,
sqlglot validation) is implemented inside this package; nothing is imported from
``planner_eval`` or the repo root.
"""

__version__ = "0.1.0"
