"""
llm_explainer.py
Novel contribution: LLM-powered natural language interface to the DTN.

Two functions:
  explain_anomaly(anomaly)      → plain-English explanation for operators
  what_if_query(question, store) → answer operator "what if" questions

Uses the Anthropic API. Set ANTHROPIC_API_KEY in your environment.
Falls back to a rule-based template if no API key is set (useful for testing).
"""

import os
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from data_store import Anomaly, DataStore

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")


def _call_claude(prompt: str) -> str:
    """Call Claude API. Returns response text or raises on failure."""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        return f"[API error: {e}]"


def _rule_based_explanation(anomaly: "Anomaly") -> str:
    """Fallback when no API key is available."""
    if anomaly.sinr_db < -20:
        cause = "severe interference or the serving base station may be overloaded / out of range"
        action = "Consider triggering a handoff to a macro cell or reducing interference from neighbouring cells."
    elif anomaly.sinr_db < 0:
        cause = "moderate interference — possibly a UE at the cell edge or near an NLOS obstruction"
        action = "A beam-steering adjustment or handoff to a less congested cell may help."
    else:
        cause = "an unusual drop from this UE's recent baseline"
        action = "Monitor for the next 2–3 ticks. If it persists, check for new obstructions or interference."

    return (
        f"UE {anomaly.ue_id} connected to {anomaly.bs_id} is experiencing poor signal quality "
        f"(SINR: {anomaly.sinr_db:.1f} dB, RSRP: {anomaly.rsrp_dbm:.1f} dBm, "
        f"throughput: {anomaly.throughput_mbps:.1f} Mbps). "
        f"Likely cause: {cause}. {action}"
    )


def explain_anomaly(anomaly: "Anomaly") -> str:
    """
    Given an anomaly, return a plain-English explanation suitable
    for a network operator who doesn't read raw dB values.
    """
    if not ANTHROPIC_API_KEY:
        return _rule_based_explanation(anomaly)

    prompt = f"""You are an expert 6G network operations assistant embedded in a Digital Twin Network system.
A network anomaly has been automatically detected. Explain it clearly to a non-expert network operator in 3–4 sentences.
Be specific about the numbers. Suggest one concrete action.

Anomaly details:
- UE (device): {anomaly.ue_id}
- Serving base station: {anomaly.bs_id}
- SINR: {anomaly.sinr_db:.1f} dB  (good = >10 dB, poor = <0 dB)
- RSRP: {anomaly.rsrp_dbm:.1f} dBm  (good = >-80 dBm, poor = <-100 dBm)
- Throughput: {anomaly.throughput_mbps:.1f} Mbps
- Latency: {anomaly.latency_ms:.1f} ms
- Detected reason: {anomaly.reason}

Write your explanation in plain language. No bullet points. No jargon unless explained."""

    return _call_claude(prompt)


def what_if_query(question: str, store: "DataStore") -> str:
    """
    Answer a natural-language what-if question from the operator
    using the current network state as context.

    Example questions:
      "What happens if BS_MAC_0 fails?"
      "Which UEs are most at risk right now?"
      "What would improve throughput in the north-east sector?"
    """
    summary   = store.kpi_summary()
    per_bs    = store.per_bs_summary()
    anomalies = store.recent_anomalies(10)

    context = {
        "network_kpis": summary,
        "per_base_station": per_bs,
        "recent_anomalies": [
            {
                "ue_id": a.ue_id,
                "bs_id": a.bs_id,
                "sinr_db": a.sinr_db,
                "throughput_mbps": a.throughput_mbps,
                "reason": a.reason,
            }
            for a in anomalies
        ],
    }

    if not ANTHROPIC_API_KEY:
        return (
            f"[LLM unavailable — set ANTHROPIC_API_KEY to enable this feature]\n\n"
            f"Current network state: {summary.get('num_ues')} UEs, "
            f"avg SINR {summary.get('avg_sinr_db')} dB, "
            f"{summary.get('total_anomalies')} anomalies detected.\n\n"
            f"Your question: '{question}'"
        )

    prompt = f"""You are an AI assistant embedded in a 6G Digital Twin Network.
The operator has asked a what-if question. Answer it in 4–6 sentences using the live network state below.
Be specific. Reference actual base stations and UE counts where relevant.

Operator question: "{question}"

Live network state (JSON):
{json.dumps(context, indent=2)}

Answer as if you are advising the operator in a real network operations centre."""

    return _call_claude(prompt)
