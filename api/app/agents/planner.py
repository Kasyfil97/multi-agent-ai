"""Planner agent — Strands ``Agent`` over gpt-oss-120b. Goal + gated retrieval → plan.

Handles the four cases (A single-ticket / B multi-ticket / C found-but-irrelevant /
D none). For C/D schema candidates are pre-fetched and injected (deterministic,
avoids the empty-final-turn after a tool round-trip); the agent may still call
``search_schema_tables``. Empty-turn guard: retry, then a tool-less fallback agent;
if still empty → ``PlannerError``.
"""
from __future__ import annotations

from strands import Agent

from ..errors import PlannerError
from ..logging_config import get_logger
from ..services.retrieval import format_context
from ..services.schema import format_schema_tables, search_schema_tables
from .prompts import PLANNER_SYSTEM_PROMPT
from .strands_model import StrandsBedrockModel
from .tools import search_schema_tables as search_schema_tables_tool

log = get_logger("agents.planner")


def build_planner(client, *, max_tokens=3500, with_tools=True):
    model = StrandsBedrockModel(client, max_tokens=max_tokens)
    tools = [search_schema_tables_tool] if with_tools else []
    return Agent(model=model, tools=tools, system_prompt=PLANNER_SYSTEM_PROMPT)


def _prefetch_schema(goal, pool):
    if pool is None:
        return ""
    try:
        with pool.borrow() as conn:
            return format_schema_tables(search_schema_tables(goal, conn, k=5))
    except Exception as exc:  # noqa: BLE001 — non-fatal, planner can still ask clarification
        log.warning("planner schema prefetch failed: %s", exc)
        return "(pencarian skema gagal)"


def build_prompt(goal, gate, schema_txt=""):
    status = gate["status"]
    conf = gate["conf_sparse"]
    conf_s = f"{conf:.1f}" if conf is not None else "n/a"
    header = (
        f"# PERMINTAAN DATA (GOAL) DARI USER\n{goal}\n\n"
        f"# STATUS RETRIEVAL\n- status: {status}\n"
        f"- conf_sparse (top-1 raw BM25): {conf_s}  (ambang relevansi = {gate['threshold']})\n"
        f"- metode: {gate['retrieval_method']}\n"
        f"- tiket relevan (lolos ambang): {len(gate['relevant_tickets'])} dari "
        f"{len(gate['tickets'])}\n\n")
    if status == "relevant":
        body = ("# KONTEKS: TIKET RELEVAN (lolos ambang — boleh dipakai)\n"
                + format_context(gate["relevant_tickets"]) + "\n\n"
                "Tentukan KASUS A (satu tiket cukup) atau B (perlu beberapa tiket) "
                "lalu susun rencana.")
    elif status == "no_relevant":
        body = ("# CATATAN: kandidat tiket DITEMUKAN tapi SEMUA di bawah ambang → "
                "anggap TIDAK relevan. JANGAN grounding ke tiket ini. Ini KASUS C — "
                "gunakan hasil pencarian skema di bawah (boleh panggil "
                "`search_schema_tables` lagi) lalu susun rencana + klarifikasi.\n\n"
                "# KANDIDAT DI BAWAH AMBANG (jangan diandalkan)\n"
                + format_context(gate["tickets"], max_solution_chars=300))
    else:  # no_candidates
        body = ("# CATATAN: TIDAK ada tiket sama sekali. Ini KASUS D — gunakan hasil "
                "pencarian skema di bawah untuk menemukan tabel sumber, lalu susun "
                "rencana + klarifikasi. Jangan mengarang tabel/kolom.")
    if status != "relevant":
        body += ("\n\n# HASIL PENCARIAN SKEMA (search_schema_tables) — kandidat tabel:\n"
                 + (schema_txt or "(tidak ada)") +
                 "\n\nSusun rencana berbasis tabel kandidat di atas dan cantumkan "
                 "pertanyaan klarifikasi. Jika tak satupun tabel relevan (mis. di luar "
                 "domain data BRI), TOLAK dengan sopan dan minta detail — jangan mengarang.")
    return header + body


def generate_plan(goal, gate, client, pool=None):
    """Return the plan markdown. Raises PlannerError if empty after all fallbacks."""
    schema_txt = _prefetch_schema(goal, pool) if gate["status"] != "relevant" else ""
    prompt = build_prompt(goal, gate, schema_txt)

    agent = build_planner(client, with_tools=True)
    plan = str(agent(prompt)).strip()
    for _ in range(2):
        if plan:
            break
        plan = str(agent("Keluarkan SEKARANG rencana final lengkap dalam format "
                         "markdown sesuai struktur (bagian 0-5). Jangan kosong, jangan "
                         "hanya memanggil tool.")).strip()
    if not plan:
        fb = build_planner(client, with_tools=False)
        plan = str(fb(prompt)).strip()
    if not plan:
        raise PlannerError("planner returned empty plan after retries", stage="plan")
    return plan
