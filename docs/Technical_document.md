# CS03 — Inteligência Operacional e Apoio à Decisão

## Visão geral

Nesta sprint estendi o pipeline RPA construído na CS02 com uma camada
analítica, dividida nos dois pilares propostos: um robô de web scraping
para captura de dados técnicos complementares, e um fluxo automatizado de
registro de eventos e atualização de status dos ativos, integrado ao
modelo de inteligência (ML/DL) de detecção de anomalias.

```
COLLECT → LOAD RAW → TRANSFORM → VALIDATE → LOAD PROCESSED
    → [STAGE 6] ANOMALY DETECTION (Isolation Forest)
    → [STAGE 7] RULES ENGINE (eventos + status + snapshot)
                                        ▲
                        [Scraper WEG W22] (contexto técnico externo)
```

Toda a persistência passou a ser feita de verdade em PostgreSQL (hospedado
no Supabase), via SQLAlchemy — na CS02 o `DataLoader` gravava em CSV,
apesar dos models já existirem prontos em `src/models/database.py`; agora
esses models são usados de fato.

## Pilar 1 — Web Scraping (`src/scraper/weg_catalog_scraper.py`)

Implementei um robô com Playwright que navega no catálogo técnico da WEG,
linha W22, e extrai três informações complementares aos dados da placa
física do motor:

- limites de tolerância de temperatura por classe de isolamento;
- curva de rendimento nominal (25/50/75/100% de carga);
- um guia básico de troubleshooting por sintoma (temperatura, vibração,
  sobrecarga de corrente, desvio de RPM, fator de potência).

Se o site estiver indisponível ou o layout mudar, o robô cai num
fallback com valores normativos conhecidos (IEC 60034-1 para os limites
de temperatura), garantindo que o scraping nunca derrube o restante do
pipeline por erro de execução — um dos critérios de avaliação da sprint.

A evidência de coleta fica salva em `data/weg_w22_technical_context.json`
e `.csv`. Rodo esse robô antes do pipeline (o arquivo fica em disco e é
reaproveitado nos ciclos seguintes):

```bash
python -m src.scraper.weg_catalog_scraper
```

## Modelo de ML/DL (`src/intelligence/anomaly_model.py`)

Como ainda não existia um modelo de ML plugado ao pipeline (o
`health_score` da CS02 é uma fórmula de penalidades, não algo treinado),
adicionei um **Isolation Forest**, treinado no histórico de
`data/processed_readings.csv`/`processed_readings` (features:
`temperature_c`, `vibration_mm_s`, `current_load_pct`,
`rpm_deviation_pct`, `power_factor`, `health_score`), para detectar
combinações anômalas que os limiares fixos do `data_validator.py` não
capturam isoladamente.

Enquanto não há histórico suficiente (menos de 30 amostras), o modelo cai
num fallback baseado em regra sobre o `health_score`, para o pipeline
nunca ficar sem sinal de anomalia disponível. O modelo treinado é
persistido em `data/models/anomaly_model.joblib` e pode ser retreinado
sob demanda (não a cada ciclo, para não pesar no loop de 30s):

```bash
python -m scripts.retrain_anomaly_model
```

## Pilar 2 — Automação do Ciclo de Eventos (`src/automations/rules_engine.py`)

O motor de regras recebe o sinal do modelo de anomalia (ativo, métrica
dominante, valor, se é anomalia) e decide a severidade cruzando com o
contexto técnico coletado no Pilar 1: para temperatura, só classifico
como crítico se o valor ultrapassar o limite real da classe de isolamento
do motor (não um número fixo arbitrário); para vibração, corrente e RPM,
uso os mesmos limiares normativos já definidos no `normalizer.py` da
CS02 (ISO 10816-3, percentual de carga nominal), mantendo uma única fonte
de verdade para esses valores em vez de duplicá-los.

A partir da severidade, o motor sugere uma ação inicial de manutenção
cruzando a métrica fora do padrão com o guia de troubleshooting coletado
pelo scraper.

## Persistência (PostgreSQL/Supabase via SQLAlchemy)

Reescrevi o `db_loader.py` para gravar de fato no banco, usando os models
de `src/models/database.py`:

- **Tabela nova**: `asset_events` — log histórico de eventos/alertas, com
  FK para `assets` e o contexto técnico usado na decisão.
- **Reaproveitei** o campo `Asset.status` (já existia no model, com o
  enum `OPERATIONAL/WARNING/CRITICAL/...`), que passou a ser atualizado
  de verdade a cada ciclo.
- **Criação automática de ativo**: a primeira leitura de um `asset_tag`
  novo cria a linha em `assets` automaticamente, usando os dados nominais
  do catálogo de motores.
- Cada método faz `commit()` no sucesso e `rollback()` em caso de erro,
  para uma falha num evento não deixar o banco num estado parcial.

## Simulação de dados (`scripts/seed_65_readings.py`)

Pra validar a parte de automação sem depender do APScheduler rodando em
batch por horas, escrevi um script que gera 65 leituras simuladas (com
cerca de 23% contendo uma anomalia proposital — temperatura crítica,
vibração crítica, sobrecarga de corrente, desvio de RPM ou fator de
potência baixo) e as leva pela pipeline real inteira: transformação,
validação, carga, detecção de anomalia e motor de regras. É a forma mais
rápida de comprovar que o ciclo completo — do sensor ao alerta registrado
no banco — funciona de ponta a ponta sem intervenção manual.

```bash
python -m scripts.seed_65_readings
```

## Testes (`tests/test_pipeline_local.py`)

Um smoke test cobre o pipeline completo (collect → transform → validate
→ load → detecção de anomalia → motor de regras) usando SQLite in-memory,
sem depender do Supabase estar acessível. Além dele, testes unitários
cobrem: o fallback do detector de anomalias sem modelo treinado, a
classificação de severidade por métrica (temperatura, vibração, corrente,
RPM), a sugestão de ação, a persistência de eventos/status no banco, e o
fallback do scraper quando o site está fora do ar.

```bash
pytest tests/test_pipeline_local.py -v
```

## Tratamento de exceções

- Scraper: `try/except` por etapa de extração, com fallback normativo —
  nunca propaga exceção para o restante do pipeline.
- Modelo de anomalia: fallback automático baseado em regra quando não há
  dado de treino suficiente.
- Motor de regras e loader: seguem o mesmo padrão do restante do
  pipeline — o `run_cycle()` do orquestrador já tem um `try/except`
  externo que registra a falha (`PipelineStatus.FAILED`) sem derrubar o
  processo, e a sessão do banco é sempre fechada no `finally`.

## Como rodar

```bash
pip install -r requirements.txt
playwright install chromium

# .env com DB_HOST/DB_PORT/DB_USER/DB_PASSWORD/DB_NAME apontando pro
# Postgres (Supabase, nesse caso). init_database() cria as tabelas
# automaticamente na primeira execução.

python -m src.scraper.weg_catalog_scraper       # Pilar 1 — contexto técnico
python -m src.pipeline.orchestrator             # ciclo completo (ML + regras já integrados)
python -m scripts.seed_65_readings              # simula histórico + anomalias pra teste
python -m scripts.retrain_anomaly_model         # depois de acumular leituras, treina o Isolation Forest de verdade
streamlit run src/dashboard/app.py              # painel com status e eventos por ativo
pytest tests/test_pipeline_local.py -v          # smoke test + testes unitários
```

## Limitações conhecidas

- O modelo de ML só passa a usar o Isolation Forest de fato depois de
  acumular pelo menos 30 leituras processadas; antes disso, funciona no
  fallback baseado em regra (documentado acima).
- O scraper extrai dados técnicos genéricos da linha W22, não
  específicos por número de série — para uma versão futura, dava pra
  cruzar pelo campo `model` de cada ativo cadastrado.
