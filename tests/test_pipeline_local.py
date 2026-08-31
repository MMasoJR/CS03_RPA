"""
tests/test_pipeline_local.py
Teste local do pipeline completo — CS02 (collect → transform → validate →
load) + CS03 (scraper, modelo de ML, motor de regras).

O DataLoader agora persiste de verdade via SQLAlchemy (antes era CSV).
Os testes que envolvem o loader injetam uma sessão SQLite in-memory
(`DataLoader(session=...)`), então não precisam de um Postgres real
nem sujam nenhum arquivo em disco. Os testes do scraper e do modelo de
anomalia continuam usando um diretório temporário (`tmp_data_dir`),
porque esses dois módulos ainda são baseados em arquivo
(data/weg_w22_technical_context.json e data/processed_readings.csv).
"""
import os
import sys
import uuid
from datetime import datetime

import pytest
from sqlalchemy import create_engine

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.models.database import Base, get_session_factory, Asset, SensorReading, ProcessedReading, AssetSnapshot, AssetEvent, PipelineStatus
from src.collector.sensor_simulator import DataCollector, MOTOR_CATALOG
from src.transformer.normalizer import DataTransformer, NormalizedReading
from src.validator.data_validator import DataValidator
from src.loader.db_loader import DataLoader
from src.intelligence.anomaly_model import AnomalyDetector, AnomalySignal
from src.automations.rules_engine import RulesEngine, Severity, AssetStatus
from src.scraper.weg_catalog_scraper import WegCatalogScraper, MotorTechnicalContext
from src.utils.logger import get_logger

logger = get_logger("test_pipeline")


# ─────────────────────────────────────────────
#  Fixtures compartilhadas
# ─────────────────────────────────────────────
@pytest.fixture
def db_session():
    """Sessão SQLAlchemy sobre SQLite in-memory — mesmo schema real
    (Base.metadata), sem precisar de Postgres rodando."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = get_session_factory(engine)()
    yield session
    session.close()


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    """Roda o teste dentro de um diretório temporário — usado pelos
    módulos que ainda são baseados em arquivo (scraper, anomaly model)."""
    monkeypatch.chdir(tmp_path)
    os.makedirs("data", exist_ok=True)
    return tmp_path


def make_reading(asset_tag="MTR-001", temperature_c=60.0, vibration_mm_s=2.0,
                  current_load_pct=80.0, rpm_deviation_pct=1.0, power_factor=0.9,
                  health_score=95.0, raw_reading_id=1) -> NormalizedReading:
    now = datetime.utcnow()
    return NormalizedReading(
        asset_tag=asset_tag,
        raw_reading_id=raw_reading_id,
        collected_at=now,
        processed_at=now,
        temperature_c=temperature_c,
        vibration_mm_s=vibration_mm_s,
        current_a=30.0,
        voltage_v=380.0,
        rpm=1780,
        power_factor=power_factor,
        operating_hours=1000.0,
        active_power_kw=15.0,
        temp_deviation_pct=(temperature_c - 40.0) / 40.0 * 100,
        current_load_pct=current_load_pct,
        rpm_deviation_pct=rpm_deviation_pct,
        health_score=health_score,
    )


# ─────────────────────────────────────────────
#  TESTE INTEGRADO — pipeline completo (CS02 + CS03), com banco real
# ─────────────────────────────────────────────
def test_pipeline_completo_collect_transform_validate_load_ml_rules(db_session):
    """Smoke test de ponta a ponta: roda o pipeline real (collect → transform
    → validate → load em SQLite) e o trecho da CS03 (deteccao de anomalia +
    motor de regras), verificando o que fica gravado no banco."""
    logger.info("=" * 60)
    logger.info("  TEST: Pipeline completo (CS02 + CS03), com SQLAlchemy")
    logger.info("=" * 60)

    loader = DataLoader(session=db_session)

    # ── 1. COLLECT ──────────────────────────────────────────
    logger.info("[1/6] COLLECT")
    collector = DataCollector()
    raw_readings = collector.iot.collect()
    assert len(raw_readings) == len(MOTOR_CATALOG), f"Esperado {len(MOTOR_CATALOG)} leituras"

    # ── 2. TRANSFORM ─────────────────────────────────────────
    logger.info("[2/6] TRANSFORM")
    transformer = DataTransformer()
    normalized = transformer.transform_batch(raw_readings)
    assert len(normalized) == len(raw_readings), "Transformação perdeu leituras"
    for n in normalized:
        assert -10 <= n.temperature_c <= 200, f"Temperatura inválida: {n.temperature_c}"
        assert 0 <= n.vibration_mm_s <= 50, f"Vibração inválida: {n.vibration_mm_s}"
        assert 0 <= n.health_score <= 100, f"Health Score inválido: {n.health_score}"
        assert n.active_power_kw > 0, "Potência ativa deve ser positiva"

    # ── 3. VALIDATE ──────────────────────────────────────────
    logger.info("[3/6] VALIDATE")
    validator = DataValidator()
    valid, invalid = validator.validate_batch(normalized)
    base_readings = valid if valid else normalized

    # ── 4. LOAD (Postgres/SQLAlchemy — SQLite in-memory no teste) ──
    logger.info("[4/6] LOAD")
    run_id = str(uuid.uuid4())
    raw_ids = loader.insert_raw_readings(raw_readings)
    assert len(raw_ids) == len(raw_readings), "Inserção de leituras brutas falhou"
    assert db_session.query(SensorReading).count() == len(raw_readings)
    assert db_session.query(Asset).count() == len(MOTOR_CATALOG)

    for n, rid in zip(normalized, raw_ids):
        n.raw_reading_id = rid

    loaded_count = loader.insert_processed_readings(normalized)
    assert loaded_count == len(normalized)
    assert db_session.query(ProcessedReading).count() == len(normalized)

    loader.generate_snapshot(base_readings)
    assert db_session.query(AssetSnapshot).count() == len(base_readings)

    # ── 5. ANOMALY DETECTION (CS03) ────────────────────────────
    logger.info("[5/6] ML — Anomaly detection")
    detector = AnomalyDetector()
    signals = detector.predict_batch(base_readings)
    assert len(signals) == len(base_readings)
    assert all(isinstance(s, AnomalySignal) for s in signals)
    assert all(s.source in ("isolation_forest", "rule_fallback") for s in signals)

    # ── 6. RULES ENGINE (CS03) — eventos + status ───────────────
    logger.info("[6/6] RULES — eventos e status do ativo")
    rules_engine = RulesEngine({
        "temperature_tolerance_c": {"warning_c": 90.0, "critical_c": 105.0},
        "troubleshooting_guide": [],
    })
    events = rules_engine.process_batch(signals)
    assert len(events) == len(signals)

    loader.log_events(events)
    assert db_session.query(AssetEvent).count() == len(events)

    status_updates = [
        {"asset_tag": e.asset_tag, "status": rules_engine.status_for(e)}
        for e in events
    ]
    loader.update_asset_status(status_updates)

    updated_assets = db_session.query(Asset).all()
    assert len(updated_assets) == len(set(e.asset_tag for e in events))
    for asset in updated_assets:
        assert asset.status is not None

    loader.close()
    logger.info("=" * 60)
    logger.info("  ✓ PIPELINE COMPLETO (CS02 + CS03) — TODOS OS TESTES PASSARAM!")
    logger.info("=" * 60)


# ─────────────────────────────────────────────
#  AnomalyDetector — fallback baseado em regra
# ─────────────────────────────────────────────
class TestAnomalyDetectorFallback:
    def test_sem_modelo_treinado_usa_fallback_de_regra(self, tmp_data_dir):
        detector = AnomalyDetector()
        assert detector.model is None  # nenhum data/models/anomaly_model.joblib ainda

        leitura_normal = make_reading(health_score=95.0)
        signal = detector.predict(leitura_normal)
        assert signal.source == "rule_fallback"
        assert signal.is_anomaly is False

    def test_health_score_baixo_marca_anomalia_no_fallback(self, tmp_data_dir):
        detector = AnomalyDetector()
        leitura_ruim = make_reading(health_score=30.0, temperature_c=95.0)
        signal = detector.predict(leitura_ruim)
        assert signal.is_anomaly is True
        assert signal.anomaly_score > 0

    def test_retrain_sem_historico_suficiente_retorna_false(self, tmp_data_dir):
        detector = AnomalyDetector()
        assert detector.retrain() is False  # sem processed_readings.csv / poucas amostras

    def test_metrica_dominante_aponta_temperatura_quando_e_o_pior_desvio(self, tmp_data_dir):
        detector = AnomalyDetector()
        leitura = make_reading(temperature_c=120.0, vibration_mm_s=1.0, current_load_pct=80.0)
        signal = detector.predict(leitura)
        assert signal.metric == "temperature"


# ─────────────────────────────────────────────
#  RulesEngine — severidade e ação sugerida
# ─────────────────────────────────────────────
class TestRulesEngine:
    @pytest.fixture
    def technical_context(self):
        return {
            "temperature_tolerance_c": {"warning_c": 90.0, "critical_c": 105.0},
            "troubleshooting_guide": [
                {"metric": "temperature", "likely_cause": "Sobrecarga", "action": "Reduzir carga"},
                {"metric": "vibration", "likely_cause": "Desalinhamento", "action": "Inspecionar acoplamento"},
            ],
        }

    def test_sem_anomalia_e_severidade_info_e_status_operational(self, technical_context):
        engine = RulesEngine(technical_context)
        signal = AnomalySignal(asset_tag="MTR-001", metric="temperature", value=60.0,
                                is_anomaly=False, anomaly_score=0.1)
        event = engine.process(signal)
        assert event.severity == Severity.INFO.value
        assert engine.status_for(event) == AssetStatus.OPERATIONAL.value
        assert event.suggested_action is None

    def test_temperatura_acima_do_critico_scraped_vira_critico(self, technical_context):
        engine = RulesEngine(technical_context)
        signal = AnomalySignal(asset_tag="MTR-001", metric="temperature", value=110.0,
                                is_anomaly=True, anomaly_score=0.9)
        event = engine.process(signal)
        assert event.severity == Severity.CRITICO.value
        assert engine.status_for(event) == AssetStatus.CRITICAL.value
        assert "Reduzir carga" in event.suggested_action

    def test_temperatura_entre_warning_e_critical_vira_alerta(self, technical_context):
        engine = RulesEngine(technical_context)
        signal = AnomalySignal(asset_tag="MTR-001", metric="temperature", value=95.0,
                                is_anomaly=True, anomaly_score=0.6)
        event = engine.process(signal)
        assert event.severity == Severity.ALERTA.value

    def test_vibracao_acima_do_limite_iso_vira_critico(self, technical_context):
        engine = RulesEngine(technical_context)
        signal = AnomalySignal(asset_tag="MTR-001", metric="vibration", value=9.0,
                                is_anomaly=True, anomaly_score=0.3)
        event = engine.process(signal)
        assert event.severity == Severity.CRITICO.value
        assert "Inspecionar acoplamento" in event.suggested_action

    def test_corrente_acima_do_limite_de_carga_vira_alerta(self, technical_context):
        engine = RulesEngine(technical_context)
        signal = AnomalySignal(asset_tag="MTR-001", metric="current_load", value=105.0,
                                is_anomaly=True, anomaly_score=0.2)
        event = engine.process(signal)
        assert event.severity == Severity.ALERTA.value

    def test_rpm_desvio_acima_de_10_por_cento_vira_critico(self, technical_context):
        engine = RulesEngine(technical_context)
        signal = AnomalySignal(asset_tag="MTR-001", metric="rpm", value=12.0,
                                is_anomaly=True, anomaly_score=0.2)
        event = engine.process(signal)
        assert event.severity == Severity.CRITICO.value

    def test_anomalia_sem_metrica_cruzando_limiar_usa_anomaly_score(self, technical_context):
        # metrica "rpm" mas desvio pequeno (não cruza os 5%/10%) — a
        # severidade cai no fallback baseado no anomaly_score do modelo.
        engine = RulesEngine(technical_context)
        signal = AnomalySignal(asset_tag="MTR-001", metric="rpm", value=2.0,
                                is_anomaly=True, anomaly_score=0.35)
        event = engine.process(signal)
        assert event.severity == Severity.CRITICO.value

    def test_metrica_sem_entrada_no_guia_usa_acao_generica(self, technical_context):
        engine = RulesEngine(technical_context)
        signal = AnomalySignal(asset_tag="MTR-001", metric="power_factor", value=0.5,
                                is_anomaly=True, anomaly_score=0.6)
        event = engine.process(signal)
        assert event.suggested_action == "Inspecionar ativo e abrir ordem de manutencao preventiva"

    def test_process_batch_processa_todos_os_sinais(self, technical_context):
        engine = RulesEngine(technical_context)
        signals = [
            AnomalySignal(asset_tag="MTR-001", metric="temperature", value=60.0, is_anomaly=False, anomaly_score=0.1),
            AnomalySignal(asset_tag="MTR-002", metric="temperature", value=110.0, is_anomaly=True, anomaly_score=0.9),
        ]
        events = engine.process_batch(signals)
        assert len(events) == 2
        assert events[1].severity == Severity.CRITICO.value


# ─────────────────────────────────────────────
#  DataLoader — persistência de eventos e status (CS03, via SQLAlchemy)
# ─────────────────────────────────────────────
class TestDataLoaderCS03:
    def test_log_events_grava_no_banco(self, db_session):
        loader = DataLoader(session=db_session)
        engine = RulesEngine({"temperature_tolerance_c": {}, "troubleshooting_guide": []})
        signal = AnomalySignal(asset_tag="MTR-001", metric="temperature", value=110.0,
                                is_anomaly=True, anomaly_score=0.9)
        event = engine.process(signal)

        count = loader.log_events([event])
        assert count == 1
        assert db_session.query(AssetEvent).count() == 1

        row = db_session.query(AssetEvent).first()
        assert row.asset.asset_tag == "MTR-001"
        assert row.severity.value == "CRITICO"

    def test_update_asset_status_faz_upsert_por_asset_tag(self, db_session):
        loader = DataLoader(session=db_session)

        loader.update_asset_status([{"asset_tag": "MTR-001", "status": "WARNING"}])
        loader.update_asset_status([{"asset_tag": "MTR-002", "status": "OPERATIONAL"}])
        # atualiza o MTR-001 de novo — deve substituir, não duplicar o Asset
        loader.update_asset_status([{"asset_tag": "MTR-001", "status": "CRITICAL"}])

        assets = {a.asset_tag: a for a in db_session.query(Asset).all()}
        assert len(assets) == 2  # sem duplicar MTR-001
        assert assets["MTR-001"].status.value == "CRITICAL"
        assert assets["MTR-002"].status.value == "OPERATIONAL"

    def test_log_events_com_lista_vazia_nao_grava_nada(self, db_session):
        loader = DataLoader(session=db_session)
        count = loader.log_events([])
        assert count == 0
        assert db_session.query(AssetEvent).count() == 0

    def test_insert_processed_readings_cria_asset_automaticamente(self, db_session):
        loader = DataLoader(session=db_session)
        reading = make_reading(asset_tag="MTR-001", raw_reading_id=1)
        loaded = loader.insert_processed_readings([reading])
        assert loaded == 1
        asset = db_session.query(Asset).filter_by(asset_tag="MTR-001").first()
        assert asset is not None
        nominal = {c["asset_tag"]: c for c in MOTOR_CATALOG}["MTR-001"]
        assert asset.rated_current_a == nominal["rated_current_a"]  # veio do MOTOR_CATALOG


# ─────────────────────────────────────────────
#  Scraper — fallback nunca deixa o contexto técnico vazio
# ─────────────────────────────────────────────
class TestScraperFallback:
    def test_apply_fallback_preenche_todos_os_campos(self):
        scraper = WegCatalogScraper()
        ctx = MotorTechnicalContext(collected_at="2026-01-01T00:00:00")
        scraper._apply_fallback(ctx)

        assert ctx.temperature_tolerance_c["warning_c"] == 90.0
        assert ctx.temperature_tolerance_c["critical_c"] == 105.0
        assert len(ctx.efficiency_curve) == 4
        assert len(ctx.troubleshooting_guide) == 5
        assert {"temperature", "vibration", "current_load", "rpm", "power_factor"} == {
            e["metric"] for e in ctx.troubleshooting_guide
        }


if __name__ == "__main__":
    import pytest as _pytest
    raise SystemExit(_pytest.main([__file__, "-v"]))
