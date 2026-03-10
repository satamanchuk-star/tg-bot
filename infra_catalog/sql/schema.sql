-- Схема таблицы infra_objects для PostgreSQL

CREATE TABLE IF NOT EXISTS infra_objects (
    id              BIGSERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    category        TEXT NOT NULL,
    subcategory     TEXT NOT NULL,
    address         TEXT,
    phone           TEXT,
    website         TEXT,
    work_time       TEXT,
    description     TEXT,
    lat             DOUBLE PRECISION NOT NULL,
    lon             DOUBLE PRECISION NOT NULL,
    distance_km     DOUBLE PRECISION NOT NULL,
    source          TEXT NOT NULL,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Индексы
CREATE INDEX IF NOT EXISTS idx_infra_objects_category ON infra_objects (category);
CREATE INDEX IF NOT EXISTS idx_infra_objects_subcategory ON infra_objects (subcategory);
CREATE INDEX IF NOT EXISTS idx_infra_objects_is_active ON infra_objects (is_active);
CREATE INDEX IF NOT EXISTS idx_infra_objects_distance_km ON infra_objects (distance_km);
CREATE INDEX IF NOT EXISTS idx_infra_objects_coords ON infra_objects (lat, lon);

-- Рекомендация: уникальность обеспечивается на ETL-уровне (дедупликация).
-- При необходимости можно добавить:
-- CREATE UNIQUE INDEX IF NOT EXISTS idx_infra_objects_uniq
--     ON infra_objects (lower(name), lower(coalesce(address, '')));

-- Триггер обновления updated_at
CREATE OR REPLACE FUNCTION update_infra_objects_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_infra_objects_updated_at ON infra_objects;
CREATE TRIGGER trg_infra_objects_updated_at
    BEFORE UPDATE ON infra_objects
    FOR EACH ROW
    EXECUTE FUNCTION update_infra_objects_updated_at();
