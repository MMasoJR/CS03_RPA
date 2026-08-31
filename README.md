# Motor Monitoring — Pipeline RPA

> **Sprint: Automação Inicial para Coleta, Registro e Atualização de Dados de Ativos**  
> Sistema de Monitoramento Preditivo de Motores Elétricos Industriais  
> FIAP — Faculdade de Informática e Administração Paulista

---

## Visão Rápida

Pipeline de automação RPA que coleta dados de sensores IoT e sistemas legados (CSV SCADA), normaliza unidades, calcula indicadores de saúde e persiste tudo em PostgreSQL — **sem intervenção manual**.

```
[IoT Sensor] ──► [Collect] ──► [Transform] ──► [Validate] ──► [Load] ──► [PostgreSQL]
[CSV Legacy] ──►                                                          [+ Logs]
```

## Modos de Execução

| Variável | Valor | Comportamento |
|---|---|---|
| `PIPELINE_MODE` | `batch` | Loop automático a cada N segundos |
| `PIPELINE_MODE` | `once` | Uma execução e encerra |
| `BATCH_INTERVAL_SECONDS` | `30` | Intervalo entre ciclos |

## Estrutura

```
src/
├── collector/    IoT + CSV simulados
├── transformer/  Conversão de unidades + Health Score
├── validator/    Hard limits + cross-validation
├── loader/       Persistência no PostgreSQL
└── pipeline/     Orquestrador RPA (APScheduler)
```

## Documentação Completa

Ver [`docs/technical_document.md`](docs/technical_document.md) para:
- Arquitetura detalhada com diagramas
- Fluxo de dados completo
- Justificativas de cada decisão técnica
- Modelo de dados comentado
- Exemplos de queries
