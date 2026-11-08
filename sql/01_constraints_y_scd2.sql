-- Modelo estrella: columnas SCD Tipo 2, claves únicas y FKs (las PK/FK base ya existen).
ALTER TABLE dim_artista ADD COLUMN IF NOT EXISTS reproducciones_globales bigint;
ALTER TABLE dim_artista ADD COLUMN IF NOT EXISTS mbid text;
ALTER TABLE dim_cancion ADD COLUMN IF NOT EXISTS artista text;
ALTER TABLE dim_fecha   ADD COLUMN IF NOT EXISTS fecha date;

ALTER TABLE dim_artista ADD CONSTRAINT uq_dim_artista_version UNIQUE (nombre, fecha_desde);
CREATE UNIQUE INDEX uq_dim_artista_vigente ON dim_artista (nombre) WHERE es_actual;
ALTER TABLE dim_artista ADD CONSTRAINT ck_dim_artista_scd CHECK (
  (es_actual AND fecha_hasta IS NULL) OR (NOT es_actual AND fecha_hasta IS NOT NULL AND fecha_hasta >= fecha_desde));
ALTER TABLE dim_ubicacion ADD CONSTRAINT uq_dim_ubicacion UNIQUE (ciudad, pais);
ALTER TABLE dim_cancion   ADD CONSTRAINT uq_dim_cancion UNIQUE (titulo, artista);
ALTER TABLE dim_fecha     ADD CONSTRAINT uq_dim_fecha_fecha UNIQUE (fecha);
ALTER TABLE fact_reproducciones ADD CONSTRAINT uq_fact_reproducciones UNIQUE (id_artista, id_fecha, id_cancion);
ALTER TABLE fact_conciertos     ADD CONSTRAINT uq_fact_conciertos UNIQUE (id_artista, id_fecha, id_ubicacion),
                                ADD CONSTRAINT ck_show_realizado CHECK (show_realizado IN (0,1));
