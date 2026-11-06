from datetime import date
import json
import os
import time
import requests
from supabase import Client, create_client

# ==========================================
# LLAVES MAESTRAS DESDE LA CAJA FUERTE DE GITHUB
# ==========================================
LASTFM_KEY = os.environ.get("LASTFM_KEY")
SETLIST_KEY = os.environ.get("SETLIST_KEY")
TICKETMASTER_KEY = os.environ.get("TICKETMASTER_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# Iniciamos el cliente de Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

ARTISTA = "Bad Bunny"
MBID_ARTISTA = "89aa5ecb-59ad-46f5-b3eb-2d424e941f19"
fecha_hoy = str(date.today())

print(f"🚀 INICIANDO CAMIÓN ETL PARA {ARTISTA.upper()} - {fecha_hoy}\n")

# ==========================================
# 1️⃣ EXTRACCIÓN Y TRANSFORMACIÓN
# ==========================================
print("📦 Extrayendo y transformando datos de APIs...")

# -- A. LAST.FM (Estadísticas globales del artista para SCD Tipo 2) --
url_artist_info = f"http://ws.audioscrobbler.com/2.0/?method=artist.getinfo&artist={ARTISTA}&api_key={LASTFM_KEY}&format=json"
try:
  stats_artista = (
      requests.get(url_artist_info, timeout=15)
      .json()
      .get("artist", {})
      .get("stats", {})
  )
  oyentes_globales_hoy = int(stats_artista.get("listeners", 0))
except Exception:
  oyentes_globales_hoy = 0

if oyentes_globales_hoy >= 3500000:
  nivel_popularidad_hoy = "Fenómeno Global (Tier S)"
elif oyentes_globales_hoy >= 2000000:
  nivel_popularidad_hoy = "Headliner Internacional (Tier A)"
else:
  nivel_popularidad_hoy = "Consolidado (Tier B)"

# -- B. LAST.FM (Top 5 Canciones) --
url_top = f"http://ws.audioscrobbler.com/2.0/?method=artist.gettoptracks&artist={ARTISTA}&api_key={LASTFM_KEY}&format=json"
lista_canciones = (
    requests.get(url_top, timeout=15)
    .json()
    .get("toptracks", {})
    .get("track", [])[:5]
)
paquete_canciones = []

for track in lista_canciones:
  nombre_cancion = track["name"]
  url_info = f"http://ws.audioscrobbler.com/2.0/?method=track.getInfo&api_key={LASTFM_KEY}&artist={ARTISTA}&track={nombre_cancion}&format=json"
  try:
    nombre_album = requests.get(url_info, timeout=15).json()["track"]["album"][
        "title"
    ]
  except Exception:
    nombre_album = "Sencillo / Sin Álbum"

  paquete_canciones.append({
      "titulo": nombre_cancion,
      "album": nombre_album,
      "oyentes": int(track.get("listeners", 0)),
      "reproducciones": int(track.get("playcount", 0)),
  })

# -- C. SETLIST.FM (Pasado - Últimos 5 shows realizados con conteo real de canciones) --
url_setlist = f"https://api.setlist.fm/rest/1.0/artist/{MBID_ARTISTA}/setlists?p=1"
datos_setlist = requests.get(
    url_setlist,
    headers={"Accept": "application/json", "x-api-key": SETLIST_KEY},
    timeout=15,
).json()

paquete_conciertos_pasados = []
shows_a_traer = 5
contador = 0

for show in datos_setlist.get("setlist", []):
  fecha_orig = show.get("eventDate", "")
  if (
      "2024" in fecha_orig
      or "2025" in fecha_orig
      or "2026" in fecha_orig
  ):
    partes = fecha_orig.split("-")
    venue_data = show.get("venue", {})
    city_data = venue_data.get("city", {})
    ciudad_nombre = city_data.get("name")
    if not ciudad_nombre:
      continue

    # Contamos cuántas canciones reales tuvo el setlist del show
    sets_lista = show.get("sets", {}).get("set", [])
    total_canciones_show = sum(
        len(bloque.get("song", [])) for bloque in sets_lista
    )

    paquete_conciertos_pasados.append({
        "fecha": f"{partes[2]}-{partes[1]}-{partes[0]}",
        "ciudad": ciudad_nombre,
        "pais": city_data.get("country", {}).get("name", "Desconocido"),
        "cantidad_canciones_tocadas": total_canciones_show,
        "show_realizado": 1,
    })

    contador += 1
    if contador == shows_a_traer:
      break

# -- D. TICKETMASTER (Futuro - Próximos 3 shows blindado contra KeyError '_embedded') --
url_tm = f"https://app.ticketmaster.com/discovery/v2/events.json?keyword={ARTISTA}&apikey={TICKETMASTER_KEY}"
datos_tm = requests.get(url_tm, timeout=15).json()
paquete_conciertos_futuros = []

eventos_tm = datos_tm.get("_embedded", {}).get("events", [])
for evento in eventos_tm:
  # Evitamos el KeyError: '_embedded' si el evento es virtual, paquete VIP o sin estadio asignado
  venues_tm = evento.get("_embedded", {}).get("venues", [])
  if not venues_tm:
    continue

  venue_principal = venues_tm[0]
  ciudad_tm = venue_principal.get("city", {}).get("name")
  fecha_tm = evento.get("dates", {}).get("start", {}).get("localDate")
  if not ciudad_tm or not fecha_tm:
    continue

  paquete_conciertos_futuros.append({
      "fecha": fecha_tm,
      "ciudad": ciudad_tm,
      "pais": venue_principal.get("country", {}).get("name", "Desconocido"),
      "cantidad_canciones_tocadas": 0,
      "show_realizado": 0,
  })

  if len(paquete_conciertos_futuros) == 3:
    break

# ==========================================
# 2️⃣ CARGA (L) A SUPABASE
# ==========================================
print("\n🧊 ABRIENDO COMPUERTAS HACIA SUPABASE...")

# A. Cargar/Actualizar DIM_ARTISTA con soporte histórico (SCD Tipo 2)
res_artista = (
    supabase.table("dim_artista")
    .select("*")
    .eq("nombre", ARTISTA)
    .order("id_artista", desc=True)
    .execute()
)

if not res_artista.data:
  # Primera inserción si no existe el artista
  nuevo_artista = {
      "nombre": ARTISTA,
      "genero_principal": "Urbano",
      "oyentes_globales": oyentes_globales_hoy,
      "nivel_popularidad": nivel_popularidad_hoy,
      "fecha_desde": fecha_hoy,
      "fecha_hasta": None,
      "es_actual": True,
  }
  try:
    res_ins = supabase.table("dim_artista").insert(nuevo_artista).execute()
  except Exception:
    res_ins = (
        supabase.table("dim_artista")
        .insert({"nombre": ARTISTA, "genero_principal": "Urbano"})
        .execute()
    )
  id_artista_db = res_ins.data[0]["id_artista"]
else:
  # Verificamos el registro vigente (SCD Tipo 2)
  reg_actual = res_artista.data[0]
  id_artista_db = reg_actual["id_artista"]

  # Si la tabla ya tiene las columnas SCD Tipo 2 y cambió el escalafón o los oyentes globales (>1% de variación)
  if "es_actual" in reg_actual and oyentes_globales_hoy > 0:
    oyentes_previos = reg_actual.get("oyentes_globales") or 0
    cambio_significativo = abs(oyentes_globales_hoy - oyentes_previos) >= 5000
    if oyentes_previos == 0:
      # Inicializamos los campos SCD2 en el registro actual si estaban vacíos
      supabase.table("dim_artista").update({
          "oyentes_globales": oyentes_globales_hoy,
          "nivel_popularidad": nivel_popularidad_hoy,
          "fecha_desde": reg_actual.get("fecha_desde") or fecha_hoy,
          "es_actual": True,
      }).eq("id_artista", id_artista_db).execute()
    elif cambio_significativo:
      # 1. Cerramos la vigencia del registro histórico anterior
      supabase.table("dim_artista").update({
          "fecha_hasta": fecha_hoy,
          "es_actual": False,
      }).eq("id_artista", id_artista_db).execute()

      # 2. Insertamos la nueva versión vigente (SCD Tipo 2)
      res_nuevo = (
          supabase.table("dim_artista")
          .insert({
              "nombre": ARTISTA,
              "genero_principal": reg_actual.get("genero_principal", "Urbano"),
              "oyentes_globales": oyentes_globales_hoy,
              "nivel_popularidad": nivel_popularidad_hoy,
              "fecha_desde": fecha_hoy,
              "fecha_hasta": None,
              "es_actual": True,
          })
          .execute()
      )
      id_artista_db = res_nuevo.data[0]["id_artista"]


# Función auxiliar para procesar fechas (Convierte "2026-05-18" en ID 20260518)
def obtener_id_fecha(fecha_str):
  partes = fecha_str.split("-")
  id_f = int(f"{partes[0]}{partes[1]}{partes[2]}")
  res_f = (
      supabase.table("dim_fecha").select("id_fecha").eq("id_fecha", id_f).execute()
  )
  if not res_f.data:
    trimestre = (int(partes[1]) - 1) // 3 + 1
    supabase.table("dim_fecha").insert({
        "id_fecha": id_f,
        "anio": int(partes[0]),
        "mes": int(partes[1]),
        "dia": int(partes[2]),
        "trimestre": trimestre,
    }).execute()
  return id_f


# B. Cargar FACTS de REPRODUCCIONES
id_fecha_hoy_db = obtener_id_fecha(fecha_hoy)
print("Subiendo métricas de canciones...")

for cancion in paquete_canciones:
  res_cancion = (
      supabase.table("dim_cancion")
      .select("id_cancion")
      .eq("titulo", cancion["titulo"])
      .execute()
  )
  if not res_cancion.data:
    res_cancion = (
        supabase.table("dim_cancion")
        .insert({"titulo": cancion["titulo"], "nombre_album": cancion["album"]})
        .execute()
    )
  id_cancion_db = res_cancion.data[0]["id_cancion"]

  supabase.table("fact_reproducciones").insert({
      "id_artista": id_artista_db,
      "id_fecha": id_fecha_hoy_db,
      "id_cancion": id_cancion_db,
      "cantidad_oyentes": cancion["oyentes"],
      "total_reproducciones": cancion["reproducciones"],
  }).execute()

# C. Cargar FACTS de CONCIERTOS
print("Subiendo historial y cartelera de conciertos...")
todos_los_conciertos = paquete_conciertos_pasados + paquete_conciertos_futuros

for show in todos_los_conciertos:
  if not show:
    continue

  res_ubi = (
      supabase.table("dim_ubicacion")
      .select("id_ubicacion")
      .eq("ciudad", show["ciudad"])
      .execute()
  )
  if not res_ubi.data:
    res_ubi = (
        supabase.table("dim_ubicacion")
        .insert({"ciudad": show["ciudad"], "pais": show["pais"]})
        .execute()
    )
  id_ubi_db = res_ubi.data[0]["id_ubicacion"]

  id_fecha_show = obtener_id_fecha(show["fecha"])
  supabase.table("fact_conciertos").insert({
      "id_artista": id_artista_db,
      "id_fecha": id_fecha_show,
      "id_ubicacion": id_ubi_db,
      "cantidad_canciones_tocadas": show.get("cantidad_canciones_tocadas", 0),
      "show_realizado": show.get("show_realizado", 1),
  }).execute()

print(
    "\n✅ ¡ETL COMPLETADO CON ÉXITO! Todos los datos están en tu Data Warehouse."
)
