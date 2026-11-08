-- Métrica 1: Índice de Fidelización por Canción y Artista
CREATE OR REPLACE VIEW v_fidelizacion_cancion_artista WITH (security_invoker = true) AS
SELECT a.nombre AS artista, c.titulo AS cancion, c.nombre_album AS album,
       SUM(fr.total_reproducciones)::bigint AS total_reproducciones,
       SUM(fr.cantidad_oyentes)::bigint     AS total_oyentes,
       ROUND(SUM(fr.total_reproducciones)::numeric / NULLIF(SUM(fr.cantidad_oyentes), 0), 2) AS indice_fidelizacion,
       MIN(df.fecha) AS primera_medicion, MAX(df.fecha) AS ultima_medicion
FROM fact_reproducciones fr
JOIN dim_artista a ON a.id_artista = fr.id_artista
JOIN dim_cancion c ON c.id_cancion = fr.id_cancion
JOIN dim_fecha  df ON df.id_fecha  = fr.id_fecha
GROUP BY a.nombre, c.titulo, c.nombre_album;

-- Métrica 2a: historial SCD2 con variación vs. la versión anterior
CREATE OR REPLACE VIEW v_scd_historial_artista WITH (security_invoker = true) AS
SELECT a.id_artista, a.nombre AS artista, a.fecha_desde, a.fecha_hasta, a.es_actual,
       a.oyentes_globales, a.reproducciones_globales, a.nivel_popularidad,
       LAG(a.oyentes_globales) OVER w AS oyentes_version_anterior,
       LAG(a.nivel_popularidad) OVER w AS nivel_version_anterior,
       ROUND(100.0 * (a.oyentes_globales - LAG(a.oyentes_globales) OVER w) / NULLIF(LAG(a.oyentes_globales) OVER w, 0), 2) AS crecimiento_oyentes_pct,
       (SELECT ROUND(AVG(fr.total_reproducciones), 0)
          FROM fact_reproducciones fr JOIN dim_fecha df ON df.id_fecha = fr.id_fecha
          JOIN dim_artista a2 ON a2.id_artista = fr.id_artista
         WHERE a2.nombre = a.nombre AND df.fecha >= a.fecha_desde
           AND (a.fecha_hasta IS NULL OR df.fecha < a.fecha_hasta)) AS promedio_reproducciones_top_en_vigencia
FROM dim_artista a
WINDOW w AS (PARTITION BY a.nombre ORDER BY a.fecha_desde);

-- Métrica 2b: Tasa de Crecimiento Histórico (última versión histórica vs. vigente)
CREATE OR REPLACE VIEW v_crecimiento_historico_scd2 WITH (security_invoker = true) AS
SELECT cur.nombre AS artista,
       prev.fecha_desde AS fecha_desde_anterior, prev.fecha_hasta AS fecha_hasta_anterior,
       cur.fecha_desde  AS fecha_desde_vigente,
       prev.oyentes_globales AS oyentes_anterior, cur.oyentes_globales AS oyentes_vigente,
       prev.nivel_popularidad AS nivel_anterior,  cur.nivel_popularidad AS nivel_vigente,
       ROUND(100.0 * (cur.oyentes_globales - prev.oyentes_globales) / NULLIF(prev.oyentes_globales, 0), 2) AS tasa_crecimiento_oyentes_pct,
       ROUND(100.0 * (cur.reproducciones_globales - prev.reproducciones_globales) / NULLIF(prev.reproducciones_globales, 0), 2) AS tasa_crecimiento_reproducciones_pct,
       (cur.nivel_popularidad IS DISTINCT FROM prev.nivel_popularidad) AS cambio_de_nivel
FROM dim_artista cur
JOIN LATERAL (SELECT p.* FROM dim_artista p WHERE p.nombre = cur.nombre AND NOT p.es_actual
              ORDER BY p.fecha_desde DESC LIMIT 1) prev ON true
WHERE cur.es_actual;

-- Métrica 3: Densidad de repertorio en vivo y balance de gira por plaza
CREATE OR REPLACE VIEW v_densidad_repertorio_plaza WITH (security_invoker = true) AS
SELECT a.nombre AS artista, u.pais, u.ciudad,
       COUNT(*) FILTER (WHERE fc.show_realizado = 1) AS shows_realizados,
       COUNT(*) FILTER (WHERE fc.show_realizado = 0) AS shows_futuros,
       COUNT(*) AS shows_totales,
       ROUND(AVG(NULLIF(fc.cantidad_canciones_tocadas, 0)) FILTER (WHERE fc.show_realizado = 1), 1) AS densidad_repertorio_promedio,
       MIN(df.fecha) AS primer_show, MAX(df.fecha) AS ultimo_show
FROM fact_conciertos fc
JOIN dim_artista   a  ON a.id_artista   = fc.id_artista
JOIN dim_ubicacion u  ON u.id_ubicacion = fc.id_ubicacion
JOIN dim_fecha     df ON df.id_fecha    = fc.id_fecha
GROUP BY a.nombre, u.pais, u.ciudad;

-- Métrica 4: Tracción digital por concierto (reproducciones acumuladas / show)
CREATE OR REPLACE VIEW v_traccion_digital_concierto WITH (security_invoker = true) AS
WITH ult AS (
  SELECT a.nombre, MAX(fr.id_fecha) AS id_fecha_ult
  FROM fact_reproducciones fr JOIN dim_artista a ON a.id_artista = fr.id_artista GROUP BY a.nombre
), repro AS (
  SELECT a.nombre, SUM(fr.total_reproducciones)::bigint AS reproducciones_acumuladas, SUM(fr.cantidad_oyentes)::bigint AS oyentes_top
  FROM fact_reproducciones fr JOIN dim_artista a ON a.id_artista = fr.id_artista
  JOIN ult ON ult.nombre = a.nombre AND ult.id_fecha_ult = fr.id_fecha GROUP BY a.nombre
), shows AS (
  SELECT a.nombre, COUNT(*) AS shows_totales, COUNT(*) FILTER (WHERE fc.show_realizado = 1) AS shows_realizados
  FROM fact_conciertos fc JOIN dim_artista a ON a.id_artista = fc.id_artista GROUP BY a.nombre
)
SELECT r.nombre AS artista, to_date(ult.id_fecha_ult::text, 'YYYYMMDD') AS fecha_ultima_medicion,
       r.reproducciones_acumuladas, COALESCE(s.shows_totales, 0) AS shows_totales, COALESCE(s.shows_realizados, 0) AS shows_realizados,
       ROUND(r.reproducciones_acumuladas::numeric / NULLIF(s.shows_totales, 0), 0) AS indice_traccion_por_show
FROM repro r JOIN ult ON ult.nombre = r.nombre LEFT JOIN shows s ON s.nombre = r.nombre;
