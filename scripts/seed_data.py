"""
scripts/seed_data.py
Popula o banco com histórico inicial de 30 dias de leituras simuladas.
Útil para demonstração e testes de análise histórica.

Uso:
  python -m scripts.seed_data
  python -m scripts.seed_data --days 7 --readings-per-hour 2
"""
import sys
import argparse
from datetime import datetime, timedelta
import random

# Adiciona o root do projeto ao path
sys.path.insert(0, ".")

from config.settings import settings
from src.models.database import init_database, get_session_factory
from src.collector.sensor_simulator import MOTOR_CATALOG, MotorState, RawSensorPayload
from src.transformer.normalizer import DataTransformer
from src.validator.data_validator import DataValidator
from src.loader.db_loader import DataLoader, AssetRegistry
from src.utils.logger import get_logger

logger = get_logger("seed_data")


def seed(days: int = 30, readings_per_hour: int = 2):
    logger.info(f"[Seed] Iniciando seed: {days} dias | {readings_per_hour} leituras/hora/motor")

    engine = init_database(settings.database_url)
    SessionFactory = get_session_factory(engine)
    session = SessionFactory()

    transformer = DataTransformer()
    validator = DataValidator()
    loader = DataLoader(session)

    # Garante cadastro dos ativos
    loader.registry.ensure_assets()

    motor_states = {c["asset_tag"]: MotorState(c) for c in MOTOR_CATALOG}

    total_raw = 0
    total_processed = 0
    interval_minutes = 60 // readings_per_hour
    start_time = datetime.utcnow() - timedelta(days=days)

    current_time = start_time
    now = datetime.utcnow()

    batch_raw = []
    batch_normalized = []

    while current_time < now:
        for tag, state in motor_states.items():
            # Gera leitura retroativa
            raw = state.sample()
            raw.collected_at = current_time.isoformat()
            batch_raw.append(raw)

        current_time += timedelta(minutes=interval_minutes)

        # Flush a cada 500 leituras
        if len(batch_raw) >= 500:
            raw_ids = loader.insert_raw_readings(batch_raw)
            normalized = transformer.transform_batch(batch_raw, raw_ids)
            valid, _ = validator.validate_batch(normalized)
            loader.insert_processed_readings(normalized)
            total_raw += len(batch_raw)
            total_processed += len(normalized)
            batch_raw = []
            logger.info(f"[Seed] Progresso: {total_raw} leituras inseridas...")

    # Flush final
    if batch_raw:
        raw_ids = loader.insert_raw_readings(batch_raw)
        normalized = transformer.transform_batch(batch_raw, raw_ids)
        loader.insert_processed_readings(normalized)
        total_raw += len(batch_raw)
        total_processed += len(normalized)

    session.close()
    logger.info(f"[Seed] ✓ Seed concluído: {total_raw} leituras brutas | {total_processed} processadas")
    logger.info(f"[Seed] Período: {start_time.date()} → {now.date()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Popula o banco com dados históricos simulados")
    parser.add_argument("--days", type=int, default=30, help="Dias de histórico (padrão: 30)")
    parser.add_argument("--readings-per-hour", type=int, default=2,
                        help="Leituras por hora por motor (padrão: 2)")
    args = parser.parse_args()
    seed(days=args.days, readings_per_hour=args.readings_per_hour)
