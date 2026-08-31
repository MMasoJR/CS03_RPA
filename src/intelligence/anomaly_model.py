"""
src/intelligence/anomaly_model.py

CS03 - Modelo de ML/DL de deteccao de anomalias
--------------------------------------------------
Ainda nao existia um modelo de ML plugado ao pipeline (o health_score da
CS02 e uma formula de penalidades, nao um modelo treinado). Este modulo
adiciona a camada de ML exigida pela CS03: um Isolation Forest treinado
no historico de data/processed_readings.csv, que aprende o padrao normal
multivariado de cada leitura (temperatura, vibracao, carga, desvio de
RPM, fator de potencia, health_score) e sinaliza combinacoes anomalas que
os limiares fixos do validator podem nao capturar isoladamente.

Se ainda nao houver historico suficiente (pipeline recem-iniciado), cai
em um fallback baseado em regra (health_score < 60 => anomalia), para o
pipeline nunca ficar sem sinal de anomalia disponivel.

O modelo e persistido em data/models/anomaly_model.joblib e retreinado
sob demanda (chame retrain() periodicamente, ex: 1x/dia, a partir do
orquestrador ou de um job separado).
"""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from typing import Optional

import joblib
from sklearn.ensemble import IsolationForest

from src.transformer.normalizer import (
    NormalizedReading,
    TEMP_WARNING_C, TEMP_CRITICAL_C,
    VIB_WARNING_MM_S, VIB_CRITICAL_MM_S,
    LOAD_WARNING_PCT, LOAD_CRITICAL_PCT,
)
from src.utils.logger import get_logger

logger = get_logger("anomaly_model")

MODEL_DIR = os.path.join("data", "models")
MODEL_PATH = os.path.join(MODEL_DIR, "anomaly_model.joblib")
PROCESSED_CSV = os.path.join("data", "processed_readings.csv")

FEATURES = [
    "temperature_c",
    "vibration_mm_s",
    "current_load_pct",
    "rpm_deviation_pct",
    "power_factor",
    "health_score",
]

MIN_SAMPLES_TO_TRAIN = 30
# O health_score do normalizer.py tem um teto de penalidade por métrica
# (ex: no máximo 40 pontos só por temperatura crítica), então uma leitura
# com UMA métrica isolada fora do padrão raramente cai abaixo de 60 —
# health_score=60 é o próprio piso típico de uma temperatura crítica
# isolada. 90 captura de forma confiável qualquer anomalia de métrica
# única, mantendo leituras normais (tipicamente ~95-100) de fora.
RULE_FALLBACK_HEALTH_THRESHOLD = 90.0


@dataclass
class AnomalySignal:
    """Formato de saida consumido pelo motor de regras (Pilar 2)."""

    asset_tag: str
    metric: str
    value: float
    is_anomaly: bool
    anomaly_score: float
    health_score: Optional[float] = None
    source: str = "isolation_forest"  # ou "rule_fallback"


class AnomalyDetector:
    def __init__(self) -> None:
        os.makedirs(MODEL_DIR, exist_ok=True)
        self.model: Optional[IsolationForest] = self._load_model()

    def _load_model(self) -> Optional[IsolationForest]:
        if os.path.exists(MODEL_PATH):
            try:
                model = joblib.load(MODEL_PATH)
                logger.info("[AnomalyModel] Modelo carregado de disco")
                return model
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[AnomalyModel] Falha ao carregar modelo salvo: {exc}")
        return None

    def retrain(self) -> bool:
        """Retreina o Isolation Forest com o historico atual de leituras
        processadas. Retorna True se retreinou, False se ainda nao ha
        historico suficiente."""
        rows = self._load_training_data()
        if len(rows) < MIN_SAMPLES_TO_TRAIN:
            logger.info(
                f"[AnomalyModel] Apenas {len(rows)} amostras disponiveis "
                f"(< {MIN_SAMPLES_TO_TRAIN}); mantendo fallback baseado em regra"
            )
            return False

        X = [[row[f] for f in FEATURES] for row in rows]
        model = IsolationForest(
            n_estimators=150,
            contamination="auto",
            random_state=42,
        )
        model.fit(X)
        joblib.dump(model, MODEL_PATH)
        self.model = model
        logger.info(f"[AnomalyModel] Modelo retreinado com {len(rows)} amostras e salvo em {MODEL_PATH}")
        return True

    def _load_training_data(self) -> list[dict]:
        if not os.path.exists(PROCESSED_CSV):
            return []
        rows = []
        with open(PROCESSED_CSV, encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for line in reader:
                try:
                    rows.append({f: float(line[f]) for f in FEATURES})
                except (KeyError, ValueError):
                    continue
        return rows

    def predict(self, reading: NormalizedReading) -> AnomalySignal:
        """Classifica uma leitura normalizada como anomala ou nao, e
        identifica a metrica mais provavelmente responsavel (para o motor
        de regras decidir a acao sugerida)."""
        dominant_metric, dominant_value = self._dominant_metric(reading)

        if self.model is not None:
            x = [[getattr(reading, f) for f in FEATURES]]
            raw_score = self.model.decision_function(x)[0]     # >0 normal, <0 anomalo
            is_anomaly = bool(self.model.predict(x)[0] == -1)
            # normaliza score bruto do IsolationForest para 0-1 (quanto maior, mais anomalo)
            anomaly_score = round(max(0.0, min(1.0, 0.5 - raw_score)), 3)
            source = "isolation_forest"
        else:
            is_anomaly = reading.health_score < RULE_FALLBACK_HEALTH_THRESHOLD
            anomaly_score = round(max(0.0, (RULE_FALLBACK_HEALTH_THRESHOLD - reading.health_score) / RULE_FALLBACK_HEALTH_THRESHOLD), 3)
            source = "rule_fallback"

        return AnomalySignal(
            asset_tag=reading.asset_tag,
            metric=dominant_metric,
            value=dominant_value,
            is_anomaly=is_anomaly,
            anomaly_score=anomaly_score,
            health_score=reading.health_score,
            source=source,
        )

    @staticmethod
    def _dominant_metric(reading: NormalizedReading) -> tuple[str, float]:
        """Identifica qual métrica está mais fora do padrão, reusando a
        MESMA lógica de penalidades do HealthScoreCalculator (normalizer.py)
        em vez de comparar unidades incompatíveis entre si (o que fazia
        temperatura dominar quase sempre, mesmo em anomalias de vibração
        ou corrente)."""
        penalties = {
            "temperature": 0.0,
            "vibration": 0.0,
            "current_load": 0.0,
            "rpm": 0.0,
            "power_factor": 0.0,
        }

        t = reading.temperature_c
        if t > TEMP_CRITICAL_C:
            penalties["temperature"] = min(40, (t - TEMP_CRITICAL_C) * 3)
        elif t > TEMP_WARNING_C:
            penalties["temperature"] = (t - TEMP_WARNING_C) / (TEMP_CRITICAL_C - TEMP_WARNING_C) * 20

        v = reading.vibration_mm_s
        if v > VIB_CRITICAL_MM_S:
            penalties["vibration"] = min(35, (v - VIB_CRITICAL_MM_S) * 5)
        elif v > VIB_WARNING_MM_S:
            penalties["vibration"] = (v - VIB_WARNING_MM_S) / (VIB_CRITICAL_MM_S - VIB_WARNING_MM_S) * 15

        c = reading.current_load_pct
        if c > LOAD_CRITICAL_PCT:
            penalties["current_load"] = min(30, (c - LOAD_CRITICAL_PCT) * 2)
        elif c > LOAD_WARNING_PCT:
            penalties["current_load"] = (c - LOAD_WARNING_PCT) / 10 * 15

        penalties["rpm"] = min(15, max(0.0, abs(reading.rpm_deviation_pct) - 5.0))
        penalties["power_factor"] = max(0.0, (0.80 - reading.power_factor) * 30)

        metric = max(penalties, key=penalties.get)
        value_by_metric = {
            "temperature": reading.temperature_c,
            "vibration": reading.vibration_mm_s,
            "current_load": reading.current_load_pct,
            "rpm": reading.rpm_deviation_pct,   # desvio %, não o RPM absoluto — é o que a severidade usa
            "power_factor": reading.power_factor,
        }
        return metric, value_by_metric[metric]

    def predict_batch(self, readings: list[NormalizedReading]) -> list[AnomalySignal]:
        return [self.predict(r) for r in readings]
