-- scripts/init_schema.sql
-- Script de inicialização executado automaticamente pelo Docker
-- na primeira vez que o container PostgreSQL sobe.

-- Extensões úteis
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_stat_statements";

-- Comentário no banco
COMMENT ON DATABASE motor_monitoring IS
  'Base de dados do sistema de monitoramento e manutenção preditiva de motores elétricos.';

-- Índices adicionais de performance (criados após o SQLAlchemy criar as tabelas)
-- Nota: O SQLAlchemy cria as tabelas via init_database(). Este script apenas
-- prepara extensões e configurações iniciais.
