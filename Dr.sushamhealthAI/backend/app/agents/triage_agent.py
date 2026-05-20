"""
Dr.SushamHealthAI — agents/triage_agent.py
TriageAgent: Publication-Grade Active Diagnostic Reasoning Agent.

NOVEL TECHNIQUE: Two-Pass Discriminative Diagnostic Questioning
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Unlike generic triage systems that ask the same "duration → severity → triggers"
for every condition, this agent implements a two-pass reasoning pipeline:

  PASS 1 — DIFFERENTIAL UPDATE (Bayesian Posterior Estimation):
    Given everything known so far, the LLM generates a running probability
    distribution over the top candidate conditions. Each round, the differential
    is updated based on the patient's new answer.

  PASS 2 — DISCRIMINATIVE QUESTION SELECTION (Information Gain Maximization):
    Given the current differential, the LLM identifies the ONE question that
    would MOST effectively differentiate between the top candidates. This is
    fundamentally different from asking generic questions — it targets the
    specific diagnostic uncertainty in THIS case.

EXAMPLE:
  Patient says "I have a headache"
  → Differential: Migraine (40%), Tension (35%), Cluster (15%), Sinusitis (10%)
  → Question: "Is the pain on one side or both sides?"
    (because one-sided → migraine/cluster, bilateral → tension/sinusitis)

  Patient answers "One side, right temple"
  → Updated Differential: Migraine (55%), Cluster (30%), Tension (10%), Sinusitis (5%)
  → Question: "Does light or sound make it worse?"
    (because photophobia/phonophobia → migraine, not cluster)

This mimics how expert clinicians actually think — maintaining and refining a
running differential, and asking questions that maximally reduce diagnostic uncertainty.

PUBLICATION TARGET: MICCAI / Lancet Digital Health
CATEGORY: Active Bayesian Diagnostic Reasoning via LLM
"""

import json
import re
from typing import Any, Dict, List, Optional

from app.core.logging_config import logger
from app.core.state import AgentState
from app.tools.llm_client import get_llm, invoke_with_fallback

MAX_TRIAGE_ROUNDS = 5  # More rounds for thorough clinical interview
CONFIDENCE_THRESHOLD = 75  # Stop early if top condition exceeds this probability


def _extract_json(text: str) -> Optional[Dict]:
    """Pull a JSON object out of LLM freeform text."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    # Try code block
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass
    # Try raw JSON
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


def _build_conversation_context(state: AgentState) -> str:
    """Build a comprehensive conversation context string."""
    history_lines = []
    for item in state.get("conversation_history", [])[-10:]:
        role = "Patient" if item.get("role") == "user" else "Doctor"
        history_lines.append(f"{role}: {item.get('content', '')}")
    return "\n".join(history_lines) if history_lines else "(first message)"


def _pass1_update_differential(
    question: str, collected: List[str], history_text: str,
    previous_differential: Optional[List[Dict]] = None,
) -> Optional[Dict]:
    """
    PASS 1: Bayesian Posterior Estimation.

    Given everything the patient has reported, generate/update the running
    probability distribution over candidate conditions.

    If a previous differential exists, the LLM updates probabilities based
    on the new information. Otherwise, it generates an initial differential.
    """
    collected_text = "\n".join(f"- {s}" for s in collected) if collected else "(none yet)"

    prev_diff_text = ""
    if previous_differential:
        prev_diff_text = "\n\nPREVIOUS DIFFERENTIAL DIAGNOSIS (update these based on the new answer):\n"
        for d in previous_differential:
            prev_diff_text += f"  - {d.get('condition', '?')}: {d.get('probability', '?')}% — {d.get('key_factors', '')}\n"

    prompt = f"""You are a senior clinical diagnostician performing Bayesian reasoning.

PATIENT INFORMATION GATHERED SO FAR:
{collected_text}

LATEST MESSAGE FROM PATIENT: "{question}"

CONVERSATION:
{history_text}
{prev_diff_text}

YOUR TASK:
Based on ALL available patient information, generate your current DIFFERENTIAL DIAGNOSIS.
Assign probability percentages that sum to approximately 100%.

Think carefully:
- Which conditions FIT the reported symptoms?
- Which conditions are RULED OUT or made less likely by what we know?
- What CRITICAL INFORMATION is still missing that would change these probabilities?

For each condition, also note the KEY DISTINGUISHING FEATURE — what single
finding would most strongly confirm or rule out this condition?

RESPOND WITH THIS EXACT JSON (no other text, no markdown):
{{
  "differential": [
    {{
      "condition": "Most likely condition name",
      "probability": 40,
      "key_factors": "What evidence supports this",
      "distinguishing_feature": "What finding would confirm/rule this out"
    }},
    {{
      "condition": "Second condition",
      "probability": 25,
      "key_factors": "Supporting evidence",
      "distinguishing_feature": "Confirmatory/exclusionary finding"
    }},
    {{
      "condition": "Third condition",
      "probability": 20,
      "key_factors": "Supporting evidence",
      "distinguishing_feature": "Confirmatory/exclusionary finding"
    }},
    {{
      "condition": "Fourth condition",
      "probability": 15,
      "key_factors": "Supporting evidence",
      "distinguishing_feature": "Confirmatory/exclusionary finding"
    }}
  ],
  "confidence_level": "low|moderate|high",
  "critical_unknowns": ["List of 2-3 most important unknown facts that would change this differential"]
}}

List 3-5 conditions. Be specific — name actual medical conditions, not vague categories.
Probabilities must be realistic and sum to approximately 100%."""

    # Use fast model for Pass 1 (differential update) to save tokens
    answer = invoke_with_fallback(prompt, fast=True)
    if answer:
        return _extract_json(answer)
    logger.error("Pass 1 (Differential Update) failed")
    return None


def _pass2_discriminative_question(
    differential: List[Dict], collected: List[str],
    history_text: str, triage_round: int, critical_unknowns: List[str],
) -> Optional[Dict]:
    """
    PASS 2: Discriminative Question Selection (Information Gain Maximization).

    Given the current differential diagnosis, select the ONE question that would
    MOST effectively differentiate between the top candidate conditions.

    This is the key innovation — instead of asking generic "how long" questions,
    we ask questions that specifically target the diagnostic uncertainty.
    """
    collected_text = "\n".join(f"- {s}" for s in collected) if collected else "(none)"

    diff_text = ""
    for d in differential:
        diff_text += f"  {d.get('probability', '?')}% — {d.get('condition', '?')}\n"
        diff_text += f"       Key factors: {d.get('key_factors', 'N/A')}\n"
        diff_text += f"       Distinguishing feature: {d.get('distinguishing_feature', 'N/A')}\n"

    unknowns_text = ", ".join(critical_unknowns) if critical_unknowns else "general clinical details"

    # Identify the top two conditions for targeted differentiation
    top_two = differential[:2] if len(differential) >= 2 else differential
    top_pair = " vs ".join([d.get("condition", "?") for d in top_two])

    prompt = f"""You are a master diagnostician performing TARGETED clinical questioning.

CURRENT DIFFERENTIAL DIAGNOSIS:
{diff_text}

CRITICAL UNKNOWNS: {unknowns_text}

INFORMATION ALREADY GATHERED:
{collected_text}

CONVERSATION SO FAR:
{history_text}

ROUND: {triage_round + 1} of {MAX_TRIAGE_ROUNDS}

YOUR TASK — DISCRIMINATIVE QUESTION SELECTION:
You need to ask the ONE question that would MOST EFFECTIVELY differentiate
between the top candidates: {top_pair}

Think about it this way:
- If the answer is "yes" → which conditions become MORE likely? Which become LESS likely?
- If the answer is "no" → how does that shift the probabilities?
- The BEST question is one where different answers point to DIFFERENT conditions.

EXAMPLE OF GOOD DISCRIMINATIVE QUESTIONING:
- Differential: Migraine vs Tension headache
  → "Is the pain on one side or both sides?" (one-sided → migraine, bilateral → tension)
- Differential: GERD vs Peptic ulcer
  → "Does the pain get worse or better after eating?" (worse after → GERD, better after → ulcer)
- Differential: Eczema vs Psoriasis
  → "Are the patches silvery/scaly or red/weeping?" (silvery → psoriasis, weeping → eczema)

BAD QUESTIONS (too generic, don't differentiate):
- "How long have you had this?" (doesn't help distinguish between candidates)
- "How severe is it?" (doesn't help narrow the differential)
- These are only useful if NO basic info exists yet.

URGENCY RULES:
- If ANY condition in the differential is potentially life-threatening → urgency = "urgent"
- If top condition has >60% probability → urgency = "routine" (we're narrowing, not screening)
- If probabilities are close (top two within 15%) → urgency = "important"

RULES:
1. Ask EXACTLY ONE question — the most discriminative one
2. Be warm, empathetic, and conversational
3. Never repeat what's been answered
4. Explain in "reasoning" which conditions you're trying to differentiate and HOW the answer will help

RESPOND WITH THIS EXACT JSON (no other text, no markdown):
{{
  "status": "needs_info",
  "question_text": "Your single, targeted follow-up question",
  "dimension": "ONSET|CHARACTER|LOCATION|SEVERITY|TIMING|MODIFYING_FACTORS|ASSOCIATED_SYMPTOMS|RED_FLAGS|HISTORY|MEDICATIONS",
  "reasoning": "I'm trying to differentiate [Condition A] from [Condition B]. If the answer is X → points to A; if Y → points to B.",
  "urgency": "routine|important|urgent",
  "target_conditions": ["{top_two[0].get('condition', '?')}", "{top_two[1].get('condition', '?') if len(top_two) > 1 else '?'}"],
  "expected_impact": "How the answer will shift the differential probabilities"
}}

OR if the differential is sufficiently confident (top condition >={CONFIDENCE_THRESHOLD}%):
{{
  "status": "ready_to_diagnose",
  "question_text": "",
  "dimension": "",
  "reasoning": "Top condition has high enough probability for assessment",
  "urgency": "routine"
}}"""

    answer = invoke_with_fallback(prompt, fast=False)
    if answer:
        return _extract_json(answer)
    logger.error("Pass 2 (Discriminative Question) failed")
    return None


def TriageAgent(state: AgentState) -> AgentState:
    """
    Two-Pass Active Diagnostic Reasoning Agent.

    Each round:
      1. Updates the running differential diagnosis (Bayesian posterior)
      2. Selects the most discriminative question (information gain maximization)
      3. Tracks the evolving differential for visualization

    Termination:
      - Max rounds reached
      - Top condition probability exceeds confidence threshold
      - LLM determines sufficient information gathered
    """

    if not state.get("is_medical_query", False):
        state["triage_status"] = "ready_to_diagnose"
        logger.info("Triage: non-medical query — skipping")
        return state

    if not get_llm():  # ensure at least one LLM is available
        state["triage_status"] = "ready_to_diagnose"
        return state

    question = state["question"]
    triage_round = state.get("triage_round", 0)
    collected = state.get("collected_symptoms", [])
    differential_history = state.get("differential_history", [])

    # Append current message to collected symptoms
    if question.strip():
        collected.append(question.strip())
        state["collected_symptoms"] = collected

    # Force diagnosis after MAX rounds
    if triage_round >= MAX_TRIAGE_ROUNDS:
        logger.info("Triage: max rounds (%d) reached — forcing diagnosis", MAX_TRIAGE_ROUNDS)
        state["triage_status"] = "ready_to_diagnose"
        state["question"] = _build_enriched_question(collected, differential_history)
        return state

    history_text = _build_conversation_context(state)

    # Get previous differential if it exists
    previous_differential = None
    if differential_history:
        previous_differential = differential_history[-1].get("differential", [])

    # ══════════════════════════════════════════════════════════════
    # PASS 1: Update the running differential diagnosis
    # ══════════════════════════════════════════════════════════════
    diff_result = _pass1_update_differential(
        question, collected, history_text, previous_differential,
    )

    current_differential = []
    critical_unknowns = []
    confidence_level = "low"

    if diff_result:
        current_differential = diff_result.get("differential", [])
        critical_unknowns = diff_result.get("critical_unknowns", [])
        confidence_level = diff_result.get("confidence_level", "low")

        # Save to differential history for tracking
        differential_history.append({
            "round": triage_round,
            "differential": current_differential,
            "confidence_level": confidence_level,
            "patient_input": question,
        })
        state["differential_history"] = differential_history

        # Check if top condition exceeds confidence threshold → auto-diagnose
        if current_differential:
            top_prob = current_differential[0].get("probability", 0)
            if top_prob >= CONFIDENCE_THRESHOLD and triage_round >= 2:
                logger.info(
                    "Triage: top condition '%s' at %d%% exceeds threshold — diagnosing",
                    current_differential[0].get("condition", "?"), top_prob,
                )
                state["triage_status"] = "ready_to_diagnose"
                state["question"] = _build_enriched_question(collected, differential_history)
                state["current_differential"] = current_differential
                return state

        logger.info(
            "Triage Pass 1: differential updated — top=%s (%d%%), confidence=%s",
            current_differential[0].get("condition", "?") if current_differential else "none",
            current_differential[0].get("probability", 0) if current_differential else 0,
            confidence_level,
        )
    else:
        logger.warning("Triage Pass 1 failed — falling back to single-pass mode")

    # ══════════════════════════════════════════════════════════════
    # PASS 2: Generate discriminative question
    # ══════════════════════════════════════════════════════════════
    if current_differential:
        question_result = _pass2_discriminative_question(
            current_differential, collected, history_text,
            triage_round, critical_unknowns,
        )
    else:
        # Fallback: if Pass 1 failed, do a simpler single-pass
        question_result = _fallback_single_pass(question, collected, history_text, triage_round)

    if not question_result:
        logger.warning("Triage: both passes failed — forcing diagnosis")
        state["triage_status"] = "ready_to_diagnose"
        state["question"] = _build_enriched_question(collected, differential_history)
        return state

    status = question_result.get("status", "ready_to_diagnose")
    question_text = question_result.get("question_text", "").strip()
    reasoning = question_result.get("reasoning", "")
    dimension = question_result.get("dimension", "GENERAL")
    urgency = question_result.get("urgency", "routine")
    target_conditions = question_result.get("target_conditions", [])
    expected_impact = question_result.get("expected_impact", "")

    if status == "needs_info" and question_text:
        state["triage_status"] = "needs_info"
        state["triage_round"] = triage_round + 1

        state["generation"] = f"I'd like to understand your condition better.\n\n**{question_text}**"
        state["source"] = "Clinical Triage"

        state["follow_up_data"] = {
            "questions": [{"text": question_text}],
            "triage_round": triage_round + 1,
            "max_rounds": MAX_TRIAGE_ROUNDS,
            "reasoning": reasoning,
            "dimension": dimension,
            "urgency": urgency,
            "target_conditions": target_conditions,
            "expected_impact": expected_impact,
            # Include current differential for frontend visualization
            "differential": [
                {
                    "condition": d.get("condition", ""),
                    "probability": d.get("probability", 0),
                }
                for d in current_differential[:4]
            ],
        }

        logger.info(
            "Triage Pass 2: round %d — dimension=%s, urgency=%s, targeting=[%s], "
            "asking: '%s'",
            triage_round + 1, dimension, urgency,
            " vs ".join(target_conditions[:2]),
            question_text[:60],
        )

    else:
        state["triage_status"] = "ready_to_diagnose"
        state["question"] = _build_enriched_question(collected, differential_history)
        state["current_differential"] = current_differential
        logger.info("Triage: sufficient info — proceeding to diagnosis")

    return state


def _fallback_single_pass(
    question: str, collected: List[str], history_text: str, triage_round: int,
) -> Optional[Dict]:
    """Fallback single-pass question generation if Pass 1 fails."""
    collected_text = "\n".join(f"- {s}" for s in collected) if collected else "(none)"

    prompt = f"""You are a clinical physician. Based on the patient's information:

PATIENT INFO: {collected_text}
LATEST: "{question}"
CONVERSATION: {history_text}
ROUND: {triage_round + 1}/{MAX_TRIAGE_ROUNDS}

Ask the ONE most important follow-up question. Be warm and specific.

RESPOND WITH JSON:
{{
  "status": "needs_info",
  "question_text": "Your question",
  "dimension": "ONSET|CHARACTER|LOCATION|SEVERITY|ASSOCIATED_SYMPTOMS|RED_FLAGS|HISTORY",
  "reasoning": "Why this question matters",
  "urgency": "routine|important|urgent"
}}"""

    answer = invoke_with_fallback(prompt, fast=True)
    if answer:
        return _extract_json(answer)
    logger.error("Fallback single-pass failed")
    return None


def _build_enriched_question(
    collected: List[str],
    differential_history: Optional[List[Dict]] = None,
) -> str:
    """
    Combine all collected patient statements AND the running differential
    into a single enriched query for the executor to generate a diagnosis.
    """
    if not collected:
        return ""
    if len(collected) == 1 and not differential_history:
        return collected[0]

    parts = [
        "The patient reported the following during clinical intake:",
    ]
    for i, symptom in enumerate(collected, 1):
        parts.append(f"  {i}. {symptom}")

    # Include the final differential if available
    if differential_history:
        latest = differential_history[-1]
        diff = latest.get("differential", [])
        if diff:
            parts.append("\nRUNNING DIFFERENTIAL DIAGNOSIS (Bayesian posterior after clinical interview):")
            for d in diff:
                parts.append(
                    f"  - {d.get('condition', '?')}: {d.get('probability', '?')}% "
                    f"({d.get('key_factors', '')})"
                )

    parts.append(
        "\nBased on ALL the information above AND the differential diagnosis, provide a "
        "comprehensive medical assessment including: final diagnosis ranking, recommended "
        "treatments, home remedies, when to seek urgent care, and preventive measures. "
        "Weight your assessment by the differential probabilities."
    )
    return "\n".join(parts)
