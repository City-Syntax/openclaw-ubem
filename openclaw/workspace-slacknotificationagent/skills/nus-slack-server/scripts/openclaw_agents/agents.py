"""
openclaw_agents/agents.py
Glue layer between slack_server.py and the OpenClaw QueryAgent (Oracle 🔮).

QueryAgent calls Oracle via the Anthropic API, providing it with:
  - The question from the facilities team
  - Live data context loaded from the NUS pipeline outputs directory

No Anthropic SDK required — uses requests directly.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger("openclaw_agents")

# ── Config ─────────────────────────────────────────────────────────────────────
NUS_PROJECT_DIR  = Path(os.getenv("NUS_PROJECT_DIR", "/Users/ye/nus-energy"))
QUERY_SCRIPT     = Path(__file__).parent.parent.parent.parent.parent  # up to skills dir
# Resolved at call time via _find_query_script()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL   = os.getenv("QUERY_AGENT_MODEL", "claude-sonnet-4-6")
ANTHROPIC_URL     = "https://api.anthropic.com/v1/messages"

MAX_TOKENS   = 1024
QUERY_SCRIPT_PATH = Path(os.getenv(
    "QUERY_SCRIPT_PATH",
    "/Users/ye/.openclaw/workspace-queryagent/skills/nus-query/scripts/query.py"
))

# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class AgentMessage:
    sender:   str
    receiver: str
    building: str
    stage:    str
    payload:  dict[str, Any] = field(default_factory=dict)


@dataclass
class PipelineState:
    building: str
    metadata: dict[str, Any] = field(default_factory=dict)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _run_query_script(flag: str, extra: list[str] | None = None) -> str:
    """Run query.py with a given flag, return stdout."""
    cmd = ["python3", str(QUERY_SCRIPT_PATH), flag] + (extra or [])
    env = {**os.environ, "NUS_PROJECT_DIR": str(NUS_PROJECT_DIR)}
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, env=env
        )
        return result.stdout.strip() or result.stderr.strip()
    except subprocess.TimeoutExpired:
        return "⚠️ Data query timed out."
    except FileNotFoundError:
        return f"⚠️ query.py not found at {QUERY_SCRIPT_PATH}"


IDF_BASE_DIR = NUS_PROJECT_DIR / "idfs"

# Known archetype folder names (all subdirs under idfs/)
def _list_archetype_folders() -> list[str]:
    if not IDF_BASE_DIR.exists():
        return []
    return [d.name for d in IDF_BASE_DIR.iterdir() if d.is_dir()]


def _buildings_in_folder(folder_name: str) -> list[str]:
    """Return building codes (stems) from all IDFs in a given archetype folder."""
    folder = IDF_BASE_DIR / folder_name
    if not folder.exists():
        return []
    return [p.stem for p in sorted(folder.glob("*.idf"))]


def _detect_archetype_in_question(question: str) -> str | None:
    """Return archetype folder name if mentioned in the question (e.g. A1_H_L)."""
    for folder in _list_archetype_folders():
        if re.search(r"\b" + re.escape(folder) + r"\b", question, re.IGNORECASE):
            return folder
    return None


def _gather_data_context(question: str) -> str:
    """
    Pre-fetch relevant data snippets to include in the system prompt.
    Always includes the campus summary; adds building-specific data if detected.
    """
    lines = []

    # Always include summary
    summary = _run_query_script("--summary")
    if summary:
        lines.append("=== Campus Summary ===")
        lines.append(summary)

    # MAPE ranking
    ranking = _run_query_script("--ranking", ["mape"])
    if ranking:
        lines.append("\n=== MAPE Ranking ===")
        lines.append(ranking)

    # Archetype/folder mentioned? Fetch data for all buildings in that folder
    archetype = _detect_archetype_in_question(question)
    if archetype:
        buildings_in_archetype = _buildings_in_folder(archetype)
        lines.append(f"\n=== Archetype {archetype} ({len(buildings_in_archetype)} buildings) ===")
        lines.append(f"Buildings: {', '.join(buildings_in_archetype)}")
        for b in buildings_in_archetype:
            bdata = _run_query_script("--building", [b])
            if bdata and "No simulation output" not in bdata:
                lines.append(f"\n--- {b} ---")
                lines.append(bdata)

    # Building-specific? Check for known building codes in the question
    q_upper = question.upper()
    known = ["FOE6", "FOE13", "FOE18", "FOS43", "FOS46"]
    for b in known:
        if b in q_upper:
            bdata = _run_query_script("--building", [b])
            if bdata:
                lines.append(f"\n=== {b} Detail ===")
                lines.append(bdata)

    # Carbon / BCA if asked
    if any(w in question.lower() for w in ["carbon", "co2", "emission", "intervention", "reduction"]):
        carbon = _run_query_script("--campus-carbon")
        if carbon:
            lines.append("\n=== Campus Carbon ===")
            lines.append(carbon)

    if any(w in question.lower() for w in ["bca", "green mark", "benchmark", "efficient", "intensity", "eui", "energy use", "energy intensity"]):
        bca = _run_query_script("--bca-gap")
        if bca:
            lines.append("\n=== BCA Gap Analysis ===")
            lines.append(bca)
        eui_ranking = _run_query_script("--ranking", ["eui"])
        if eui_ranking:
            lines.append("\n=== EUI Ranking ===")
            lines.append(eui_ranking)

    return "\n".join(lines) if lines else "No pipeline data available yet."


NUS_SYSTEM_PROMPT = """\
You are Oracle 🔮, the NUS campus energy query agent.
You answer questions from the NUS facilities team about campus building energy performance.

Rules:
- Ground every answer in the DATA CONTEXT below. Never fabricate numbers.
- If data is missing, say "No data available for X — run Forge first."
- Always include units: MAPE 18.3%, not "high". 245 kWh/m²/year, not "a lot".
- Be concise — facilities team reads on mobile. Max 10 lines.
- Lead with the direct answer, then the supporting number(s).

NUS Domain Constants:
- Grid carbon: 0.4168 kgCO2e/kWh (EMA 2023)
- Tariff: SGD 0.28/kWh
- BCA Green Mark 2021: Platinum ≤85, Gold Plus ≤100, Gold ≤115, Certified ≤130 kWh/m²/year
- MAPE target: <15% (ASHRAE G14)
- 5 buildings with ground truth (MAPE available): FOE6, FOE13, FOE18, FOS43, FOS46
- Archetype folders under idfs/: A1_H_L, A1_L_L, A1_M_H, A1_M_L, A5 (and others)
- For non-GT buildings, only simulated totals are available (no MAPE)
- "Interventions" = actions that reduce energy: chiller setpoint, lighting, scheduling, insulation, solar
- 5% carbon reduction target means 5% reduction in kWh (same carbon factor applies)

DATA CONTEXT (from pipeline outputs):
{data_context}
"""


def _call_anthropic(system: str, question: str) -> str:
    """Call Anthropic API directly via requests."""
    if not ANTHROPIC_API_KEY:
        return "⚠️ ANTHROPIC_API_KEY not set — Oracle cannot answer."

    headers = {
        "x-api-key":         ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type":      "application/json",
    }
    body = {
        "model":      ANTHROPIC_MODEL,
        "max_tokens": MAX_TOKENS,
        "system":     system,
        "messages":   [{"role": "user", "content": question}],
    }

    try:
        resp = requests.post(ANTHROPIC_URL, headers=headers, json=body, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"].strip()
    except requests.HTTPError as e:
        log.error(f"Anthropic API error: {e.response.status_code} {e.response.text}")
        return f"⚠️ API error {e.response.status_code} — check ANTHROPIC_API_KEY."
    except Exception as e:
        log.error(f"Anthropic call failed: {e}")
        return f"⚠️ Oracle unavailable: {str(e)}"


# ── QueryAgent ─────────────────────────────────────────────────────────────────

class QueryAgent:
    """
    Reactive query agent (Oracle 🔮).
    Called by slack_server.py to answer @Energy_assistant questions.

    Usage:
        agent  = QueryAgent()
        msg    = AgentMessage(sender="slack_server", receiver="query",
                              building="CAMPUS", stage="query",
                              payload={"question": "which building has worst MAPE?"})
        state  = PipelineState(building="CAMPUS")
        result = agent.run(msg, state)
        answer = result.payload["answer"]
    """

    def run(self, message: AgentMessage, state: PipelineState) -> AgentMessage:
        question = message.payload.get("question", "").strip()
        if not question:
            return self._reply(message, "No question provided.")

        log.info(f"QueryAgent received: '{question}'")

        # 1. Gather live data context from pipeline outputs
        data_context = _gather_data_context(question)

        # 2. Build system prompt with data injected
        system = NUS_SYSTEM_PROMPT.format(data_context=data_context)

        # 3. Call Oracle (Anthropic) 
        answer = _call_anthropic(system, question)

        log.info(f"QueryAgent answer: '{answer[:80]}...'")
        return self._reply(message, answer)

    def _reply(self, original: AgentMessage, answer: str) -> AgentMessage:
        return AgentMessage(
            sender="query",
            receiver=original.sender,
            building=original.building,
            stage="query_response",
            payload={"answer": answer},
        )
