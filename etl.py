import os
import requests
import time
from datetime import date
import json
from supabase import create_client, Client #  ¡NUEVA HERRAMIENTA!

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

# -- LAST.FM (Canciones) --
url_top = f"http://ws.audioscrobbler.com/2.0/?method=artist.gettoptracks&artist={ARTISTA}&api_key={LASTFM_KEY}&format=json"
lista_canciones = requests.get(url_top).json()['toptracks']['track'][:5]
paquete_canciones = []

for track in lista_canciones:
    nombre_cancion = track['name']
    url_info = f"http://ws.audioscrobbler.com/2.0/?method=track.getInfo&api_key={LASTFM_KEY}&artist={ARTISTA}&track={nombre_cancion}&format=json"
    try:
        nombre_album = requests.get(url_info).json()['track']['album']['title']
    except:
        nombre_album = "Sencillo / Sin Álbum"
        
    paquete_canciones.append({
        "titulo": nombre_cancion, "album": nombre_album,
        "oyentes": int(track['listeners']), "reproducciones": int(track['playcount'])
    })

# -- SETLIST.FM (Pasado - Últimos 5 shows) --
url_setlist = f"https://api.setlist.fm/rest/1.0/artist/{MBID_ARTISTA}/setlists?p=4"
datos_setlist = requests.get(url_setlist, headers={"Accept": "application/json", "x-api-key": SETLIST_KEY}).json()

paquete_conciertos_pasados = []  # Ahora es una lista vacía
shows_a_traer = 5 
contador = 0

for show in datos_setlist.get('setlist', []):
    fecha_orig = show.get('eventDate', '')
    if "2024" in fecha_orig or "2025" in fecha_orig or "2026" in fecha_orig:
        partes = fecha_orig.split('-')
        
        # Guardamos cada show en la lista con .append()
        paquete_conciertos_pasados.append({
            "fecha": f"{partes[2]}-{partes[1]}-{partes[0]}",
            "ciudad": show['venue']['city']['name'], 
            "pais": show['venue']['city'].get('country', {}).get('name', 'Desconocido')
        })
        
        # Sumamos 1 al contador y frenamos recién cuando llega a 5
        contador += 1
        if contador == shows_a_traer:
            break

# -- TICKETMASTER (Futuro - Próximos 3 shows) --
url_tm = f"https://app.ticketmaster.com/discovery/v2/events.json?keyword={ARTISTA}&apikey={TICKETMASTER_KEY}"
datos_tm = requests.get(url_tm).json()
paquete_conciertos_futuros = []

if '_embedded' in datos_tm:
    for evento in datos_tm['_embedded']['events'][:3]:
        paquete_conciertos_futuros.append({
            "fecha": evento['dates']['start']['localDate'],
            "ciudad": evento['_embedded']['venues'][0]['city']['name'],
            "pais": evento['_embedded']['venues'][0].get('country', {}).get('name', 'Desconocido')
        })

# ==========================================
# 2️⃣ CARGA (L) A SUPABASE 
# ==========================================
print("\n🧊 ABRIENDO COMPUERTAS HACIA SUPABASE...")

# A. Cargar/Obtener DIM_ARTISTA
res_artista = supabase.table("dim_artista").select("id_artista").eq("nombre", ARTISTA).execute()
if not res_artista.data:
    res_artista = supabase.table("dim_artista").insert({"nombre": ARTISTA, "genero_principal": "Urbano"}).execute()
id_artista_db = res_artista.data[0]['id_artista']

# Función auxiliar para procesar fechas (Convierte "2026-05-18" en ID 20260518)
def obtener_id_fecha(fecha_str):
    partes = fecha_str.split('-')
    id_f = int(f"{partes[0]}{partes[1]}{partes[2]}")
    res_f = supabase.table("dim_fecha").select("id_fecha").eq("id_fecha", id_f).execute()
    if not res_f.data:
        trimestre = (int(partes[1]) - 1) // 3 + 1
        supabase.table("dim_fecha").insert({
            "id_fecha": id_f, "anio": int(partes[0]), "mes": int(partes[1]), 
            "dia": int(partes[2]), "trimestre": trimestre
        }).execute()
    return id_f

# B. Cargar FACTS de REPRODUCCIONES
id_fecha_hoy_db = obtener_id_fecha(fecha_hoy)
print("Subiendo métricas de canciones...")

for cancion in paquete_canciones:
    # Dimensión Canción
    res_cancion = supabase.table("dim_cancion").select("id_cancion").eq("titulo", cancion["titulo"]).execute()
    if not res_cancion.data:
        res_cancion = supabase.table("dim_cancion").insert({"titulo": cancion["titulo"], "nombre_album": cancion["album"]}).execute()
    id_cancion_db = res_cancion.data[0]['id_cancion']
    
    # Hecho Reproducciones
    supabase.table("fact_reproducciones").insert({
        "id_artista": id_artista_db, "id_fecha": id_fecha_hoy_db, "id_cancion": id_cancion_db,
        "cantidad_oyentes": cancion["oyentes"], "total_reproducciones": cancion["reproducciones"]
    }).execute()

# C. Cargar FACTS de CONCIERTOS
print("Subiendo historial y cartelera de conciertos...")
todos_los_conciertos = paquete_conciertos_pasados + paquete_conciertos_futuros

for show in todos_los_conciertos:
    if not show: continue # Por si no encontró show pasado
    
    # Dimensión Ubicación
    res_ubi = supabase.table("dim_ubicacion").select("id_ubicacion").eq("ciudad", show["ciudad"]).execute()
    if not res_ubi.data:
        res_ubi = supabase.table("dim_ubicacion").insert({"ciudad": show["ciudad"], "pais": show["pais"]}).execute()
    id_ubi_db = res_ubi.data[0]['id_ubicacion']
    
    # Hecho Conciertos
    id_fecha_show = obtener_id_fecha(show["fecha"])
    supabase.table("fact_conciertos").insert({
        "id_artista": id_artista_db, "id_fecha": id_fecha_show, "id_ubicacion": id_ubi_db,
        "cantidad_canciones_tocadas": 0, "show_realizado": 1 
    }).execute()

print("\n✅ ¡ETL COMPLETADO CON ÉXITO! Todos los datos están en tu Data Warehouse.")

#prueba(?)