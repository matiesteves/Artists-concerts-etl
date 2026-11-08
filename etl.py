"""ETL de artistas y conciertos hacia un Data Warehouse en Supabase (esquema estrella).

Fuentes : Last.fm (estadísticas y top canciones), Setlist.fm (shows pasados),
          Ticketmaster Discovery (shows futuros).
Destino : dim_artista (SCD Tipo 2), dim_cancion, dim_ubicacion, dim_fecha,
          fact_reproducciones, fact_conciertos.

El script es idempotente: las cargas de hechos usan UPSERT sobre las claves únicas
del modelo, por lo que correrlo varias veces el mismo día no genera duplicados.
"""

import os
import sys
import time
from datetime import date, datetime

import requests
from supabase import Client, create_client

# ==========================================
# CONFIGURACIÓN
# ==========================================
ARTISTAS = [
    {"nombre": "Bad Bunny", "mbid": "89aa5ecb-59ad-46f5-b3eb-2d424e941f19", "genero": "Urbano"},
    {"nombre": "Karol G", "mbid": "de222709-0926-4f6c-b89c-2142b1745238", "genero": "Urbano"},
    {"nombre": "Feid", "mbid": "e2a38a67-c21b-444f-8558-87822e4f8f50", "genero": "Urbano"},
    {"nombre": "Rauw Alejandro", "mbid": "5f810209-6463-444f-8a6b-a30992d65b73", "genero": "Urbano"},
    {"nombre": "Coldplay", "mbid": "cc197bad-dc9c-440d-a5b5-d52ba2e14234", "genero": "Pop Rock"},
    {"nombre": "Taylor Swift", "mbid": "20244d07-534f-4eff-b4d4-930878889970", "genero": "Pop"},
]

TOP_CANCIONES = 5
SHOWS_PASADOS = 5
SHOWS_FUTUROS = 3
UMBRAL_CAMBIO_OYENTES = 0.01  # una variación >= 1% abre una nueva versión SCD2
HTTP_TIMEOUT = 20

LASTFM_URL = "https://ws.audioscrobbler.com/2.0/"
SETLIST_URL = "https://api.setlist.fm/rest/1.0/artist/{mbid}/setlists"
TICKETMASTER_URL = "https://app.ticketmaster.com/discovery/v2/events.json"

HOY = date.today()


def leer_entorno(nombre: str) -> str:
    valor = os.environ.get(nombre)
    if not valor:
        sys.exit(f"❌ Falta la variable de entorno {nombre}")
    return valor


# ==========================================
# UTILIDADES
# ==========================================
def get_json(url: str, **kwargs) -> dict:
    """GET con 3 reintentos ante errores de red o rate limit. Devuelve {} si falla."""
    for intento in range(3):
        try:
            resp = requests.get(url, timeout=HTTP_TIMEOUT, **kwargs)
            if resp.status_code == 429:
                time.sleep(2 * (intento + 1))
                continue
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            print(f"   ⚠️ {url.split('?')[0]} intento {intento + 1}/3: {exc}")
            time.sleep(1.5 * (intento + 1))
    return {}


def clasificar_popularidad(oyentes: int) -> str:
    if oyentes >= 3_500_000:
        return "Fenómeno Global (Tier S)"
    if oyentes >= 2_000_000:
        return "Headliner Internacional (Tier A)"
    return "Consolidado (Tier B)"


def id_fecha_de(d: date) -> int:
    return int(d.strftime("%Y%m%d"))


# ==========================================
# EXTRACCIÓN
# ==========================================
def extraer_lastfm_artista(nombre: str, api_key: str) -> dict:
    data = get_json(
        LASTFM_URL,
        params={"method": "artist.getinfo", "artist": nombre, "api_key": api_key, "format": "json"},
    )
    stats = data.get("artist", {}).get("stats", {})
    return {
        "oyentes": int(stats.get("listeners") or 0),
        "reproducciones": int(stats.get("playcount") or 0),
    }


def extraer_lastfm_top_canciones(nombre: str, api_key: str) -> list[dict]:
    data = get_json(
        LASTFM_URL,
        params={
            "method": "artist.gettoptracks", "artist": nombre,
            "api_key": api_key, "format": "json", "limit": TOP_CANCIONES,
        },
    )
    tracks = data.get("toptracks", {}).get("track", [])[:TOP_CANCIONES]
    return [
        {
            "titulo": t["name"],
            "oyentes": int(t.get("listeners") or 0),
            "reproducciones": int(t.get("playcount") or 0),
        }
        for t in tracks
    ]


def extraer_album(nombre: str, titulo: str, api_key: str) -> str:
    data = get_json(
        LASTFM_URL,
        params={
            "method": "track.getInfo", "artist": nombre, "track": titulo,
            "api_key": api_key, "format": "json",
        },
    )
    return data.get("track", {}).get("album", {}).get("title") or "Sencillo / Sin Álbum"


def extraer_shows_pasados(artista: dict, api_key: str) -> list[dict]:
    """Últimos shows realizados (Setlist.fm) con el conteo real de canciones tocadas."""
    data = get_json(
        SETLIST_URL.format(mbid=artista["mbid"]),
        params={"p": 1},
        headers={"Accept": "application/json", "x-api-key": api_key},
    )
    shows = []
    for show in data.get("setlist", []):
        try:
            fecha = datetime.strptime(show.get("eventDate", ""), "%d-%m-%Y").date()
        except ValueError:
            continue
        if fecha > HOY:
            continue
        ciudad = show.get("venue", {}).get("city", {})
        if not ciudad.get("name"):
            continue
        bloques = show.get("sets", {}).get("set", [])
        canciones = sum(len(b.get("song", [])) for b in bloques)
        shows.append({
            "fecha": fecha,
            "ciudad": ciudad["name"],
            "pais": ciudad.get("country", {}).get("name", "Desconocido"),
            "cantidad_canciones_tocadas": canciones,
            "show_realizado": 1,
        })
        if len(shows) == SHOWS_PASADOS:
            break
    return shows


def extraer_shows_futuros(artista: dict, api_key: str) -> list[dict]:
    """Próximos shows (Ticketmaster). Omite eventos virtuales o sin recinto físico."""
    data = get_json(
        TICKETMASTER_URL,
        params={
            "keyword": artista["nombre"], "classificationName": "music",
            "sort": "date,asc", "size": 30, "apikey": api_key,
        },
    )
    shows = []
    for evento in data.get("_embedded", {}).get("events", []):
        embebido = evento.get("_embedded", {})
        venues = embebido.get("venues", [])
        if not venues:
            continue  # evento virtual / paquete VIP / sin recinto

        atracciones = [a.get("name", "").lower() for a in embebido.get("attractions", [])]
        if atracciones and artista["nombre"].lower() not in atracciones:
            continue  # coincidencia por keyword de otro artista (tributos, etc.)

        venue = venues[0]
        ciudad = venue.get("city", {}).get("name")
        fecha_txt = evento.get("dates", {}).get("start", {}).get("localDate")
        if not ciudad or not fecha_txt:
            continue
        try:
            fecha = date.fromisoformat(fecha_txt)
        except ValueError:
            continue
        if fecha <= HOY:
            continue
        shows.append({
            "fecha": fecha,
            "ciudad": ciudad,
            "pais": venue.get("country", {}).get("name", "Desconocido"),
            "cantidad_canciones_tocadas": 0,
            "show_realizado": 0,
        })
        if len(shows) == SHOWS_FUTUROS:
            break
    return shows


# ==========================================
# CARGA: DIMENSIONES
# ==========================================
def versiones_artista(sb: Client, nombre: str) -> list[dict]:
    res = sb.table("dim_artista").select("*").eq("nombre", nombre).order("fecha_desde").execute()
    return res.data


def resolver_version(versiones: list[dict], fecha: date) -> int:
    """Devuelve el id_artista de la versión SCD2 vigente en `fecha` (o la más cercana)."""
    for v in versiones:
        desde = date.fromisoformat(v["fecha_desde"])
        hasta = date.fromisoformat(v["fecha_hasta"]) if v["fecha_hasta"] else None
        if desde <= fecha and (hasta is None or fecha < hasta):
            return v["id_artista"]
    return versiones[0]["id_artista"] if fecha < date.fromisoformat(versiones[0]["fecha_desde"]) else versiones[-1]["id_artista"]


def aplicar_scd2_artista(sb: Client, artista: dict, stats: dict) -> list[dict]:
    """SCD Tipo 2: cierra la fila vigente e inserta una nueva si cambió la popularidad."""
    nombre = artista["nombre"]
    oyentes, reproducciones = stats["oyentes"], stats["reproducciones"]
    nivel = clasificar_popularidad(oyentes)

    nueva = {
        "nombre": nombre, "genero_principal": artista["genero"], "mbid": artista["mbid"],
        "oyentes_globales": oyentes, "reproducciones_globales": reproducciones,
        "nivel_popularidad": nivel, "fecha_desde": str(HOY), "fecha_hasta": None, "es_actual": True,
    }
    versiones = versiones_artista(sb, nombre)
    actual = next((v for v in versiones if v["es_actual"]), None)

    if actual is None:
        sb.table("dim_artista").insert(nueva).execute()
        print(f"   🆕 Versión inicial SCD2 ({oyentes:,} oyentes, {nivel})")
    elif oyentes > 0:
        previos = actual.get("oyentes_globales") or 0
        cambio_relativo = abs(oyentes - previos) / previos if previos else 1.0
        if nivel != actual.get("nivel_popularidad") or cambio_relativo >= UMBRAL_CAMBIO_OYENTES:
            if actual["fecha_desde"] == str(HOY):
                # Ya hay una versión abierta hoy: se corrige en lugar de versionar de nuevo
                sb.table("dim_artista").update({
                    "oyentes_globales": oyentes, "reproducciones_globales": reproducciones,
                    "nivel_popularidad": nivel, "mbid": artista["mbid"],
                }).eq("id_artista", actual["id_artista"]).execute()
            else:
                sb.table("dim_artista").update(
                    {"fecha_hasta": str(HOY), "es_actual": False}
                ).eq("id_artista", actual["id_artista"]).execute()
                try:
                    sb.table("dim_artista").insert(nueva).execute()
                except Exception:
                    # Si falla el alta, reabrimos la versión para no dejar al artista sin fila vigente
                    sb.table("dim_artista").update(
                        {"fecha_hasta": None, "es_actual": True}
                    ).eq("id_artista", actual["id_artista"]).execute()
                    raise
            print(f"   🔁 SCD2: {previos:,} → {oyentes:,} oyentes ({actual['nivel_popularidad']} → {nivel})")
        else:
            print(f"   ✔️ Sin cambios significativos ({oyentes:,} oyentes, {nivel})")
    return versiones_artista(sb, nombre)


def asegurar_fecha(sb: Client, d: date, cache: set) -> int:
    id_f = id_fecha_de(d)
    if id_f not in cache:
        sb.table("dim_fecha").upsert({
            "id_fecha": id_f, "anio": d.year, "mes": d.month, "dia": d.day,
            "trimestre": (d.month - 1) // 3 + 1, "fecha": str(d),
        }, on_conflict="id_fecha").execute()
        cache.add(id_f)
    return id_f


def asegurar_ubicacion(sb: Client, ciudad: str, pais: str, cache: dict) -> int:
    clave = (ciudad, pais)
    if clave not in cache:
        sb.table("dim_ubicacion").upsert(
            {"ciudad": ciudad, "pais": pais}, on_conflict="ciudad,pais"
        ).execute()
        res = sb.table("dim_ubicacion").select("id_ubicacion").eq("ciudad", ciudad).eq("pais", pais).execute()
        cache[clave] = res.data[0]["id_ubicacion"]
    return cache[clave]


def asegurar_cancion(sb: Client, artista: str, titulo: str, api_key: str) -> int:
    res = sb.table("dim_cancion").select("id_cancion").eq("titulo", titulo).eq("artista", artista).execute()
    if res.data:
        return res.data[0]["id_cancion"]
    album = extraer_album(artista, titulo, api_key)
    ins = sb.table("dim_cancion").insert(
        {"titulo": titulo, "nombre_album": album, "artista": artista}
    ).execute()
    return ins.data[0]["id_cancion"]


# ==========================================
# CARGA: HECHOS
# ==========================================
def cargar_reproducciones(sb, artista, id_artista, canciones, id_fecha_hoy, lastfm_key):
    filas = []
    for c in canciones:
        filas.append({
            "id_artista": id_artista,
            "id_fecha": id_fecha_hoy,
            "id_cancion": asegurar_cancion(sb, artista["nombre"], c["titulo"], lastfm_key),
            "cantidad_oyentes": c["oyentes"],
            "total_reproducciones": c["reproducciones"],
        })
    if filas:
        sb.table("fact_reproducciones").upsert(filas, on_conflict="id_artista,id_fecha,id_cancion").execute()
    print(f"   🎵 {len(filas)} canciones cargadas")


def cargar_conciertos(sb, versiones, shows, cache_fechas, cache_ubi):
    filas = {}
    for s in shows:
        fila = {
            "id_artista": resolver_version(versiones, s["fecha"]),
            "id_fecha": asegurar_fecha(sb, s["fecha"], cache_fechas),
            "id_ubicacion": asegurar_ubicacion(sb, s["ciudad"], s["pais"], cache_ubi),
            "cantidad_canciones_tocadas": s["cantidad_canciones_tocadas"],
            "show_realizado": s["show_realizado"],
        }
        filas[(fila["id_artista"], fila["id_fecha"], fila["id_ubicacion"])] = fila
    if filas:
        sb.table("fact_conciertos").upsert(
            list(filas.values()), on_conflict="id_artista,id_fecha,id_ubicacion"
        ).execute()
    print(f"   🎤 {len(filas)} conciertos cargados")


# ==========================================
# ORQUESTACIÓN
# ==========================================
def procesar_artista(sb, artista, claves, cache_fechas, cache_ubi):
    print(f"\n🎧 {artista['nombre']}")
    stats = extraer_lastfm_artista(artista["nombre"], claves["lastfm"])
    versiones = aplicar_scd2_artista(sb, artista, stats) if stats["oyentes"] > 0 else versiones_artista(sb, artista["nombre"])
    if not versiones:
        raise RuntimeError("Last.fm no devolvió estadísticas y el artista no existe aún en dim_artista")
    id_actual = resolver_version(versiones, HOY)

    canciones = extraer_lastfm_top_canciones(artista["nombre"], claves["lastfm"])
    id_fecha_hoy = asegurar_fecha(sb, HOY, cache_fechas)
    cargar_reproducciones(sb, artista, id_actual, canciones, id_fecha_hoy, claves["lastfm"])

    shows = extraer_shows_pasados(artista, claves["setlist"])
    time.sleep(0.6)  # Setlist.fm: máximo ~2 requests/segundo
    shows += extraer_shows_futuros(artista, claves["ticketmaster"])
    cargar_conciertos(sb, versiones, shows, cache_fechas, cache_ubi)


def main() -> int:
    claves = {
        "lastfm": leer_entorno("LASTFM_KEY"),
        "setlist": leer_entorno("SETLISTFM_KEY"),
        "ticketmaster": leer_entorno("TICKETMASTER_KEY"),
    }
    sb = create_client(leer_entorno("SUPABASE_URL"), leer_entorno("SUPABASE_KEY"))

    print(f"🚀 ETL {HOY} — {len(ARTISTAS)} artistas")
    cache_fechas: set = set()
    cache_ubi: dict = {}
    fallidos = []
    for artista in ARTISTAS:
        try:
            procesar_artista(sb, artista, claves, cache_fechas, cache_ubi)
        except Exception as exc:  # un artista con error no debe frenar a los demás
            print(f"   ❌ Error con {artista['nombre']}: {exc}")
            fallidos.append(artista["nombre"])

    if fallidos:
        print(f"\n⚠️ ETL terminó con errores en: {', '.join(fallidos)}")
        return 1
    print("\n✅ ETL completado: Data Warehouse actualizado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
