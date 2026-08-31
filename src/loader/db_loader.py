"""
src/loader/db_loader.py

Persistência real via SQLAlchemy/PostgreSQL (antes era só CSV). Usa os
models de src/models/database.py — inclusive a tabela nova asset_events
(CS03). Mantém a mesma API pública que o orchestrator já chama, então
não precisa mexer em mais nada além deste arquivo + database.py.
"""
from datetime import datetime

from config.settings import settings
from src.collector.sensor_simulator import MOTOR_CATALOG
from src.models.database import (
    Asset, SensorReading, ProcessedReading, PipelineLog,
    AssetSnapshot, AssetEvent, AssetStatus, EventSeverity,
    get_session_factory, init_database,
)
from src.utils.logger import get_logger

logger = get_logger("loader")

# Índice de parâmetros nominais por asset_tag, usado para criar o Asset
# automaticamente na primeira vez que uma leitura desse motor aparece.
NOMINAL_BY_TAG = {c["asset_tag"]: c for c in MOTOR_CATALOG}


class DataLoader:
    def __init__(self, session=None):
        if session is not None:
            # Permite injetar uma sessão pronta (ex: testes com SQLite in-memory)
            self.session = session
            self._owns_session = False
        else:
            engine = init_database(settings.database_url)
            self.session = get_session_factory(engine)()
            self._owns_session = True

    def close(self):
        if self._owns_session:
            self.session.close()

    # ─────────────────────────────────────────────
    #  Helpers
    # ─────────────────────────────────────────────
    def _get_or_create_asset(self, asset_tag: str) -> Asset:
        asset = self.session.query(Asset).filter_by(asset_tag=asset_tag).first()
        if asset:
            return asset

        nominal = NOMINAL_BY_TAG.get(asset_tag, {})
        asset = Asset(
            asset_tag=asset_tag,
            name=nominal.get("name", asset_tag),
            location=nominal.get("location", "N/A"),
            manufacturer=nominal.get("manufacturer", "WEG"),
            model=nominal.get("model", "W22"),
            rated_power_kw=nominal.get("rated_power_kw", 0.0),
            rated_voltage_v=nominal.get("rated_voltage_v", 0.0),
            rated_current_a=nominal.get("rated_current_a", 0.0),
            rated_rpm=nominal.get("rated_rpm", 0),
        )
        self.session.add(asset)
        self.session.flush()  # garante asset.id sem precisar commitar ainda
        logger.info(f"[Loader] Novo ativo cadastrado automaticamente: {asset_tag} (id={asset.id})")
        return asset

    # ─────────────────────────────────────────────
    #  Pipeline logs
    # ─────────────────────────────────────────────
    def log_pipeline_run(self, run_id, stage, status, started_at,
                          records_input=0, records_output=0, records_failed=0, error_message=""):
        try:
            log = PipelineLog(
                run_id=run_id,
                stage=stage,
                status=status,
                started_at=self._parse_dt(started_at),
                finished_at=datetime.utcnow(),
                records_input=records_input,
                records_output=records_output,
                records_failed=records_failed,
                error_message=error_message or None,
            )
            self.session.add(log)
            self.session.commit()
        except Exception as exc:
            logger.error(f"[Loader] Falha ao gravar log do pipeline: {exc}")
            self.session.rollback()

    # ─────────────────────────────────────────────
    #  Leituras brutas
    # ─────────────────────────────────────────────
    def insert_raw_readings(self, raw_readings):
        ids = []
        try:
            for r in raw_readings:
                asset = self._get_or_create_asset(r.asset_tag)
                row = SensorReading(
                    asset_id=asset.id,
                    source="iot_simulator",
                    collected_at=self._parse_dt(r.collected_at),
                    raw_temperature=r.raw_temperature,
                    raw_vibration=r.raw_vibration,
                    raw_current=r.raw_current,
                    raw_voltage=r.raw_voltage,
                    raw_rpm=r.raw_rpm,
                    raw_power_factor=r.raw_power_factor,
                    raw_operating_hours=r.raw_operating_hours,
                    raw_unit_temperature=getattr(r, "raw_unit_temperature", "C"),
                    raw_unit_vibration=getattr(r, "raw_unit_vibration", "mm/s"),
                    raw_payload=self._to_dict(r),
                )
                self.session.add(row)
                self.session.flush()
                ids.append(row.id)
            self.session.commit()
        except Exception as exc:
            logger.error(f"[Loader] Falha ao inserir leituras brutas: {exc}")
            self.session.rollback()
            raise
        return ids

    # ─────────────────────────────────────────────
    #  Leituras processadas
    # ─────────────────────────────────────────────
    def insert_processed_readings(self, normalized):
        if not normalized:
            return 0
        try:
            count = 0
            for n in normalized:
                asset = self._get_or_create_asset(n.asset_tag)
                row = ProcessedReading(
                    asset_id=asset.id,
                    raw_reading_id=n.raw_reading_id,
                    processed_at=datetime.utcnow(),
                    temperature_c=n.temperature_c,
                    vibration_mm_s=n.vibration_mm_s,
                    current_a=n.current_a,
                    voltage_v=n.voltage_v,
                    rpm=n.rpm,
                    power_factor=n.power_factor,
                    operating_hours=n.operating_hours,
                    active_power_kw=n.active_power_kw,
                    temp_deviation_pct=n.temp_deviation_pct,
                    current_load_pct=n.current_load_pct,
                    rpm_deviation_pct=n.rpm_deviation_pct,
                    health_score=n.health_score,
                    is_valid=n.is_valid,
                    validation_errors=n.validation_errors or [],
                )
                self.session.add(row)
                count += 1
            self.session.commit()
            return count
        except Exception as exc:
            logger.error(f"[Loader] Falha ao inserir leituras processadas: {exc}")
            self.session.rollback()
            raise

    # ─────────────────────────────────────────────
    #  CS03: eventos e status do ativo
    # ─────────────────────────────────────────────
    def log_events(self, events):
        if not events:
            return 0
        try:
            count = 0
            for e in events:
                asset = self._get_or_create_asset(e.asset_tag)
                row = AssetEvent(
                    asset_id=asset.id,
                    event_type=e.event_type,
                    severity=EventSeverity(e.severity),
                    metric=e.metric,
                    metric_value=e.metric_value,
                    threshold_used=e.threshold_used,
                    anomaly_score=e.anomaly_score,
                    suggested_action=e.suggested_action,
                    created_at=self._parse_dt(e.created_at),
                )
                self.session.add(row)
                count += 1
            self.session.commit()
            logger.info(f"[Loader] {count} evento(s) gravado(s) em asset_events")
            return count
        except Exception as exc:
            logger.error(f"[Loader] Falha ao gravar eventos: {exc}")
            self.session.rollback()
            raise

    def update_asset_status(self, status_updates):
        """Atualiza o campo Asset.status (já existente no model) de cada
        ativo. Recebe uma lista de dicts:
            {"asset_tag": ..., "status": ..., "health_score": ..., ...}
        """
        if not status_updates:
            return
        try:
            for update in status_updates:
                asset = self._get_or_create_asset(update["asset_tag"])
                asset.status = AssetStatus(update["status"])
                asset.updated_at = datetime.utcnow()
            self.session.commit()
            logger.info(f"[Loader] Status atualizado para {len(status_updates)} ativo(s)")
        except Exception as exc:
            logger.error(f"[Loader] Falha ao atualizar status dos ativos: {exc}")
            self.session.rollback()
            raise

    # ─────────────────────────────────────────────
    #  Snapshot
    # ─────────────────────────────────────────────
    def generate_snapshot(self, readings):
        if not readings:
            return
        try:
            for r in readings:
                asset = self._get_or_create_asset(r.asset_tag)
                status = asset.status or AssetStatus.OPERATIONAL
                snap = AssetSnapshot(
                    asset_id=asset.id,
                    snapshot_at=datetime.utcnow(),
                    status=status,
                    avg_temperature_c=r.temperature_c,
                    avg_vibration_mm_s=r.vibration_mm_s,
                    avg_current_a=r.current_a,
                    avg_voltage_v=r.voltage_v,
                    avg_rpm=r.rpm,
                    avg_power_factor=r.power_factor,
                    avg_health_score=r.health_score,
                    min_health_score=r.health_score,
                    max_health_score=r.health_score,
                    readings_count=1,
                    anomalies_count=0 if r.is_valid else 1,
                    period_hours=settings.batch_interval_seconds / 3600,
                )
                self.session.add(snap)
            self.session.commit()
        except Exception as exc:
            logger.error(f"[Loader] Falha ao gerar snapshot: {exc}")
            self.session.rollback()
            raise

    # ─────────────────────────────────────────────
    #  Utilitários
    # ─────────────────────────────────────────────
    @staticmethod
    def _to_dict(obj):
        if isinstance(obj, dict):
            return obj
        if hasattr(obj, "model_dump"):
            return obj.model_dump()
        if hasattr(obj, "dict"):
            return obj.dict()
        return {k: str(v) for k, v in vars(obj).items()}

    @staticmethod
    def _parse_dt(value):
        if isinstance(value, datetime):
            return value
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return datetime.utcnow()
