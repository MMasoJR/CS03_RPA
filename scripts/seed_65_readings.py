"""
scripts/seed_65_readings.py

Gera 65 leituras simuladas de motores e faz elas passarem pela pipeline
de verdade (transform -> validate -> load -> anomaly detection -> rules
engine), incluindo alguns cenários propositais de anomalia — pra testar
a parte de automação/RPA (Pilar 2 da CS03) sem precisar esperar o
APScheduler rodar em batch por horas.

Não mexe no DataCollector real (sensor_simulator.py) — gera os dados
brutos aqui mesmo, no mesmo formato que o transformer espera, e usa os
MESMOS módulos reais do projeto (DataTransformer, DataValidator,
AnomalyDetector, RulesEngine, DataLoader) pra garantir que o teste é
fiel ao que roda em produção.

Uso:
    python -m scripts.seed_65_readings
"""
import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from src.collector.sensor_simulator import MOTOR_CATALOG
from src.transformer.normalizer import DataTransformer
from src.validator.data_validator import DataValidator
from src.loader.db_loader import DataLoader
from src.intelligence.anomaly_model import AnomalyDetector
from src.automations.rules_engine import RulesEngine, load_technical_context
from src.utils.logger import get_logger

logger = get_logger("seed_65_readings")

TOTAL_READINGS = 65
# ~23% das leituras vêm com alguma anomalia proposital
ANOMALY_TYPES = [
    "temperatura_critica",
    "vibracao_critica",
    "sobrecarga_corrente",
    "desvio_rpm",
    "fator_potencia_baixo",
]
ANOMALY_RATIO = 0.23


@dataclass
class SyntheticRawReading:
    """Mesmo formato que o SensorReading/transformer esperam — não
    precisa ser a classe real do collector, só ter os mesmos atributos."""
    asset_tag: str
    collected_at: str
    raw_temperature: float
    raw_vibration: float
    raw_current: float
    raw_voltage: float
    raw_rpm: float
    raw_power_factor: float
    raw_operating_hours: float
    raw_unit_temperature: str = "C"
    raw_unit_vibration: str = "mm/s"


def gerar_leitura(asset_tag: str, nominal: dict, timestamp: datetime, anomalia: str | None) -> SyntheticRawReading:
    """Gera uma leitura bruta plausível — normal ou com uma anomalia
    proposital numa dimensão específica, mantendo as demais dentro do
    esperado (pra a métrica dominante ficar clara pro motor de regras)."""
    temperature = random.uniform(55, 70)
    vibration = random.uniform(1.0, 3.0)
    current = nominal["rated_current_a"] * random.uniform(0.70, 0.95)
    voltage = nominal["rated_voltage_v"] * random.uniform(0.98, 1.02)
    rpm = nominal["rated_rpm"] * random.uniform(0.99, 1.01)
    power_factor = random.uniform(0.85, 0.95)
    operating_hours = random.uniform(500, 5000)

    if anomalia == "temperatura_critica":
        temperature = random.uniform(106, 130)  # acima do limite crítico WEG (105°C)
    elif anomalia == "vibracao_critica":
        vibration = random.uniform(9.0, 15.0)   # acima do limite ISO 10816-3 (7.1 mm/s)
    elif anomalia == "sobrecarga_corrente":
        current = nominal["rated_current_a"] * random.uniform(1.15, 1.40)
    elif anomalia == "desvio_rpm":
        rpm = nominal["rated_rpm"] * random.uniform(1.16, 1.25)  # penalidade > 10 pontos, garante < 90
    elif anomalia == "fator_potencia_baixo":
        power_factor = random.uniform(0.35, 0.50)  # penalidade > 10 pontos, garante < 90

    return SyntheticRawReading(
        asset_tag=asset_tag,
        collected_at=timestamp.isoformat(),
        raw_temperature=round(temperature, 2),
        raw_vibration=round(vibration, 3),
        raw_current=round(current, 2),
        raw_voltage=round(voltage, 1),
        raw_rpm=round(rpm, 0),
        raw_power_factor=round(power_factor, 3),
        raw_operating_hours=round(operating_hours, 2),
    )


def main():
    logger.info("=" * 70)
    logger.info(f"  SEED — Gerando {TOTAL_READINGS} leituras simuladas (com anomalias)")
    logger.info("=" * 70)

    nominal_by_tag = {m["asset_tag"]: m for m in MOTOR_CATALOG}
    asset_tags = list(nominal_by_tag.keys())

    n_anomalias = round(TOTAL_READINGS * ANOMALY_RATIO)
    plano = ["normal"] * (TOTAL_READINGS - n_anomalias) + [
        random.choice(ANOMALY_TYPES) for _ in range(n_anomalias)
    ]
    random.shuffle(plano)

    transformer = DataTransformer()
    validator = DataValidator()
    detector = AnomalyDetector()
    technical_context = load_technical_context()
    rules_engine = RulesEngine(technical_context)
    loader = DataLoader()

    base_time = datetime.utcnow() - timedelta(hours=TOTAL_READINGS)
    resumo = {"total": 0, "anomalias_detectadas": 0, "alerta": 0, "critico": 0}

    try:
        for i, tipo_anomalia in enumerate(plano):
            asset_tag = asset_tags[i % len(asset_tags)]
            nominal = nominal_by_tag[asset_tag]
            timestamp = base_time + timedelta(minutes=i * 15)
            anomalia = None if tipo_anomalia == "normal" else tipo_anomalia

            raw = gerar_leitura(asset_tag, nominal, timestamp, anomalia)

            raw_ids = loader.insert_raw_readings([raw])
            normalized = transformer.transform(raw, raw_ids[0])
            if normalized is None:
                continue

            validator.validate_batch([normalized])
            loader.insert_processed_readings([normalized])
            loader.generate_snapshot([normalized])

            signal = detector.predict(normalized)
            event = rules_engine.process(signal)
            loader.log_events([event])
            loader.update_asset_status([{
                "asset_tag": event.asset_tag,
                "status": rules_engine.status_for(event),
            }])

            resumo["total"] += 1
            if signal.is_anomaly:
                resumo["anomalias_detectadas"] += 1
            if event.severity == "ALERTA":
                resumo["alerta"] += 1
            elif event.severity == "CRITICO":
                resumo["critico"] += 1

            marcador = f" ← {tipo_anomalia.upper()}" if anomalia else ""
            logger.info(
                f"[{i+1:02d}/{TOTAL_READINGS}] {asset_tag} | "
                f"T={raw.raw_temperature}°C Vib={raw.raw_vibration}mm/s "
                f"I={raw.raw_current}A RPM={raw.raw_rpm} PF={raw.raw_power_factor} "
                f"→ severidade={event.severity}{marcador}"
            )
    finally:
        loader.close()

    logger.info("=" * 70)
    logger.info("  RESUMO")
    logger.info(f"  Leituras geradas       : {resumo['total']}")
    logger.info(f"  Anomalias detectadas   : {resumo['anomalias_detectadas']}")
    logger.info(f"  Eventos ALERTA         : {resumo['alerta']}")
    logger.info(f"  Eventos CRITICO        : {resumo['critico']}")
    logger.info("=" * 70)
    logger.info("  Confira em asset_events / asset_status no Supabase, ou no dashboard Streamlit.")


if __name__ == "__main__":
    main()
