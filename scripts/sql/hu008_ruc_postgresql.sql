-- HU-008: alternativa manual para el responsable de la base de cuentas en Neon.
-- La aplicación aplica esta misma migración aditiva al inicializar la base de cuentas.
-- Las empresas existentes conservan NULL hasta completar su RUC; no se mezclan cuentas.
BEGIN;
ALTER TABLE empresas ADD COLUMN IF NOT EXISTS ruc VARCHAR(11);
CREATE UNIQUE INDEX IF NOT EXISTS ix_empresas_ruc ON empresas (ruc);
COMMIT;
