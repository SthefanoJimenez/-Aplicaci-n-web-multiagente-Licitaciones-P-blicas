-- HU-008: alternativa manual para el responsable de la base de cuentas en Neon.
-- No ejecutar sobre producción sin respaldo. Si había RUC duplicados, revisar las cuentas
-- antes de crear el índice; este script revierte la transacción si el índice no puede crearse.
-- Las empresas existentes conservan NULL hasta completar su RUC; no se mezclan cuentas.
BEGIN;
ALTER TABLE empresas ADD COLUMN IF NOT EXISTS ruc VARCHAR(11);
UPDATE empresas SET ruc = NULL WHERE ruc = '';
CREATE UNIQUE INDEX IF NOT EXISTS ix_empresas_ruc ON empresas (ruc);
COMMIT;
