"""
src/validator/data_validator.py
Validação e controle de qualidade dos dados normalizados.

Implementa regras de negócio baseadas em:
  - Intervalos físicos plausíveis (hard limits)
  - Consistência entre parâmetros (cross-validation)
  - Desvios em relação aos nominais cadastrados
"""
from dataclasses import dataclass
from typing import List, Tuple

from src.transformer.normalizer import NormalizedReading, NOMINAL_PARAMS
from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ValidationResult:
    is_valid: bool
    errors: List[str]
    warnings: List[str]

    @property
    def has_warnings(self) -> bool:
        return len(self.warnings) > 0


# ─────────────────────────────────────────────
#  Regras de validação
# ─────────────────────────────────────────────
RULES = {
    # (min, max, campo, mensagem de erro)
    "temperature_c":  (-10.0, 200.0, "Temperatura fora do intervalo físico (-10 a 200°C)"),
    "vibration_mm_s": (0.0, 50.0,   "Vibração fora do intervalo físico (0 a 50 mm/s)"),
    "current_a":      (0.0, 5000.0, "Corrente fora do intervalo físico (0 a 5000 A)"),
    "voltage_v":      (100.0, 1500.0,"Tensão fora do intervalo físico (100 a 1500 V)"),
    "rpm":            (0, 10000,    "RPM fora do intervalo físico (0 a 10000)"),
    "power_factor":   (0.0, 1.0,    "Fator de potência fora do intervalo (0 a 1)"),
    "operating_hours":(0.0, 200000, "Horas de operação fora do intervalo"),
    "health_score":   (0.0, 100.0,  "Health score fora do intervalo (0 a 100)"),
}

WARNINGS = {
    "temperature_c":  (0.0, 90.0,   "Temperatura acima de 90°C — verificar refrigeração"),
    "vibration_mm_s": (0.0, 10.0,   "Vibração acima de 10 mm/s — inspeção recomendada"),
    "power_factor":   (0.75, 1.0,   "Fator de potência abaixo de 0.75 — avaliar banco de capacitores"),
    "current_load_pct":(0.0, 105.0, "Corrente acima de 105% da nominal — risco de sobrecarga"),
}


class DataValidator:
    """Valida NormalizedReading e retorna resultado com erros e avisos."""

    def validate(self, reading: NormalizedReading) -> ValidationResult:
        errors = []
        warnings = []

        # ─── Hard limits ───
        for field, (min_v, max_v, msg) in RULES.items():
            value = getattr(reading, field, None)
            if value is not None and not (min_v <= value <= max_v):
                errors.append(f"{field}={value}: {msg}")

        # ─── Warnings de negócio ───
        for field, (min_v, max_v, msg) in WARNINGS.items():
            value = getattr(reading, field, None)
            if value is not None and not (min_v <= value <= max_v):
                warnings.append(f"{field}={value}: {msg}")

        # ─── Cross-validation: potência × corrente × tensão ───
        if all([reading.active_power_kw, reading.current_a, reading.voltage_v]):
            expected_kw = (1.732 * reading.voltage_v * reading.current_a * reading.power_factor) / 1000
            deviation = abs(reading.active_power_kw - expected_kw) / max(expected_kw, 0.001)
            if deviation > 0.05:
                warnings.append(
                    f"Inconsistência em potência ativa: calculado={expected_kw:.2f}kW "
                    f"vs registrado={reading.active_power_kw:.2f}kW (desvio={deviation:.1%})"
                )

        # ─── Consistência com parâmetros nominais ───
        nominal = NOMINAL_PARAMS.get(reading.asset_tag)
        if nominal:
            if reading.voltage_v > nominal["rated_voltage_v"] * 1.10:
                warnings.append(
                    f"Tensão {reading.voltage_v}V excede 110% da nominal "
                    f"({nominal['rated_voltage_v']}V)"
                )
            if reading.voltage_v < nominal["rated_voltage_v"] * 0.90:
                warnings.append(
                    f"Tensão {reading.voltage_v}V abaixo de 90% da nominal "
                    f"({nominal['rated_voltage_v']}V)"
                )

        is_valid = len(errors) == 0

        # Atualiza a leitura com resultado da validação
        reading.is_valid = is_valid
        reading.validation_errors = errors + warnings

        if errors:
            logger.warning(
                f"[Validator] {reading.asset_tag} INVÁLIDO — {len(errors)} erro(s): {errors}"
            )
        if warnings:
            logger.info(
                f"[Validator] {reading.asset_tag} — {len(warnings)} aviso(s): {warnings}"
            )

        return ValidationResult(is_valid=is_valid, errors=errors, warnings=warnings)

    def validate_batch(
        self, readings: List[NormalizedReading]
    ) -> Tuple[List[NormalizedReading], List[NormalizedReading]]:
        """
        Valida um lote e separa em válidos/inválidos.
        Retorna (válidos, inválidos).
        """
        valid, invalid = [], []
        for reading in readings:
            result = self.validate(reading)
            if result.is_valid:
                valid.append(reading)
            else:
                invalid.append(reading)

        logger.info(
            f"[Validator] Lote: {len(readings)} total → "
            f"{len(valid)} válidos, {len(invalid)} inválidos"
        )
        return valid, invalid
