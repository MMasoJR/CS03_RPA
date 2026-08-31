"""
src/automations/rules_engine.py

CS03 - Pilar 2: Automacao do Ciclo de Eventos
-----------------------------------------------
Recebe os AnomalySignal gerados pelo AnomalyDetector (src/intelligence),
cruza com o contexto tecnico externo coletado pelo scraper WEG W22
(src/scraper), decide a severidade, registra o evento no log historico e
atualiza o status do ativo — tudo sem intervencao manual, no mesmo
formato CSV usado pelo resto do pipeline (DataLoader).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

from src.intelligence.anomaly_model import AnomalySignal
from src.transformer.normalizer import (
    TEMP_WARNING_C, TEMP_CRITICAL_C,
    VIB_WARNING_MM_S, VIB_CRITICAL_MM_S,
    LOAD_WARNING_PCT, LOAD_CRITICAL_PCT,
)
from src.utils.logger import get_logger

logger = get_logger("rules_engine")

TECHNICAL_CONTEXT_PATH = os.path.join("data", "weg_w22_technical_context.json")


class Severity(str, Enum):
    INFO = "INFO"
    ALERTA = "ALERTA"
    CRITICO = "CRITICO"


class AssetStatus(str, Enum):
    OPERATIONAL = "OPERATIONAL"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


_SEVERITY_TO_STATUS = {
    Severity.INFO: AssetStatus.OPERATIONAL,
    Severity.ALERTA: AssetStatus.WARNING,
    Severity.CRITICO: AssetStatus.CRITICAL,
}


@dataclass
class AssetEvent:
    asset_tag: str
    event_type: str          # ANOMALY_DETECTED | STATUS_CHANGE
    severity: str
    metric: str
    metric_value: float
    threshold_used: Optional[float]
    anomaly_score: float
    suggested_action: Optional[str]
    created_at: str


def load_technical_context() -> dict:
    if not os.path.exists(TECHNICAL_CONTEXT_PATH):
        logger.warning(
            f"[RulesEngine] {TECHNICAL_CONTEXT_PATH} nao encontrado — "
            "rode o scraper (src/scraper/weg_catalog_scraper.py) antes do pipeline. "
            "Usando limiares normativos padrao como fallback."
        )
        return {
            "temperature_tolerance_c": {"warning_c": 90.0, "critical_c": 105.0},
            "troubleshooting_guide": [],
        }
    with open(TECHNICAL_CONTEXT_PATH, encoding="utf-8") as fh:
        return json.load(fh)


class RulesEngine:
    def __init__(self, technical_context: Optional[dict] = None) -> None:
        self.technical_context = technical_context or load_technical_context()

    def process(self, signal: AnomalySignal) -> AssetEvent:
        severity, threshold = self._classify_severity(signal)
        suggested_action = self._suggest_action(signal, severity)

        event = AssetEvent(
            asset_tag=signal.asset_tag,
            event_type="ANOMALY_DETECTED" if signal.is_anomaly else "STATUS_CHANGE",
            severity=severity.value,
            metric=signal.metric,
            metric_value=signal.value,
            threshold_used=threshold,
            anomaly_score=signal.anomaly_score,
            suggested_action=suggested_action,
            created_at=datetime.utcnow().isoformat(),
        )
        logger.info(
            f"[RulesEngine] {signal.asset_tag} | metric={signal.metric} "
            f"valor={signal.value:.2f} severidade={severity.value} "
            f"status={_SEVERITY_TO_STATUS[severity].value}"
        )
        return event

    def process_batch(self, signals: list[AnomalySignal]) -> list[AssetEvent]:
        return [self.process(s) for s in signals]

    @staticmethod
    def status_for(event: AssetEvent) -> str:
        return _SEVERITY_TO_STATUS[Severity(event.severity)].value

    # -- regras de negocio ---------------------------------------------
    def _classify_severity(self, signal: AnomalySignal) -> tuple[Severity, Optional[float]]:
        if not signal.is_anomaly:
            return Severity.INFO, None

        if signal.metric == "temperature":
            # Usa o limite real da classe de isolamento (scraped), não um
            # número fixo arbitrário.
            limits = self.technical_context.get("temperature_tolerance_c", {})
            warning = limits.get("warning_c", 90.0)
            critical = limits.get("critical_c", 105.0)
            if signal.value >= critical:
                return Severity.CRITICO, critical
            if signal.value >= warning:
                return Severity.ALERTA, warning
            return Severity.INFO, warning

        if signal.metric == "vibration":
            if signal.value > VIB_CRITICAL_MM_S:
                return Severity.CRITICO, VIB_CRITICAL_MM_S
            if signal.value > VIB_WARNING_MM_S:
                return Severity.ALERTA, VIB_WARNING_MM_S

        elif signal.metric == "current_load":
            if signal.value > LOAD_CRITICAL_PCT:
                return Severity.CRITICO, LOAD_CRITICAL_PCT
            if signal.value > LOAD_WARNING_PCT:
                return Severity.ALERTA, LOAD_WARNING_PCT

        elif signal.metric == "rpm":
            # signal.value aqui é o desvio percentual, não o RPM absoluto
            if abs(signal.value) > 10.0:
                return Severity.CRITICO, 10.0
            if abs(signal.value) > 5.0:
                return Severity.ALERTA, 5.0

        elif signal.metric == "power_factor":
            if signal.value < 0.50:
                return Severity.CRITICO, 0.50
            if signal.value < 0.80:
                return Severity.ALERTA, 0.80

        # Chegou aqui como anomalia (is_anomaly=True) mas nenhuma métrica
        # isolada cruzou seu próprio limiar (combinação de pequenos
        # desvios) — usa o anomaly_score como critério de desempate.
        if signal.anomaly_score >= 0.30:
            return Severity.CRITICO, None
        if signal.anomaly_score >= 0.10:
            return Severity.ALERTA, None
        return Severity.INFO, None

    def _suggest_action(self, signal: AnomalySignal, severity: Severity) -> Optional[str]:
        if severity == Severity.INFO:
            return None
        for entry in self.technical_context.get("troubleshooting_guide", []):
            if entry.get("metric") == signal.metric:
                return f"{entry.get('action')} (causa provavel: {entry.get('likely_cause')})"
        return "Inspecionar ativo e abrir ordem de manutencao preventiva"
