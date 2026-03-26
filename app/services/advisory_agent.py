# app/services/advisory_agent.py

import logging
import base64
import httpx
from typing import TypedDict, Optional
from langgraph.graph import StateGraph, END
from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, SystemMessage
from app.core.config import settings

logger = logging.getLogger(__name__)

from dotenv import load_dotenv
load_dotenv()

# ── State schema ──────────────────────────────────────────────
class AdvisoryState(TypedDict):
    request_id: str
    farmer_phone: str
    query_text: Optional[str]
    image_url: Optional[str]
    image_b64: Optional[str]          # base64-encoded image bytes (populated by fetch node)
    image_media_type: Optional[str]   # e.g. "image/jpeg"
    # Farmer & field context — injected before graph runs
    farmer_name: Optional[str]
    crop_type: Optional[str]
    area_ha: Optional[float]
    soil_type: Optional[str]
    irrigation_method: Optional[str]
    district: Optional[str]
    ndvi_score: Optional[float]
    # Computed
    disease_hint: Optional[str]
    vision_diagnosis: Optional[dict]  # {disease, confidence, severity, description}
    advisory_draft: Optional[str]
    final_response: Optional[str]
    error: Optional[str]

# ── LLM client ────────────────────────────────────────────────
llm = init_chat_model("google_genai:gemini-2.5-flash")


# ── Node 0: fetch image bytes ─────────────────────────────────
def fetch_image_bytes(state: AdvisoryState) -> AdvisoryState:
    """
    If the message has an image_url (WhatsApp Graph API URL),
    fetch the actual bytes and base64-encode them for vision analysis.
    """
    image_url = state.get("image_url")
    if not image_url:
        return state

    # If already base64 (passed directly), skip
    if state.get("image_b64"):
        return state

    try:
        headers = {}
        # WhatsApp Graph API images require auth token
        if "graph.facebook.com" in image_url and settings.WHATSAPP_TOKEN:
            # First call gets the actual CDN URL
            with httpx.Client(timeout=15.0) as client:
                meta_resp = client.get(
                    image_url,
                    headers={"Authorization": f"Bearer {settings.WHATSAPP_TOKEN}"},
                )
                meta_resp.raise_for_status()
                meta = meta_resp.json()
                cdn_url = meta.get("url", image_url)

                # Second call downloads the image
                img_resp = client.get(
                    cdn_url,
                    headers={"Authorization": f"Bearer {settings.WHATSAPP_TOKEN}"},
                )
                img_resp.raise_for_status()
                img_bytes = img_resp.content
                content_type = img_resp.headers.get("content-type", "image/jpeg")
        else:
            # Direct URL (e.g. from tests or S3)
            with httpx.Client(timeout=15.0) as client:
                img_resp = client.get(image_url)
                img_resp.raise_for_status()
                img_bytes = img_resp.content
                content_type = img_resp.headers.get("content-type", "image/jpeg")

        b64 = base64.b64encode(img_bytes).decode("utf-8")
        # Normalise content-type
        if "jpeg" in content_type or "jpg" in content_type:
            media_type = "image/jpeg"
        elif "png" in content_type:
            media_type = "image/png"
        elif "webp" in content_type:
            media_type = "image/webp"
        else:
            media_type = "image/jpeg"

        logger.info(f"Image fetched for request {state['request_id']}: {len(img_bytes)} bytes")
        return {**state, "image_b64": b64, "image_media_type": media_type}

    except Exception as e:
        logger.error(f"Image fetch failed for {state['request_id']}: {e}")
        # Don't fail the whole pipeline — fall back to text-only mode
        return {**state, "image_b64": None, "image_media_type": None}


# ── Node 1: vision disease analysis ──────────────────────────
def analyze_image_for_disease(state: AdvisoryState) -> AdvisoryState:
    """
    Use Gemini vision to diagnose crop disease from image.
    Only runs if image_b64 is present.
    """
    if not state.get("image_b64"):
        logger.info(f"No image for {state['request_id']}, skipping vision analysis")
        return state

    try:
        crop_context = state.get("crop_type") or "unknown crop"

        vision_prompt = f"""You are an expert plant pathologist AI specializing in crops grown in Punjab, Pakistan.

Analyze this crop image carefully and provide a diagnosis.

Crop type (if known): {crop_context}

Respond ONLY with a valid JSON object in this exact format (no markdown, no extra text):
{{
  "has_disease": true/false,
  "disease_name": "name or null if healthy",
  "confidence": 0.0-1.0,
  "severity": "none/mild/moderate/severe",
  "symptoms_observed": ["list", "of", "visible", "symptoms"],
  "affected_area_pct": 0-100,
  "description": "1-2 sentence description of what you see",
  "recommended_action": "immediate action in one sentence"
}}

If the image is not a crop/plant, set has_disease to false and explain in description."""

        messages = [
            HumanMessage(content=[
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{state['image_media_type']};base64,{state['image_b64']}"
                    }
                },
                {
                    "type": "text",
                    "text": vision_prompt
                }
            ])
        ]

        response = llm.invoke(messages)
        raw = response.content.strip()

        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        import json
        diagnosis = json.loads(raw)

        logger.info(
            f"Vision diagnosis for {state['request_id']}: "
            f"{diagnosis.get('disease_name')} (conf={diagnosis.get('confidence')})"
        )
        return {**state, "vision_diagnosis": diagnosis}

    except Exception as e:
        logger.error(f"Vision analysis failed for {state['request_id']}: {e}")
        return {**state, "vision_diagnosis": None}


# ── Node 2: extract disease hint (text-based fallback) ────────
def extract_disease_hint(state: AdvisoryState) -> AdvisoryState:
    """
    If we already have a vision diagnosis, use that.
    Otherwise fall back to keyword matching on the query text.
    """
    # If vision already found something, use it as the hint
    if state.get("vision_diagnosis") and state["vision_diagnosis"].get("has_disease"):
        diag = state["vision_diagnosis"]
        hint = f"{diag['disease_name']} (confidence: {diag['confidence']:.0%}, severity: {diag['severity']})"
        return {**state, "disease_hint": hint}

    # Text-based keyword fallback
    query = state.get("query_text", "") or ""
    keywords = {
        "yellow":   "nitrogen deficiency or waterlogging",
        "spots":    "fungal disease",
        "wilt":     "bacterial wilt or root rot",
        "dry":      "drought stress",
        "insects":  "pest infestation",
        "aphid":    "aphid infestation",
        "rust":     "wheat rust fungal disease",
        "blight":   "blight disease",
        "curl":     "leaf curl virus",
        "white":    "powdery mildew",
    }
    hint = None
    for word, diagnosis in keywords.items():
        if word in query.lower():
            hint = diagnosis
            break

    logger.info(f"Disease hint: {hint}")
    return {**state, "disease_hint": hint}


# ── Node 3: generate advisory ─────────────────────────────────
def generate_advisory(state: AdvisoryState) -> AdvisoryState:
    try:
        # Build disease context — vision result takes priority
        vision = state.get("vision_diagnosis")
        if vision and vision.get("has_disease"):
            symptoms = ", ".join(vision.get("symptoms_observed", []))
            disease_context = f"""VISION AI DIAGNOSIS (from farmer's photo):
- Disease: {vision['disease_name']}
- Confidence: {vision['confidence']:.0%}
- Severity: {vision['severity']}
- Affected area: ~{vision.get('affected_area_pct', '?')}%
- Symptoms observed: {symptoms}
- AI observation: {vision['description']}"""
        elif vision and not vision.get("has_disease"):
            disease_context = f"IMAGE ANALYSIS: No disease detected. {vision.get('description', 'Crop appears healthy.')}"
        elif state.get("disease_hint"):
            disease_context = f"Text-based hint: {state['disease_hint']}"
        else:
            disease_context = "No specific pattern — provide general crop health guidance."

        ndvi_context = "Not available"
        if state.get("ndvi_score") is not None:
            score = state["ndvi_score"]
            if score >= 0.6:
                ndvi_context = f"{score:.2f} — Crop health is GOOD"
            elif score >= 0.4:
                ndvi_context = f"{score:.2f} — Crop health is MODERATE, monitor closely"
            else:
                ndvi_context = f"{score:.2f} — Crop health is POOR, urgent attention needed"

        has_image = bool(state.get("image_b64") or state.get("image_url"))

        system_prompt = f"""You are an expert agricultural advisor for smallholder farmers in Punjab, Pakistan.
Always respond in BOTH Urdu and English — Urdu first, then English.
Be specific and actionable. Use product names available in Pakistan with exact dosages.
{'The farmer sent a PHOTO of their crop — your diagnosis is based on visual AI analysis.' if has_image else ''}

FARMER PROFILE:
- Name: {state.get("farmer_name") or "Unknown"}
- District: {state.get("district") or "Punjab, Pakistan"}
- Crop: {state.get("crop_type") or "Unknown"}
- Field size: {state.get("area_ha") or "Unknown"} hectares
- Soil type: {state.get("soil_type") or "Unknown"}
- Irrigation: {state.get("irrigation_method") or "Unknown"}
- Field health (NDVI): {ndvi_context}

DISEASE ANALYSIS:
{disease_context}

Structure your response EXACTLY like this:

**تشخیص:** (one sentence)
**فوری اقدام:**
- (bullet 1)
- (bullet 2)
**علاج:** (product name, dosage, timing in Urdu)
**احتیاط:** (one prevention tip)

---

**Diagnosis:** (one sentence)
**Immediate Action:**
- (bullet 1)
- (bullet 2)
**Treatment:** (specific product, dosage, timing)
**Prevention:** (one tip for next season)
**Urgency:** LOW / MEDIUM / HIGH"""

        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=f"Farmer's message: {state.get('query_text') or 'Please analyze my crop photo'}"),
        ]

        response = llm.invoke(messages)
        logger.info(f"Advisory generated for {state['request_id']}")
        return {**state, "advisory_draft": response.content, "error": None}

    except Exception as e:
        logger.error(f"LLM failed: {e}")
        return {**state, "error": str(e)}


# ── Node 4: fallback ──────────────────────────────────────────
def fallback_advisory(state: AdvisoryState) -> AdvisoryState:
    logger.warning(f"Fallback used for {state['request_id']}")
    return {**state, "advisory_draft": (
        "معذرت، ابھی ہمارا سسٹم مصروف ہے۔ براہ کرم دوبارہ کوشش کریں۔\n\n"
        "Sorry, the advisory system is temporarily unavailable. "
        "Please try again in a few minutes."
    ), "error": None}


# ── Node 5: finalize ──────────────────────────────────────────
def finalize_response(state: AdvisoryState) -> AdvisoryState:
    return {**state, "final_response": state.get("advisory_draft")}


# ── Routing ───────────────────────────────────────────────────
def route_after_generation(state: AdvisoryState) -> str:
    return "fallback_advisory" if state.get("error") else "finalize_response"


# ── Build graph ───────────────────────────────────────────────
def build_advisory_graph():
    graph = StateGraph(AdvisoryState)

    graph.add_node("fetch_image_bytes",         fetch_image_bytes)
    graph.add_node("analyze_image_for_disease", analyze_image_for_disease)
    graph.add_node("extract_disease_hint",      extract_disease_hint)
    graph.add_node("generate_advisory",         generate_advisory)
    graph.add_node("fallback_advisory",         fallback_advisory)
    graph.add_node("finalize_response",         finalize_response)

    graph.set_entry_point("fetch_image_bytes")
    graph.add_edge("fetch_image_bytes",         "analyze_image_for_disease")
    graph.add_edge("analyze_image_for_disease", "extract_disease_hint")
    graph.add_edge("extract_disease_hint",      "generate_advisory")
    graph.add_conditional_edges("generate_advisory", route_after_generation)
    graph.add_edge("fallback_advisory",  "finalize_response")
    graph.add_edge("finalize_response",  END)
    return graph.compile()

advisory_graph = build_advisory_graph()


# ── Public function ───────────────────────────────────────────
def run_advisory_pipeline(
    request_id: str,
    farmer_phone: str,
    query_text: str | None,
    image_url: str | None,
    image_b64: str | None = None,
    image_media_type: str | None = None,
    # real context from DB
    farmer_name: str | None = None,
    crop_type: str | None = None,
    area_ha: float | None = None,
    soil_type: str | None = None,
    irrigation_method: str | None = None,
    district: str | None = None,
    ndvi_score: float | None = None,
) -> str:
    result = advisory_graph.invoke({
        "request_id": request_id,
        "farmer_phone": farmer_phone,
        "query_text": query_text,
        "image_url": image_url,
        "image_b64": image_b64,
        "image_media_type": image_media_type,
        "farmer_name": farmer_name,
        "crop_type": crop_type,
        "area_ha": area_ha,
        "soil_type": soil_type,
        "irrigation_method": irrigation_method,
        "district": district,
        "ndvi_score": ndvi_score,
        "disease_hint": None,
        "vision_diagnosis": None,
        "advisory_draft": None,
        "final_response": None,
        "error": None,
    })
    return result.get("final_response", "No response generated")