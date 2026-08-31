"""
src/transformer/normalizer.py
Transformação e normalização dos dados brutos dos sensores.

Responsabilidades:
  1. Conversão de unidades (°F → °C, in/s → mm/s, etc.)
  2. Padronização de timestamps para UTC ISO-8601
  3. Cálculo de indicadores derivados (health_score, desvios)
  4. Cálculo de potência ativa (P = √3 × V × I × cos φ)
"""
from dataclasses import dataclass
from typing import Optional, Dict, Any
from datetime import datetime

from src.collector.sensor_simulator import RawSensorPayload, MOTOR_CATALOG
from src.utils.logger import get_logger

logger = get_logger(__name__)

# Índice de parâmetros nominais por asset_tag
NOMINAL_PARAMS: Dict[str, dict] = {c["asset_tag"]: c for c in MOTOR_CATALOG}

# Limiares de saúde (ISO 10816-3 para máquinas industriais)
TEMP_WARNING_C = 70.0
TEMP_CRITICAL_C = 85.0
VIB_WARNING_MM_S = 4.5
VIB_CRITICAL_MM_S = 7.1
LOAD_WARNING_PCT = 100.0
LOAD_CRITICAL_PCT = 110.0


@dataclass
class NormalizedReading:
    """Leitura com unidades padronizadas e indicadores calculados."""
    asset_tag: str
    raw_reading_id: Optional[int]
    collected_at: datetime
    processed_at: datetime

    temperature_c: float
    vibration_mm_s: float
    current_a: float
    voltage_v: float
    rpm: int
    power_factor: float
    operating_hours: float
    active_power_kw: float

    temp_deviation_pct: float
    current_load_pct: float
    rpm_deviation_pct: float
    health_score: float

    is_valid: bool = True
    validation_errors: list = None

    def __post_init__(self):
        if self.validation_errors is None:
            self.validation_errors = []


class UnitConverter:
    """Converte unidades para o padrão SI do sistema."""

    @staticmethod
    def temperature_to_celsius(value: float, unit: str) -> float:
        unit = unit.strip().upper()
        if unit in ("F", "°F", "FAHRENHEIT"):
            converted = (value - 32) * 5 / 9
            logger.debug(f"[UnitConv] {value}°F → {converted:.2f}°C")
            return round(converted, 2)
        if unit in ("K", "KELVIN"):
            return round(value - 273.15, 2)
        return value  # Já em Celsius

    @staticmethod
    def vibration_to_mm_s(value: float, unit: str) -> float:
        unit = unit.strip().lower()
        if unit in ("in/s", "ins"):
            converted = value * 25.4
            logger.debug(f"[UnitConv] {value} in/s → {converted:.3f} mm/s")
            return round(converted, 3)
        if unit in ("cm/s",):
            return round(value * 10, 3)
        return value  # Já em mm/s


class HealthScoreCalculator:
    """
    Calcula o Health Score (0–100) com base em múltiplos parâmetros.
    Score 100 = perfeito, 0 = crítico.

    Metodologia: ponderação de penalidades por desvio dos limiares.
    """

    @staticmethod
    def calculate(
        temperature_c: float,
        vibration_mm_s: float,
        current_load_pct: float,
        rpm_deviation_pct: float,
        power_factor: float,
    ) -> float:
        score = 100.0
        penalties = []

        # ─── Temperatura ───
        if temperature_c > TEMP_CRITICAL_C:
            p = min(40, (temperature_c - TEMP_CRITICAL_C) * 3)
            penalties.append(("temp_critical", p))
        elif temperature_c > TEMP_WARNING_C:
            p = (temperature_c - TEMP_WARNING_C) / (TEMP_CRITICAL_C - TEMP_WARNING_C) * 20
            penalties.append(("temp_warning", p))

        # ─── Vibração (ISO 10816-3) ───
        if vibration_mm_s > VIB_CRITICAL_MM_S:
            p = min(35, (vibration_mm_s - VIB_CRITICAL_MM_S) * 5)
            penalties.append(("vib_critical", p))
        elif vibration_mm_s > VIB_WARNING_MM_S:
            p = (vibration_mm_s - VIB_WARNING_MM_S) / (VIB_CRITICAL_MM_S - VIB_WARNING_MM_S) * 15
            penalties.append(("vib_warning", p))

        # ─── Sobrecarga de corrente ───
        if current_load_pct > LOAD_CRITICAL_PCT:
            p = min(30, (current_load_pct - LOAD_CRITICAL_PCT) * 2)
            penalties.append(("overload_critical", p))
        elif current_load_pct > LOAD_WARNING_PCT:
            p = (current_load_pct - LOAD_WARNING_PCT) / 10 * 15
            penalties.append(("overload_warning", p))

        # ─── Desvio de RPM ───
        if abs(rpm_deviation_pct) > 5.0:
            p = min(15, abs(rpm_deviation_pct) - 5)
            penalties.append(("rpm_deviation", p))

        # ─── Fator de potência ───
        if power_factor < 0.80:
            p = (0.80 - power_factor) * 30
            penalties.append(("low_pf", p))

        total_penalty = sum(v for _, v in penalties)
        final_score = max(0.0, round(score - total_penalty, 2))

        if penalties:
            logger.debug(f"[HealthScore] Penalidades: {penalties} → Score: {final_score}")

        return final_score


class DataTransformer:
    """
    Orquestra a transformação completa de um RawSensorPayload
    em um NormalizedReading.
    """

    def __init__(self):
        self.converter = UnitConverter()
        self.health_calc = HealthScoreCalculator()

    def transform(
        self,
        raw: RawSensorPayload,
        raw_reading_id: Optional[int] = None,
    ) -> Optional[NormalizedReading]:
        """
        Transforma uma leitura bruta em dado normalizado.
        Retorna None se a transformação for impossível.
        """
        nominal = NOMINAL_PARAMS.get(raw.asset_tag)
        if nominal is None:
            logger.warning(f"[Transformer] Motor desconhecido: {raw.asset_tag} — ignorado")
            return None

        try:
            collected_at = datetime.fromisoformat(raw.collected_at)
        except ValueError:
            logger.warning(f"[Transformer] Timestamp inválido: {raw.collected_at}")
            collected_at = datetime.utcnow()

        # ─── Conversão de unidades ───
        temp_c = self.converter.temperature_to_celsius(
            raw.raw_temperature, raw.raw_unit_temperature
        )
        vib_mm_s = self.converter.vibration_to_mm_s(
            raw.raw_vibration, raw.raw_unit_vibration
        )

        # ─── Potência ativa (trifásico): P = √3 × V × I × cos φ ───
        active_power_kw = round(
            (1.732 * raw.raw_voltage * raw.raw_current * raw.raw_power_factor) / 1000,
            3,
        )

        # ─── Indicadores derivados ───
        rated_temp_base = 40.0  # Temperatura nominal de operação (ambiente + perdas)
        temp_deviation_pct = round(
            ((temp_c - rated_temp_base) / rated_temp_base) * 100, 2
        ) if rated_temp_base else 0.0

        current_load_pct = round(
            (raw.raw_current / nominal["rated_current_a"]) * 100, 2
        )

        rpm_deviation_pct = round(
            ((raw.raw_rpm - nominal["rated_rpm"]) / nominal["rated_rpm"]) * 100, 2
        )

        # ─── Health Score ───
        health_score = self.health_calc.calculate(
            temperature_c=temp_c,
            vibration_mm_s=vib_mm_s,
            current_load_pct=current_load_pct,
            rpm_deviation_pct=rpm_deviation_pct,
            power_factor=raw.raw_power_factor,
        )

        normalized = NormalizedReading(
            asset_tag=raw.asset_tag,
            raw_reading_id=raw_reading_id,
            collected_at=collected_at,
            processed_at=datetime.utcnow(),
            temperature_c=round(temp_c, 2),
            vibration_mm_s=round(vib_mm_s, 3),
            current_a=round(raw.raw_current, 2),
            voltage_v=round(raw.raw_voltage, 1),
            rpm=int(round(raw.raw_rpm)),
            power_factor=round(raw.raw_power_factor, 3),
            operating_hours=round(raw.raw_operating_hours, 2),
            active_power_kw=active_power_kw,
            temp_deviation_pct=temp_deviation_pct,
            current_load_pct=current_load_pct,
            rpm_deviation_pct=rpm_deviation_pct,
            health_score=health_score,
        )

        logger.debug(
            f"[Transformer] {raw.asset_tag} → T={temp_c:.1f}°C | "
            f"Vib={vib_mm_s:.2f}mm/s | Health={health_score}"
        )
        return normalized

    def transform_batch(
        self, raw_list: list, raw_ids: Optional[list] = None
    ) -> list:
        """Transforma um lote de leituras brutas."""
        results = []
        raw_ids = raw_ids or [None] * len(raw_list)
        for raw, rid in zip(raw_list, raw_ids):
            normalized = self.transform(raw, rid)
            if normalized:
                results.append(normalized)
        logger.info(
            f"[Transformer] Lote processado: {len(raw_list)} entrada(s) → "
            f"{len(results)} saída(s)"
        )
        return results
