"""
HARMONIE NEERSLAG MONITOR - HEKERBEEKDAL (EARLY WARNING VERSIE)
================================================================
Auteur: Gegenereerd voor 3027057 Aeres Hogeschool
Datum: Maart 2026
Update: Final Clean Version - Environment & Engine Fix
"""

import sys
import os

# 1. PATH FIX VOOR ARCGIS PRO
# We voegen de map toe waar de 'stiekeme' pip installaties zijn geland
user_site_packages = r"C:\Users\mwass\AppData\Roaming\Python\Python313\site-packages"

if os.path.exists(user_site_packages):
    if user_site_packages not in sys.path:
        sys.path.append(user_site_packages)
    # Stel de eccodes locatie in op de plek waar de bibliotheek echt staat
    os.environ['ECCODES_DEFINITION_PATH'] = os.path.join(user_site_packages, "eccodes", "definitions")
else:
    # Fallback naar standaard ArcGIS pad als de user-map niet bestaat
    os.environ['ECCODES_DEFINITION_PATH'] = r"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\Lib\site-packages\eccodes\definitions"

# 2. IMPORTS
import re
import time
import smtplib
import requests
import numpy as np
import xarray as xr
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ========================================
# CONFIGURATIE
# ========================================

# KNMI API configuratie
KNMI_API_KEY = "eyJvcmciOiI1ZTU1NGUxOTI3NGE5NjAwMDEyYTNlYjEiLCJpZCI6IjhkOTNkNWQyODVlYjRkMWVhNDdiNTUyNWNjZWI2OGI4IiwiaCI6Im11cm11cjEyOCJ9"
KNMI_API_BASE = "https://api.dataplatform.knmi.nl/open-data/v1"
DATASET_NAME = "harmonie_arome_cy43_p1"
DATASET_VERSION = "1.0"

# Studiegebied: Hekerbeekdal
HEKERBEEK_LAT_MIN, HEKERBEEK_LAT_MAX = 50.840, 50.880
HEKERBEEK_LON_MIN, HEKERBEEK_LON_MAX = 5.810, 5.870

# Email configuratie
EMAIL_CONFIG = {
    'from':        '3027057@aeres.nl',
    'to':          'Mwassenborg1@gmail.com',
    'smtp_server': 'smtp.gmail.com',
    'smtp_port':   587,
    'username':    '',                 # <- VUL IN: je Gmail adres
    'password':    'jouw_app_wachtwoord'   # <- VUL IN: Gmail app-wachtwoord
}

THRESHOLDS = [10, 15, 20, 25, 30, 35, 40]
EMAIL_THRESHOLD_MINIMUM = 20

WORKSPACE = os.path.join(os.path.expanduser("~"), "Documents", "Harmonie_Monitor")
LOG_FILE   = os.path.join(WORKSPACE, "monitoring_log.txt")

# ========================================
# HULPFUNCTIES
# ========================================

def log_message(message, level="INFO"):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{timestamp}] [{level}] {message}"
    print(entry)
    try:
        os.makedirs(WORKSPACE, exist_ok=True)
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(entry + "\n")
    except Exception: pass

def get_latest_harmonie_file():
    url = f"{KNMI_API_BASE}/datasets/{DATASET_NAME}/versions/{DATASET_VERSION}/files"
    try:
        r = requests.get(url, headers={"Authorization": KNMI_API_KEY}, 
                         params={"maxKeys": 5, "orderBy": "created", "sorting": "desc"}, timeout=30)
        if r.status_code == 200:
            files = r.json().get("files", [])
            return files[0] if files else None
    except Exception as e:
        log_message(f"Fout bij ophalen bestanden: {e}", "ERROR")
    return None

def download_harmonie_file(file_info):
    filename = file_info.get("filename")
    url = f"{KNMI_API_BASE}/datasets/{DATASET_NAME}/versions/{DATASET_VERSION}/files/{filename}/url"
    try:
        r = requests.get(url, headers={"Authorization": KNMI_API_KEY}, timeout=30)
        download_url = r.json().get("temporaryDownloadUrl")
        log_message(f"Download start: {filename}")
        fr = requests.get(download_url, timeout=300)
        if fr.status_code == 200:
            output_path = os.path.join(WORKSPACE, filename)
            with open(output_path, 'wb') as f: f.write(fr.content)
            return output_path
    except Exception as e:
        log_message(f"Download fout: {e}", "ERROR")
    return None

# ========================================
# GRIB2 ANALYSE
# ========================================

def find_precipitation_dataset(grib_file):
    precip_candidates = ['tp', 'APCP', 'precipitation_amount', 'pr', 'lsp', 'cp', 'rain']
    try:
        # We proberen eerst de meest moderne engine aanpak
        ds = xr.open_dataset(grib_file, engine='cfgrib')
        for var in ds.data_vars:
            if any(c.lower() in var.lower() for c in precip_candidates):
                log_message(f"Neerslag gevonden: '{var}'")
                return ds, var
    except Exception as e:
        log_message(f"GRIB openen mislukt, probeer oppervlakte-filter... ({e})", "WARNING")
        try:
            ds = xr.open_dataset(grib_file, engine='cfgrib', filter_by_keys={'typeOfLevel': 'surface'})
            for var in ds.data_vars:
                if any(c.lower() in var.lower() for c in precip_candidates):
                    log_message(f"Neerslag gevonden na filtering: '{var}'")
                    return ds, var
        except Exception as e2:
            log_message(f"Kon GRIB-bestand niet openen: {e2}", "ERROR")
    return None, None

def extract_precipitation_timeseries(grib_file, forecast_start_time):
    log_message("Neerslag tijdserie analyseren...")
    ds, var_name = find_precipitation_dataset(grib_file)
    if ds is None: return []

    try:
        da = ds[var_name]
        lat_name = next((n for n in ['latitude', 'lat', 'y'] if n in da.coords), None)
        lon_name = next((n for n in ['longitude', 'lon', 'x'] if n in da.coords), None)

        # Gebied uitsnijden
        da_gebied = da.sel({
            lat_name: slice(HEKERBEEK_LAT_MIN, HEKERBEEK_LAT_MAX),
            lon_name: slice(HEKERBEEK_LON_MIN, HEKERBEEK_LON_MAX),
        })
        
        # Check op omgekeerde assen
        if da_gebied[lat_name].size == 0:
            da_gebied = da.sel({
                lat_name: slice(HEKERBEEK_LAT_MAX, HEKERBEEK_LAT_MIN),
                lon_name: slice(HEKERBEEK_LON_MIN, HEKERBEEK_LON_MAX),
            })

        time_coord = next((t for t in ['valid_time', 'time', 'step'] if t in da_gebied.coords), None)
        units = str(da.attrs.get('units', '')).lower()
        timeseries_data = []
        time_values = da_gebied.coords[time_coord].values if time_coord else [0]

        for i, t in enumerate(time_values):
            slice_da = da_gebied.isel({time_coord: i}) if time_coord in da_gebied.dims else da_gebied
            vals = slice_da.values.flatten()
            vals = vals[~np.isnan(vals)]
            if len(vals) == 0: continue

            max_p = np.max(vals)
            mean_p = np.mean(vals)
            
            # Conversie naar mm/uur (indien in meters)
            if units == 'm':
                max_p *= 1000
                mean_p *= 1000

            try:
                import pandas as pd
                ts = pd.Timestamp(t).to_pydatetime()
                if ts.tzinfo is None: ts = ts.replace(tzinfo=timezone.utc)
            except: ts = forecast_start_time + timedelta(hours=i)

            timeseries_data.append({
                'timestamp': ts, 'max_precip': float(max_p), 'mean_precip': float(mean_p)
            })
        return timeseries_data
    except Exception as e:
        log_message(f"Fout bij extractie: {e}", "ERROR")
        return []

# ========================================
# MAIN LOOP
# ========================================

def monitor_cycle():
    log_message("=" * 60)
    log_message("START MONITORING CYCLUS - HEKERBEEKDAL")
    log_message("=" * 60)
    
    os.makedirs(WORKSPACE, exist_ok=True)
    file_info = get_latest_harmonie_file()
    if not file_info: return False

    filename = file_info.get("filename", "")
    # Haal referentietijd uit bestandsnaam (bijv. 2026033001)
    match = re.search(r'(\d{10})', filename)
    forecast_start = datetime.strptime(match.group(1), '%Y%m%d%H').replace(tzinfo=timezone.utc) if match else datetime.now(timezone.utc)

    grib_path = download_harmonie_file(file_info)
    if not grib_path: return False

    data = extract_precipitation_timeseries(grib_path, forecast_start)
    
    if data:
        log_message(f"Analyse voltooid voor {len(data)} tijdstappen.")
        high_precip = [d for d in data if d['max_precip'] >= 10]
        if high_precip:
            log_message(f"LET OP: {len(high_precip)} momenten met >10mm/u gevonden!")
        else:
            log_message("Geen zware neerslag (>10mm/u) gedetecteerd voor de komende periode.")
    
    # Opruimen
    if os.path.exists(grib_path): os.remove(grib_path)
    log_message("Cyclus succesvol afgerond.")
    return True

if __name__ == "__main__":
    monitor_cycle()