"""
src/pipeline/orchestrator.py
Orquestrador principal do pipeline de automação RPA.

Fluxo de execução por ciclo:
  [COLLECT] → [LOAD RAW] → [TRANSFORM] → [VALIDATE] → [LOAD PROCESSED]
      → [ANOMALY DETECTION] → [RULES ENGINE] → [SNAPSHOT]

Modos de operação:
  - batch     : Executa em intervalos fixos via APScheduler
  - streaming : Loop contínuo com intervalo curto (quasi real-time)
  - once      : Executa uma vez e encerra (útil para testes/CI)
"""
import uuid
import sys
from datetime import datetime
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config.settings import settings
# Importando apenas o PipelineStatus, sem funções de DB
from src.models.database import PipelineStatus
from src.collector.sensor_simulator import DataCollector
from src.transformer.normalizer import DataTransformer
from src.validator.data_validator import DataValidator
from src.loader.db_loader import DataLoader
from src.intelligence.anomaly_model import AnomalyDetector
from src.automations.rules_engine import RulesEngine
from src.utils.logger import get_logger

logger = get_logger("orchestrator")


class PipelineOrchestrator:
    """
    Implementa o fluxo completo de automação RPA para ingestão,
    transformação, validação, persistência e — a partir da CS03 —
    detecção de anomalias e resposta operacional automatizada.
    """

    def __init__(self):
        logger.info("=" * 70)
        logger.info("  Motor Monitoring Pipeline — Inicializando")
        logger.info(f"  Modo: {settings.pipeline_mode.upper()}")
        logger.info(f"  Intervalo: {settings.batch_interval_seconds}s")
        logger.info("=" * 70)

        # Componentes do pipeline (Sem banco de dados, direto para CSV)
        self.collector = DataCollector()
        self.transformer = DataTransformer()
        self.validator = DataValidator()
        # CS03: inteligência e resposta operacional
        self.anomaly_detector = AnomalyDetector()
        self.rules_engine = RulesEngine()
        self._run_count = 0

    def run_cycle(self):
        """Executa um ciclo completo do pipeline."""
        run_id = str(uuid.uuid4())
        cycle_start = datetime.utcnow()
        self._run_count += 1

        logger.info(f"{'─'*60}")
        logger.info(f"[Pipeline] Ciclo #{self._run_count} | run_id={run_id[:8]}...")
        logger.info(f"{'─'*60}")

        # Instancia o DataLoader limpo, sem passar session
        loader = DataLoader(None)

        try:
            # ═══════════════════════════════════════
            #  STAGE 1: COLLECT
            # ═══════════════════════════════════════
            stage_start = datetime.utcnow()
            logger.info("[STAGE 1/7] COLLECT — Coletando dados dos sensores...")

            raw_readings = self.collector.collect_all()

            loader.log_pipeline_run(
                run_id=run_id, stage="collect",
                status=PipelineStatus.SUCCESS,
                started_at=stage_start,
                records_input=0,
                records_output=len(raw_readings),
            )

            if not raw_readings:
                logger.warning("[STAGE 1] Nenhuma leitura coletada. Ciclo encerrado.")
                return

            # ═══════════════════════════════════════
            #  STAGE 2: LOAD RAW
            # ═══════════════════════════════════════
            stage_start = datetime.utcnow()
            logger.info(f"[STAGE 2/7] LOAD RAW — Persistindo {len(raw_readings)} leituras brutas...")

            raw_ids = loader.insert_raw_readings(raw_readings)

            loader.log_pipeline_run(
                run_id=run_id, stage="load_raw",
                status=PipelineStatus.SUCCESS,
                started_at=stage_start,
                records_input=len(raw_readings),
                records_output=len(raw_ids),
                records_failed=len(raw_readings) - len(raw_ids),
            )

            # ═══════════════════════════════════════
            #  STAGE 3: TRANSFORM
            # ═══════════════════════════════════════
            stage_start = datetime.utcnow()
            logger.info(f"[STAGE 3/7] TRANSFORM — Normalizando {len(raw_readings)} leituras...")

            ids_for_transform = raw_ids[:len(raw_readings)]
            normalized = self.transformer.transform_batch(raw_readings, ids_for_transform)

            loader.log_pipeline_run(
                run_id=run_id, stage="transform",
                status=PipelineStatus.SUCCESS,
                started_at=stage_start,
                records_input=len(raw_readings),
                records_output=len(normalized),
                records_failed=len(raw_readings) - len(normalized),
            )

            # ═══════════════════════════════════════
            #  STAGE 4: VALIDATE
            # ═══════════════════════════════════════
            stage_start = datetime.utcnow()
            logger.info(f"[STAGE 4/7] VALIDATE — Validando {len(normalized)} leituras normalizadas...")

            valid_readings, invalid_readings = self.validator.validate_batch(normalized)

            loader.log_pipeline_run(
                run_id=run_id, stage="validate",
                status=PipelineStatus.SUCCESS if not invalid_readings else PipelineStatus.PARTIAL,
                started_at=stage_start,
                records_input=len(normalized),
                records_output=len(valid_readings),
                records_failed=len(invalid_readings),
            )

            # ═══════════════════════════════════════
            #  STAGE 5: LOAD PROCESSED
            # ═══════════════════════════════════════
            stage_start = datetime.utcnow()
            logger.info(f"[STAGE 5/7] LOAD — Persistindo {len(normalized)} leituras processadas...")

            loaded_count = loader.insert_processed_readings(normalized)

            loader.log_pipeline_run(
                run_id=run_id, stage="load_processed",
                status=PipelineStatus.SUCCESS,
                started_at=stage_start,
                records_input=len(normalized),
                records_output=loaded_count,
            )

            # ═══════════════════════════════════════
            #  STAGE 6: ANOMALY DETECTION (CS03 — Pilar 2)
            # ═══════════════════════════════════════
            stage_start = datetime.utcnow()
            base_readings = valid_readings if valid_readings else normalized
            logger.info(f"[STAGE 6/7] ML — Avaliando anomalias em {len(base_readings)} leitura(s)...")

            anomaly_signals = self.anomaly_detector.predict_batch(base_readings)
            anomalies_found = sum(1 for s in anomaly_signals if s.is_anomaly)

            loader.log_pipeline_run(
                run_id=run_id, stage="anomaly_detection",
                status=PipelineStatus.SUCCESS,
                started_at=stage_start,
                records_input=len(base_readings),
                records_output=anomalies_found,
            )

            # ═══════════════════════════════════════
            #  STAGE 7: RULES ENGINE — eventos + status (CS03 — Pilar 2)
            # ═══════════════════════════════════════
            stage_start = datetime.utcnow()
            logger.info("[STAGE 7/7] RULES — Gerando eventos e atualizando status dos ativos...")

            events = self.rules_engine.process_batch(anomaly_signals)
            loader.log_events(events)

            status_updates = [
                {
                    "asset_tag": e.asset_tag,
                    "status": self.rules_engine.status_for(e),
                    "health_score": next(
                        (r.health_score for r in base_readings if r.asset_tag == e.asset_tag), ""
                    ),
                    "last_severity": e.severity,
                    "updated_at": e.created_at,
                }
                for e in events
            ]
            loader.update_asset_status(status_updates)
            loader.generate_snapshot(base_readings)

            loader.log_pipeline_run(
                run_id=run_id, stage="rules_engine",
                status=PipelineStatus.SUCCESS,
                started_at=stage_start,
                records_input=len(anomaly_signals),
                records_output=len(events),
            )

            # ═══════════════════════════════════════
            #  SUMÁRIO
            # ═══════════════════════════════════════
            elapsed = (datetime.utcnow() - cycle_start).total_seconds()
            logger.info(f"{'═'*60}")
            logger.info(f"[Pipeline] ✓ Ciclo #{self._run_count} concluído em {elapsed:.2f}s")
            logger.info(f"           Coletados   : {len(raw_readings)}")
            logger.info(f"           Normalizados: {len(normalized)}")
            logger.info(f"           Válidos     : {len(valid_readings)}")
            logger.info(f"           Inválidos   : {len(invalid_readings)}")
            logger.info(f"           Persistidos : {loaded_count}")
            logger.info(f"           Anomalias   : {anomalies_found}")
            logger.info(f"{'═'*60}")

        except Exception as e:
            cycle_start = cycle_start or datetime.utcnow()
            logger.error(f"[Pipeline] FALHA no ciclo #{self._run_count}: {e}", exc_info=True)
            try:
                loader.log_pipeline_run(
                    run_id=run_id, stage="pipeline",
                    status=PipelineStatus.FAILED,
                    started_at=cycle_start,
                    error_message=str(e),
                )
            except Exception:
                pass
        finally:
            loader.close()

    def run_once(self):
        """Executa um único ciclo (modo CI/teste)."""
        logger.info("[Orchestrator] Modo: ONCE")
        self.run_cycle()
        logger.info("[Orchestrator] Execução concluída.")

    def run_batch(self):
        """Executa em loop com intervalo definido via APScheduler."""
        interval = settings.batch_interval_seconds
        logger.info(f"[Orchestrator] Modo: BATCH | Intervalo: {interval}s")

        self.run_cycle()

        scheduler = BlockingScheduler(timezone="UTC")
        scheduler.add_job(
            func=self.run_cycle,
            trigger=IntervalTrigger(seconds=interval),
            id="motor_pipeline",
            name="Motor Data Pipeline",
            max_instances=1,
            misfire_grace_time=10,
        )

        logger.info("[Orchestrator] Scheduler iniciado. Pressione Ctrl+C para encerrar.")
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            logger.info("[Orchestrator] Pipeline encerrado pelo usuário.")
            scheduler.shutdown()


def main():
    orchestrator = PipelineOrchestrator()
    mode = settings.pipeline_mode.lower()

    if mode == "once":
        orchestrator.run_once()
    elif mode in ("batch", "streaming"):
        orchestrator.run_batch()
    else:
        logger.error(f"[Orchestrator] Modo inválido: {mode}. Use: once | batch | streaming")
        sys.exit(1)


if __name__ == "__main__":
    main()
