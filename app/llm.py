from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from app.config import Settings


logger = logging.getLogger(__name__)


class ChatModelClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def invoke(self, messages: list[dict[str, str]], json_response: bool = False) -> str:
        provider = self.settings.llm_provider.lower()
        if provider == "ollama":
            return self._invoke_ollama(messages, json_response=json_response)
        if provider == "openai":
            return self._invoke_openai(messages)
        raise ValueError(f"Unsupported LLM_PROVIDER={self.settings.llm_provider}")

    def configured(self) -> bool:
        provider = self.settings.llm_provider.lower()
        if provider == "ollama":
            return True
        if provider == "openai":
            return bool(self.settings.openai_api_key)
        return False

    def model_name(self) -> str:
        if self.settings.llm_provider.lower() == "ollama":
            return self.settings.ollama_model
        return self.settings.openai_model

    def _invoke_ollama(self, messages: list[dict[str, str]], json_response: bool = False) -> str:
        url = f"{self.settings.ollama_base_url.rstrip('/')}/api/chat"
        payload: dict[str, Any] = {
            "model": self.settings.ollama_model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": 0},
        }
        if json_response:
            payload["format"] = "json"
        response = httpx.post(url, json=payload, timeout=self.settings.llm_timeout_seconds)
        response.raise_for_status()
        data = response.json()
        return str(data.get("message", {}).get("content", ""))

    def _invoke_openai(self, messages: list[dict[str, str]]) -> str:
        from langchain_openai import ChatOpenAI

        llm = ChatOpenAI(
            model=self.settings.openai_model,
            api_key=self.settings.openai_api_key,
            temperature=0,
            max_retries=0,
        )
        response = llm.invoke([(message["role"], message["content"]) for message in messages])
        return str(getattr(response, "content", "") or "")


def strip_thinking(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()


def parse_json_object(text: str) -> dict[str, Any]:
    cleaned = strip_thinking(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(cleaned[start : end + 1])


class CarrierNameNormalizer:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = ChatModelClient(settings)

    def normalize(
        self,
        input_carrier_name: str,
        carriers: list[dict[str, str]],
        best_fuzzy_score: float,
    ) -> dict[str, Any] | None:
        if not self.settings.enable_llm_carrier_normalization or not self.client.configured():
            logger.info(
                "llm_carrier_normalization_skipped",
                extra={
                    "input_carrier_name": input_carrier_name,
                    "reason": "disabled_or_provider_not_configured",
                    "llm_provider": self.settings.llm_provider,
                    "enable_llm_carrier_normalization": self.settings.enable_llm_carrier_normalization,
                    "best_fuzzy_score": round(best_fuzzy_score, 3),
                },
            )
            return None

        try:
            logger.info(
                "llm_carrier_normalization_started",
                extra={
                    "input_carrier_name": input_carrier_name,
                    "candidate_carrier_ids": [carrier["id"] for carrier in carriers],
                    "llm_provider": self.settings.llm_provider,
                    "model": self.client.model_name(),
                },
            )
            content = self.client.invoke(
                [
                    {
                        "role": "system",
                        "content": "You normalize messy logistics carrier names. Choose only from the provided carriers. "
                        "Return strict JSON with keys: carrier_id, confidence, reason. "
                        "If no carrier is likely, use carrier_id null and confidence 0. "
                        "Do not include markdown or prose outside JSON.",
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "input_carrier_name": input_carrier_name,
                                "candidate_carriers": carriers,
                            },
                            ensure_ascii=True,
                        ),
                    },
                ],
                json_response=True,
            )
            parsed = parse_json_object(content)
            carrier_ids = {carrier["id"] for carrier in carriers}
            carrier_id = parsed.get("carrier_id")
            confidence = float(parsed.get("confidence") or 0)
            if carrier_id in carrier_ids and confidence >= 0.75:
                logger.info(
                    "llm_carrier_normalization_completed",
                    extra={
                        "input_carrier_name": input_carrier_name,
                        "carrier_id": carrier_id,
                        "confidence": round(confidence, 3),
                        "reason": parsed.get("reason"),
                    },
                )
                return {"carrier_id": carrier_id, "confidence": confidence, "reason": parsed.get("reason")}
            logger.info(
                "llm_carrier_normalization_no_match",
                extra={
                    "input_carrier_name": input_carrier_name,
                    "carrier_id": carrier_id,
                    "confidence": round(confidence, 3),
                    "reason": parsed.get("reason"),
                },
            )
            return None
        except Exception as exc:
            logger.warning(
                "llm_carrier_normalization_failed",
                extra={
                    "input_carrier_name": input_carrier_name,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            return None


class ExplanationService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = ChatModelClient(settings)

    def explain(self, bill: dict[str, Any], decision: str, confidence: float, evidence: dict[str, Any]) -> str:
        fallback = self._fallback_explanation(bill, decision, confidence, evidence)
        if not self.settings.enable_llm_explanations or not self.client.configured():
            logger.info(
                "llm_explanation_skipped",
                extra={
                    "bill_id": bill.get("id"),
                    "reason": "disabled_or_provider_not_configured",
                    "llm_provider": self.settings.llm_provider,
                    "enable_llm_explanations": self.settings.enable_llm_explanations,
                },
            )
            return fallback

        try:
            logger.info(
                "llm_explanation_started",
                extra={
                    "bill_id": bill.get("id"),
                    "llm_provider": self.settings.llm_provider,
                    "model": self.client.model_name(),
                },
            )
            content = self.client.invoke(
                [
                    {
                        "role": "system",
                        "content": "You write concise freight audit explanations for logistics operations reviewers. "
                        "Do not invent facts; use only the supplied evidence.",
                    },
                    {
                        "role": "user",
                        "content": f"Freight bill: {bill}\nDecision: {decision}\nConfidence: {confidence}\nEvidence: {evidence}\n"
                        "Write 2-3 plain-English sentences.",
                    },
                ]
            )
            content = strip_thinking(content)
            explanation = content.strip() if content.strip() else fallback
            logger.info("llm_explanation_completed", extra={"bill_id": bill.get("id")})
            return explanation
        except Exception as exc:
            logger.warning(
                "llm_explanation_failed_using_fallback",
                extra={"bill_id": bill.get("id"), "error_type": type(exc).__name__, "error": str(exc)},
            )
            return fallback

    def _fallback_explanation(
        self,
        bill: dict[str, Any],
        decision: str,
        confidence: float,
        evidence: dict[str, Any],
    ) -> str:
        validations = evidence.get("validations", [])
        failed = [item["message"] for item in validations if item.get("severity") in {"error", "critical"}]
        warnings = [item["message"] for item in validations if item.get("severity") == "warning"]
        selected = evidence.get("selected_contract") or {}
        contract_text = f" Contract {selected.get('id')} was selected." if selected else ""
        if failed:
            reason = failed[0]
        elif warnings:
            reason = warnings[0]
        else:
            reason = "The carrier, contract, shipment, route, weight, and charges matched within tolerance."
        return f"{decision.replace('_', ' ').title()} at {confidence:.2f} confidence.{contract_text} {reason}"
