#!/usr/bin/env python3
"""AirSat static pipeline with QA, georeferenced web images and validation."""
from __future__ import annotations

import hashlib, io, json, os, re, time, zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import ee
import numpy as np
import rasterio
import requests
from PIL import Image
from rasterio.io import MemoryFile
from rasterio.warp import transform_bounds

PIPELINE_VERSION = "airsat-pipeline-v7.0-reliable-atomic-self-healing"
ROOT = Path(os.environ.get("AIRSAT_REPOSITORY_ROOT", str(Path(__file__).resolve().parents[1]))).expanduser().resolve()
PUBLIC = ROOT / "public"
DATA = PUBLIC / "data"
CATALOG = DATA / "catalog"
BOUNDARIES = DATA / "boundaries"
STATS = DATA / "stats"
TIMESERIES = DATA / "timeseries"
VISUAL = PUBLIC / "visual_real"
for p in (CATALOG, BOUNDARIES, STATS, TIMESERIES, VISUAL): p.mkdir(parents=True, exist_ok=True)

NOW = datetime.now(timezone.utc)
BUILD_MODE = os.getenv("AIRSAT_BUILD_MODE", "daily").strip().lower()
START_YEAR = int(os.getenv("AIRSAT_START_YEAR", "2018"))
THUMB_DIMENSIONS = int(os.getenv("AIRSAT_THUMB_DIMENSIONS", "1200"))
STATS_SCALE = int(os.getenv("AIRSAT_STATS_SCALE", "7000"))
STATS_CRS = os.getenv("AIRSAT_STATS_CRS", "EPSG:4326").strip()
WEB_CRS = os.getenv("AIRSAT_WEB_CRS", "EPSG:3857").strip()
DOWNLOAD_TIMEOUT = int(os.getenv("AIRSAT_DOWNLOAD_TIMEOUT", "900"))
BOUNDARY_SIMPLIFY_M = float(os.getenv("AIRSAT_BOUNDARY_SIMPLIFY_M", "100"))
MAX_DOWNLOAD_ATTEMPTS = int(os.getenv("AIRSAT_MAX_DOWNLOAD_ATTEMPTS", "1"))
VISUAL_EXPORT_DIMENSIONS = [
    int(item.strip())
    for item in os.getenv(
        "AIRSAT_VISUAL_EXPORT_DIMENSIONS",
        f"{THUMB_DIMENSIONS},1000,800",
    ).split(",")
    if item.strip()
]
VISUAL_EXPORT_DIMENSIONS = list(dict.fromkeys(
    dimension for dimension in VISUAL_EXPORT_DIMENSIONS if dimension >= 500
))
VISUAL_GAP_FILL = os.getenv("AIRSAT_VISUAL_GAP_FILL", "true").strip().lower() not in {"0", "false", "no", "off"}
VISUAL_GAP_FILL_RADII_KM = [
    float(x) for x in os.getenv("AIRSAT_VISUAL_GAP_FILL_RADII_KM", "10,30,75").split(",")
    if x.strip()
]
VISUAL_FINAL_MEAN_FILL = os.getenv("AIRSAT_VISUAL_FINAL_MEAN_FILL", "true").strip().lower() not in {"0", "false", "no", "off"}
EXPECTED_PROVINCE_COUNT = int(os.getenv("AIRSAT_EXPECTED_PROVINCE_COUNT", "31"))
BUILD_TIMESERIES = os.getenv("AIRSAT_BUILD_TIMESERIES", "false").strip().lower() in {"1", "true", "yes", "on"}

ONLY_PERIOD_KEYS = {
    item.strip()
    for item in os.getenv("AIRSAT_PERIOD_KEYS", "").split(",")
    if item.strip()
}
TIMESERIES_YEAR_RAW = os.getenv("AIRSAT_TIMESERIES_YEAR", "").strip()
TIMESERIES_YEAR = int(TIMESERIES_YEAR_RAW) if TIMESERIES_YEAR_RAW else None
TIMESERIES_MONTHS = {
    int(item.strip())
    for item in os.getenv("AIRSAT_TIMESERIES_MONTHS", "").split(",")
    if item.strip()
}
SKIP_EXISTING = os.getenv("AIRSAT_SKIP_EXISTING", "true").strip().lower() in {
    "1", "true", "yes", "on"
}

POLLUTANTS: dict[str, dict[str, Any]] = {
    # Earth Engine OFFL/L3 products are already screened during L2 -> L3
    # ingestion. The published science band is therefore used directly.
    # Gap filling below is applied only to the public visual image; statistics
    # and downloadable scientific rasters continue to use the original mask.
    "NO2": {
        "dataset_start":"2018-06-28",
        "label":"NO₂", "name_fa":"دی‌اکسید نیتروژن", "name_en":"Nitrogen dioxide",
        "collection":"COPERNICUS/S5P/OFFL/L3_NO2",
        "band":"tropospheric_NO2_column_number_density",
        "qa_band":None, "qa_threshold":0.50,
        "qa_applied_by":"earth_engine_l3_ingestion",
        "qa_note":"The Earth Engine OFFL/L3 NO2 product is ingested after source validity screening.",
        "unit":"mol/m²",
        "palette":["#f4fbff","#ccecff","#79d2ef","#27b3c8","#4bc36b","#f2d34f","#f08a35","#d63b32"],
        "vmin":0.0, "vmax":0.0002
    },
    "SO2": {
        "dataset_start":"2018-12-05",
        "label":"SO₂", "name_fa":"دی‌اکسید گوگرد", "name_en":"Sulfur dioxide",
        "collection":"COPERNICUS/S5P/OFFL/L3_SO2",
        "band":"SO2_column_number_density",
        "qa_band":None, "qa_threshold":0.50,
        "qa_applied_by":"earth_engine_l3_ingestion",
        "qa_note":"The Earth Engine OFFL/L3 SO2 product is ingested after source quality screening.",
        "unit":"mol/m²",
        "palette":["#f8f5ff","#ded5ff","#b79cf2","#8c66d1","#b84eaa","#e47886","#c73d50"],
        "vmin":0.0, "vmax":0.0005
    },
    "CO": {
        "dataset_start":"2018-06-28",
        "label":"CO", "name_fa":"مونوکسید کربن", "name_en":"Carbon monoxide",
        "collection":"COPERNICUS/S5P/OFFL/L3_CO",
        "band":"CO_column_number_density",
        "qa_band":None, "qa_threshold":0.50,
        "qa_applied_by":"earth_engine_l3_ingestion",
        "qa_note":"The Earth Engine OFFL/L3 CO product is ingested after source validity screening.",
        "unit":"mol/m²",
        "palette":["#f2fbf7","#c6ead9","#72c9a9","#2aa38e","#d6d956","#f2a446","#d6534f"],
        "vmin":0.0, "vmax":0.05
    },
    "O3": {
        "dataset_start":"2018-09-08",
        "label":"O₃", "name_fa":"ازن", "name_en":"Ozone",
        "collection":"COPERNICUS/S5P/OFFL/L3_O3",
        "band":"O3_column_number_density",
        "qa_band":None, "qa_threshold":0.50,
        "qa_applied_by":"earth_engine_l3_ingestion",
        "qa_note":"The Earth Engine OFFL/L3 ozone product is ingested after source quality screening.",
        "unit":"mol/m²",
        "palette":["#f1faff","#c1e7f7","#70c8dc","#83cf7a","#eedc62","#ed9b59","#a95eb8"],
        "vmin":0.12, "vmax":0.15
    },
    "HCHO": {
        "dataset_start":"2018-12-05",
        "label":"HCHO", "name_fa":"فرمالدهید", "name_en":"Formaldehyde",
        "collection":"COPERNICUS/S5P/OFFL/L3_HCHO",
        "band":"tropospheric_HCHO_column_number_density",
        "qa_band":None, "qa_threshold":0.50,
        "qa_applied_by":"earth_engine_l3_ingestion",
        "qa_note":"The Earth Engine OFFL/L3 HCHO product is ingested after source validity screening.",
        "unit":"mol/m²",
        "palette":["#f3fbfa","#c4ebe1","#6cccb8","#50b68b","#b8d85d","#efb24d","#db655c"],
        "vmin":0.0, "vmax":0.0003
    },
    "AER_AI": {
        "dataset_start":"2018-07-04",
        "label":"AER_AI", "name_fa":"شاخص جذب آئروسل", "name_en":"Absorbing aerosol index",
        "collection":"COPERNICUS/S5P/OFFL/L3_AER_AI",
        "band":"absorbing_aerosol_index",
        "qa_band":None, "qa_threshold":0.80,
        "qa_applied_by":"earth_engine_l3_ingestion",
        "qa_note":"The Earth Engine OFFL/L3 aerosol-index product is ingested after source quality screening.",
        "unit":"index",
        "palette":["#3f7fa3","#87bdd2","#d9eef1","#f4edc3","#f2cf72","#e58b46","#bb4b2d","#7f2d20"],
        "vmin":-1.0, "vmax":3.0
    },
    "CH4": {
        "dataset_start":"2019-02-08",
        "label":"CH₄", "name_fa":"متان", "name_en":"Methane",
        "collection":"COPERNICUS/S5P/OFFL/L3_CH4",
        "band":"CH4_column_volume_mixing_ratio_dry_air",
        "qa_band":None, "qa_threshold":0.50,
        "qa_applied_by":"earth_engine_l3_ingestion",
        "qa_note":"The Earth Engine OFFL/L3 CH4 product is ingested after source validity screening.",
        "unit":"ppb",
        "palette":["#f4fff8","#c9f0d8","#83d8aa","#43bb80","#c7d957","#efa045","#d65a4d"],
        "vmin":1750.0, "vmax":1950.0
    },
}
NAME_FIELDS = ["name_fa","NAME_FA","نام","نام_استان","ostan","OSTAN","Province","province","NAME_1","ADM1_NAME","name","Name","NAME"]

def write_json(path: Path, obj: Any) -> None:
    """Write JSON atomically so cancelled jobs never leave truncated files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)

def read_json(path: Path, default: Any) -> Any:
    if not path.exists(): return default
    try: return json.loads(path.read_text(encoding="utf-8"))
    except Exception: return default

def sha256_bytes(data: bytes) -> str: return hashlib.sha256(data).hexdigest()
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024*1024), b""): h.update(chunk)
    return h.hexdigest()
def safe_id(v: Any) -> str:
    s = str(v or "").strip(); return re.sub(r"[^\w\u0600-\u06FF]+", "_", s).strip("_") or "province"
def dstr(v: date) -> str: return v.strftime("%Y-%m-%d")
def add_months(v: date, m: int) -> date:
    y = v.year + (v.month - 1 + m)//12; mo = (v.month - 1 + m)%12 + 1
    return v.replace(year=y, month=mo, day=1)
def ticks(vmin: float, vmax: float, n: int = 5) -> list[float]: return [vmin+i*(vmax-vmin)/(n-1) for i in range(n)]

def dataset_start_date(cfg):
    return date.fromisoformat(cfg["dataset_start"])

def annual_periods(cfg):
    out = {}
    first = dataset_start_date(cfg).year
    last = NOW.date().year - 1
    for y in range(max(START_YEAR, first), last + 1):
        out[f"annual_{y}"] = {
            "label_fa": f"سال {y}",
            "start": date(y, 1, 1),
            "end": date(y + 1, 1, 1),
            "group": "annual",
            "year": y,
        }
    return out

def range_periods(cfg):
    out = {}
    first = dataset_start_date(cfg).year
    last = NOW.date().year - 1
    for y1 in range(max(START_YEAR, first), last + 1):
        for y2 in range(y1 + 1, last + 1):
            out[f"range_{y1}_{y2}"] = {
                "label_fa": f"چندساله {y1} تا {y2}",
                "start": date(y1, 1, 1),
                "end": date(y2 + 1, 1, 1),
                "group": "range",
                "start_year": y1,
                "end_year": y2,
            }
    return out

def dynamic_periods(latest: date):
    end = latest + timedelta(days=1); first = latest.replace(day=1); prev = add_months(first,-1)
    return {
        "latest_7d":{"label_fa":"۷ روز اخیر","start":end-timedelta(days=7),"end":end,"group":"dynamic","data_latest_date":latest},
        "latest_30d":{"label_fa":"۳۰ روز اخیر","start":end-timedelta(days=30),"end":end,"group":"dynamic","data_latest_date":latest},
        "latest_90d":{"label_fa":"۹۰ روز اخیر","start":end-timedelta(days=90),"end":end,"group":"dynamic","data_latest_date":latest},
        "latest_month":{"label_fa":"ماه کامل قبلی","start":prev,"end":first,"group":"dynamic","data_latest_date":latest},
        "current_year":{"label_fa":"سال جاری","start":date(latest.year,1,1),"end":end,"group":"dynamic","data_latest_date":latest},
    }

def monthly_periods_from_start(cfg):
    start = dataset_start_date(cfg).replace(day=1)
    current = max(date(START_YEAR, 1, 1), start)
    stop = NOW.date().replace(day=1)
    out = []

    while current < stop:
        if (
            (TIMESERIES_YEAR is None or current.year == TIMESERIES_YEAR)
            and (not TIMESERIES_MONTHS or current.month in TIMESERIES_MONTHS)
        ):
            out.append(
                (
                    current.strftime("%Y-%m"),
                    current,
                    add_months(current, 1),
                )
            )
        current = add_months(current, 1)

    return out

def select_periods(periods):
    if not ONLY_PERIOD_KEYS:
        return periods
    missing = sorted(ONLY_PERIOD_KEYS - set(periods))
    if missing:
        raise RuntimeError(
            f"Requested period keys are not valid for build mode {BUILD_MODE}: {missing}"
        )
    return {key: value for key, value in periods.items() if key in ONLY_PERIOD_KEYS}

def existing_layer_is_complete(pid, key, existing_layers):
    if not SKIP_EXISTING:
        return False
    layer = next(
        (
            item for item in existing_layers
            if item.get("pollutant") == pid and item.get("period_key") == key
        ),
        None,
    )
    if not layer or not layer.get("available"):
        return False
    visual_path = PUBLIC / str(layer.get("visual_path", "")).lstrip("/")
    georef_path = PUBLIC / str(layer.get("georef_path", "")).lstrip("/")
    stats_path = STATS / pid / f"{key}.json"
    complete = visual_path.exists() and georef_path.exists() and stats_path.exists()
    if complete:
        print("Skip complete existing layer:", pid, key)
    return complete

def ee_init():
    key_json=os.getenv("EE_SERVICE_ACCOUNT_JSON"); project=os.getenv("EE_PROJECT")
    if not key_json or not project: raise RuntimeError("Missing EE_SERVICE_ACCOUNT_JSON or EE_PROJECT")
    info=json.loads(key_json); creds=ee.ServiceAccountCredentials(info["client_email"], key_data=json.dumps(info)); ee.Initialize(creds, project=project)
    print("EE initialized:", project)

def choose_name_field(fc):
    forced=os.getenv("EE_PROVINCE_NAME_FIELD","").strip()
    if forced: return forced
    sample=fc.limit(5).getInfo().get("features",[]); keys=set()
    for ft in sample: keys.update(ft.get("properties",{}).keys())
    for k in NAME_FIELDS:
        if k in keys: return k
    for k in sorted(keys):
        vals=[str(ft.get("properties",{}).get(k)) for ft in sample if ft.get("properties",{}).get(k) is not None]
        if vals and any(not v.isdigit() for v in vals): return k
    return sorted(keys)[0] if keys else "system:index"

def normalize_fc(fc,name_field): return fc.map(lambda f:f.set({"name_fa":ee.String(f.get(name_field)),"airsat_id":ee.String(f.get(name_field))}))

def feature_bbox(ft):
    coords=[]
    def walk(o):
        if isinstance(o,dict):
            if "coordinates" in o: walk(o["coordinates"])
            if "geometries" in o: walk(o["geometries"])
        elif isinstance(o,list):
            if len(o)>=2 and isinstance(o[0],(int,float)) and isinstance(o[1],(int,float)): coords.append((float(o[0]),float(o[1])))
            else:
                for i in o: walk(i)
    walk(ft.get("geometry"))
    if not coords: return None
    xs=[c[0] for c in coords]; ys=[c[1] for c in coords]
    return [[min(ys),min(xs)],[max(ys),max(xs)]]

def build_provinces(fc,name_field):
    browser=fc if BOUNDARY_SIMPLIFY_M<=0 else fc.map(lambda f:f.simplify(BOUNDARY_SIMPLIFY_M))
    geo=browser.getInfo(); provinces=[]
    for i,ft in enumerate(geo.get("features",[]),1):
        p=ft.setdefault("properties",{}); name=str(p.get("name_fa") or p.get(name_field) or f"استان {i}"); pid=safe_id(name)
        p["airsat_id"]=pid; p["name_fa"]=name; provinces.append({"id":pid,"name_fa":name,"bbox":feature_bbox(ft)})
    write_json(BOUNDARIES/"provinces.geojson",geo)
    write_json(CATALOG/"provinces.json",{"name_field":name_field,"boundary_simplify_m":BOUNDARY_SIMPLIFY_M,"provinces":sorted(provinces,key=lambda x:x["name_fa"])})
    return provinces

def raw_collection(cfg,start,end,country): return ee.ImageCollection(cfg["collection"]).filterBounds(country).filterDate(str(start),str(end))
def process_image(image,cfg):
    # The OFFL/L3 collections have already been quality-filtered during
    # Earth Engine ingestion and do not contain a qa_value band. Selecting
    # qa_value here would fail. We therefore select only the published science
    # band and record the upstream quality policy in output metadata.
    return image.select(cfg["band"]).rename("value").copyProperties(
        image,["system:time_start","system:index"]
    )

def validate_dataset_schema(cfg,country):
    coll=raw_collection(cfg,date(START_YEAR,1,1),NOW.date()+timedelta(days=1),country)
    first=ee.Image(coll.first())
    bands=first.bandNames().getInfo()
    if cfg["band"] not in bands:
        raise RuntimeError(
            f"Science band {cfg['band']!r} is missing from {cfg['collection']}. "
            f"Available bands: {bands}"
        )
    print(
        "Dataset schema OK:",
        cfg["collection"],
        "science_band=",
        cfg["band"],
        "quality_filter=",
        cfg.get("qa_applied_by"),
        "source_threshold=",
        cfg.get("qa_threshold"),
    )

def latest_available_date(cfg,country):
    coll=raw_collection(cfg,date(START_YEAR,1,1),NOW.date()+timedelta(days=1),country); ts=coll.aggregate_max("system:time_start").getInfo()
    return None if ts is None else datetime.fromtimestamp(float(ts)/1000.0,tz=timezone.utc).date()

def ordinary_composite(cfg,start,end,country):
    coll=raw_collection(cfg,start,end,country); count=int(coll.size().getInfo() or 0)
    if count<=0: return None,0,{"reason":"no_images"}
    img=coll.map(lambda im:process_image(im,cfg)).mean().rename("value").clip(country)
    return img,count,{"raw_image_count":count,"aggregation":"mean_of_valid_observations"}

def equal_year_range_composite(cfg,y1,y2,country):
    imgs=[]; counts={}; missing=[]; total=0
    for y in range(y1,y2+1):
        coll=raw_collection(cfg,date(y,1,1),date(y+1,1,1),country); c=int(coll.size().getInfo() or 0); counts[str(y)]=c; total+=c
        if c<=0: missing.append(y); continue
        imgs.append(coll.map(lambda im:process_image(im,cfg)).mean().rename("value").clip(country))
    if missing or not imgs: return None,total,{"reason":"missing_years","missing_years":missing,"year_image_counts":counts}
    img=ee.ImageCollection.fromImages(imgs).mean().rename("value").clip(country)
    return img,total,{"raw_image_count":total,"year_image_counts":counts,"years_used":list(range(y1,y2+1)),"aggregation":"equal_weight_mean_of_annual_means"}

def composite_for_period(cfg,pinfo,country):
    if pinfo.get("group")=="range": return equal_year_range_composite(cfg,int(pinfo["start_year"]),int(pinfo["end_year"]),country)
    return ordinary_composite(cfg,pinfo["start"],pinfo["end"],country)

def reducer():
    return ee.Reducer.minMax().combine(
        reducer2=ee.Reducer.mean(), outputPrefix="", sharedInputs=True
    )

def _number_from(properties: dict[str, Any], *keys: str):
    for key in keys:
        value = properties.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None

def region_stats(image,geom):
    r=image.reduceRegion(
        reducer=reducer(), geometry=geom, scale=STATS_SCALE, crs=STATS_CRS,
        bestEffort=False, maxPixels=1e13, tileScale=4
    ).getInfo()
    # reduceRegion with a combined reducer normally prefixes the science-band
    # name. Accept both prefixed and unprefixed keys for forward compatibility.
    return {
        "min":_number_from(r,"value_min","min"),
        "max":_number_from(r,"value_max","max"),
        "mean":_number_from(r,"value_mean","mean","value"),
    }

def _province_rows(image,fc):
    feats=image.reduceRegions(
        collection=fc, reducer=reducer(), scale=STATS_SCALE, crs=STATS_CRS,
        tileScale=4, maxPixelsPerRegion=100000000
    ).getInfo().get("features",[])
    rows=[]
    for ft in feats:
        p=ft.get("properties",{})
        name=str(p.get("name_fa") or p.get("NAME_FA") or p.get("name") or "استان")
        # Earth Engine reduceRegions appends reducer outputs to each feature.
        # Depending on reducer composition/API generation the keys may be
        # value_mean/value_min/value_max or mean/min/max. Supporting both fixes
        # the empty NO2/SO2/CO provincial tables seen in pipeline v5.2.
        rows.append({
            "id":safe_id(name), "name_fa":name,
            "mean":_number_from(p,"value_mean","mean","value"),
            "min":_number_from(p,"value_min","min"),
            "max":_number_from(p,"value_max","max"),
            "interpolated":False,
        })
    return rows

def province_stats(image,fc,fallback_image=None):
    rows=_province_rows(image,fc)
    fallback={}
    if fallback_image is not None and any(r["mean"] is None for r in rows):
        fallback={r["id"]:r for r in _province_rows(fallback_image,fc)}
    for row in rows:
        if row["mean"] is None and row["id"] in fallback:
            alt=fallback[row["id"]]
            if alt["mean"] is not None:
                row.update({"mean":alt["mean"],"min":alt["min"],"max":alt["max"],"interpolated":True})
    ranked=[r for r in rows if r["mean"] is not None]
    ranked.sort(key=lambda x:x["mean"],reverse=True)
    for i,row in enumerate(ranked,1): row["rank"]=i
    unranked=[r for r in rows if r["mean"] is None]
    for row in unranked: row["rank"]=None
    return ranked+sorted(unranked,key=lambda x:x["name_fa"])

def visual_gap_filled_image(image,country,summary_mean):
    """Fill display-only gaps while retaining original values where observed."""
    if not VISUAL_GAP_FILL:
        return image.clip(country), {
            "enabled":False, "method":"none", "radii_km":[],
            "final_mean_fill":False, "statistics_use_original_mask":True,
        }
    filled=image
    for radius_km in VISUAL_GAP_FILL_RADII_KM:
        neighbourhood=filled.focal_mean(
            radius=float(radius_km)*1000.0, kernelType="circle",
            units="meters", iterations=1
        )
        filled=filled.unmask(neighbourhood)
    if VISUAL_FINAL_MEAN_FILL and summary_mean is not None:
        filled=filled.unmask(ee.Image.constant(float(summary_mean)).rename("value"))
    return filled.clip(country), {
        "enabled":True,
        "method":"original_pixels_then_multiscale_focal_mean",
        "radii_km":VISUAL_GAP_FILL_RADII_KM,
        "final_mean_fill":bool(VISUAL_FINAL_MEAN_FILL),
        "statistics_use_original_mask":True,
        "note_fa":"پرکردن شکاف فقط برای نمایش نقشه انجام شده و آمار ملی و استانی تا حد امکان از پیکسل‌های اصلی محاسبه می‌شوند.",
        "note_en":"Gap filling is used only for the public visual. National and provincial statistics preferentially use original valid pixels.",
    }

def projected_country_bbox(country):
    rect=country.transform(WEB_CRS,1).bounds(1,WEB_CRS); ring=rect.coordinates().getInfo()[0]; xs=[float(p[0]) for p in ring]; ys=[float(p[1]) for p in ring]
    b=[min(xs),min(ys),max(xs),max(ys)]; return ee.Geometry.Rectangle(b,WEB_CRS,False),b

def download_bytes(url):
    last=None
    for attempt in range(1,MAX_DOWNLOAD_ATTEMPTS+1):
        try:
            print(f"Download attempt {attempt}/{MAX_DOWNLOAD_ATTEMPTS}"); r=requests.get(url,timeout=DOWNLOAD_TIMEOUT); r.raise_for_status()
            if not r.content: raise RuntimeError("Earth Engine returned empty response")
            return r.content
        except Exception as e:
            last=e; print("Download failed:",repr(e)); time.sleep(10*attempt)
    raise last

def extract_tiff_bytes(payload):
    if payload[:2]==b"PK":
        with zipfile.ZipFile(io.BytesIO(payload)) as z:
            names=[n for n in z.namelist() if n.lower().endswith((".tif",".tiff"))]
            if not names: raise RuntimeError("ZIP response has no GeoTIFF")
            return z.read(names[0])
    return payload

def validate_geotiff(ds,expected):
    if ds.crs is None: raise RuntimeError("GeoTIFF has no CRS")
    if WEB_CRS.upper()=="EPSG:3857" and ds.crs.to_epsg()!=3857: raise RuntimeError(f"Unexpected CRS: {ds.crs}")
    if ds.width<=0 or ds.height<=0: raise RuntimeError("Invalid raster dimensions")
    tr=ds.transform
    if abs(tr.b)>1e-9 or abs(tr.d)>1e-9: raise RuntimeError("Raster is rotated/skewed")
    px,py=abs(float(tr.a)),abs(float(tr.e)); actual=[float(ds.bounds.left),float(ds.bounds.bottom),float(ds.bounds.right),float(ds.bounds.top)]
    diff=[abs(actual[i]-expected[i]) for i in range(4)]
    if diff[0]>px*2.5 or diff[2]>px*2.5 or diff[1]>py*2.5 or diff[3]>py*2.5: raise RuntimeError(f"Bounds drift exceeds tolerance: {diff}")
    mask=ds.dataset_mask(); valid=int(np.count_nonzero(mask)); total=int(mask.size)
    if valid<=0: raise RuntimeError("GeoTIFF contains no valid pixels")
    return {"validated":True,"crs":ds.crs.to_string(),"epsg":ds.crs.to_epsg(),"width":ds.width,"height":ds.height,"transform":list(tr)[:6],"projected_bounds":actual,"pixel_size_x":px,"pixel_size_y":py,"valid_pixel_fraction":valid/total,"bounds_difference_m":diff}

def build_rgba(ds):
    bands=ds.read(); rgb=np.stack([bands[min(i,bands.shape[0]-1)] for i in range(3)],axis=-1); rgb=np.clip(rgb,0,255).astype(np.uint8); alpha=ds.dataset_mask().astype(np.uint8)
    return np.dstack((rgb,alpha))

def download_georeferenced_webp(image,cfg,country,out_path):
    """
    Export the visual with a new Earth Engine URL on every outer retry.

    A failed signed download URL must not be retried forever. We therefore
    regenerate the URL and progressively lower only the public preview
    dimensions. Scientific statistics remain unchanged.
    """
    last_error = None

    for outer_attempt, dimensions in enumerate(VISUAL_EXPORT_DIMENSIONS, 1):
        try:
            print(
                f"Georeferenced visual attempt "
                f"{outer_attempt}/{len(VISUAL_EXPORT_DIMENSIONS)} "
                f"at {dimensions}px"
            )

            region, expected = projected_country_bbox(country)
            scale = max(
                expected[2] - expected[0],
                expected[3] - expected[1],
            ) / dimensions

            vis = image.visualize(
                min=float(cfg["vmin"]),
                max=float(cfg["vmax"]),
                palette=[c.replace("#","") for c in cfg["palette"]],
                opacity=1.0,
            )

            # Generate a fresh URL for every attempt.
            url = vis.getDownloadURL({
                "name": f"airsat_visual_{dimensions}",
                "region": region,
                "crs": WEB_CRS,
                "scale": scale,
                "format": "GEO_TIFF",
                "filePerBand": False,
            })

            tif = extract_tiff_bytes(download_bytes(url))

            with MemoryFile(tif) as mem:
                with mem.open() as ds:
                    meta = validate_geotiff(ds, expected)
                    rgba = build_rgba(ds)
                    west, south, east, north = transform_bounds(
                        ds.crs,
                        "EPSG:4326",
                        ds.bounds.left,
                        ds.bounds.bottom,
                        ds.bounds.right,
                        ds.bounds.top,
                        densify_pts=21,
                    )

            out_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_webp = out_path.with_name(out_path.name + ".tmp")
            Image.fromarray(rgba).save(
                temporary_webp,
                "WEBP",
                quality=90,
                method=6,
                exact=True,
            )
            os.replace(temporary_webp, out_path)

            meta.update({
                "web_bounds": [[south, west], [north, east]],
                "requested_scale_m": scale,
                "requested_dimensions": dimensions,
                "webp_sha256": sha256_file(out_path),
                "temporary_geotiff_sha256": sha256_bytes(tif),
                "webp_path": f"/{out_path.relative_to(PUBLIC).as_posix()}",
                "export_attempt": outer_attempt,
            })
            return meta

        except Exception as error:
            last_error = error
            print(
                f"Georeferenced visual attempt {outer_attempt} failed:",
                repr(error),
            )
            if out_path.exists():
                out_path.unlink()
            if outer_attempt < len(VISUAL_EXPORT_DIMENSIONS):
                time.sleep(20 * outer_attempt)

    raise RuntimeError(
        "All georeferenced visual export attempts failed: "
        + repr(last_error)
    )

def clean_dir(path):
    path.mkdir(parents=True,exist_ok=True)
    for f in path.iterdir():
        if f.is_file(): f.unlink()

def unavailable_layer(pid,cfg,key,pinfo,reason,details=None):
    layer={
        "id":f"{pid}_{key}","pollutant":pid,"label":cfg["label"],
        "name_fa":cfg["name_fa"],"name_en":cfg.get("name_en"),"unit":cfg["unit"],
        "period_key":key,"period_label_fa":pinfo["label_fa"],"period_group":pinfo["group"],
        "start_date":dstr(pinfo["start"]),"end_date":dstr(pinfo["end"]),
        "image_count":0,"available":False,"skip_reason":reason,
        "message_fa":"برای این آلاینده در این بازه، داده معتبر ماهواره‌ای برای انتشار آماده یا موجود نیست.",
        "message_en":"No publishable satellite observations are available for this pollutant and period.",
        "visual_path":None,"georef_path":None,"bounds":None,"palette":cfg["palette"],
        "visual_min":cfg["vmin"],"visual_max":cfg["vmax"],
        "ticks":ticks(cfg["vmin"],cfg["vmax"]),"qa_band":cfg.get("qa_band"),
        "qa_threshold":cfg.get("qa_threshold"),"qa_applied_by":cfg.get("qa_applied_by"),
        "qa_note":cfg.get("qa_note"),"qa_applied_in_pipeline":False,
        "summary":{"min":None,"max":None,"mean":None},
        "validation":{"validated":False,"reason":reason},"details":details or {},
        "stats_schema_version":2,"pipeline_version":PIPELINE_VERSION,
        "generated_at_utc":NOW.isoformat().replace("+00:00","Z")
    }
    write_json(STATS/pid/f"{key}.json",{
        "layer":layer,"province_stats":[],"provinces":[],"available":False,
        "skip_reason":reason,"details":details or {},"stats_schema_version":2
    })
    return layer

def build_layer(pid,cfg,key,pinfo,country,fc):
    print("Layer",pid,key,pinfo["start"],pinfo["end"])
    image,count,cmeta=composite_for_period(cfg,pinfo,country)
    if image is None or count<=0:
        raise RuntimeError(
            f"No source images for supported layer {pid} {key}: {cmeta}"
        )

    summary=region_stats(image,country)
    if summary["mean"] is None:
        raise RuntimeError(
            f"No valid pixels for supported layer {pid} {key}: {cmeta}"
        )

    visual_image,fill_meta=visual_gap_filled_image(
        image,
        country,
        summary["mean"],
    )

    out_dir=VISUAL/pid/key
    clean_dir(out_dir)
    webp=out_dir/"Iran.webp"
    georef=download_georeferenced_webp(
        visual_image,
        cfg,
        country,
        webp,
    )
    write_json(out_dir/"georef.json",{
        **georef,
        "pollutant":pid,
        "period_key":key,
        "visual_gap_fill":fill_meta,
        "pipeline_version":PIPELINE_VERSION,
    })

    # Provincial reductions run only after the visual export has succeeded.
    rows=province_stats(image,fc,fallback_image=visual_image)
    observed_count=sum(
        1
        for row in rows
        if row.get("mean") is not None and not row.get("interpolated")
    )
    interpolated_count=sum(
        1
        for row in rows
        if row.get("mean") is not None and row.get("interpolated")
    )

    latest=pinfo.get("data_latest_date")
    layer={
        "id":f"{pid}_{key}","pollutant":pid,"label":cfg["label"],
        "name_fa":cfg["name_fa"],"name_en":cfg.get("name_en"),"unit":cfg["unit"],
        "period_key":key,"period_label_fa":pinfo["label_fa"],"period_group":pinfo["group"],
        "start_date":dstr(pinfo["start"]),"end_date":dstr(pinfo["end"]),
        "data_latest_date":dstr(latest) if latest else None,"image_count":count,"available":True,
        "visual_path":f"/visual_real/{pid}/{key}/Iran.webp",
        "georef_path":f"/visual_real/{pid}/{key}/georef.json",
        "bounds":georef["web_bounds"],"web_crs":georef["crs"],
        "projected_bounds":georef["projected_bounds"],"raster_width":georef["width"],
        "raster_height":georef["height"],"pixel_size_x":georef["pixel_size_x"],
        "pixel_size_y":georef["pixel_size_y"],"visual_sha256":georef["webp_sha256"],
        "palette":cfg["palette"],"visual_min":cfg["vmin"],"visual_max":cfg["vmax"],
        "ticks":ticks(cfg["vmin"],cfg["vmax"]),"qa_band":cfg.get("qa_band"),
        "qa_threshold":cfg.get("qa_threshold"),"qa_applied_by":cfg.get("qa_applied_by"),
        "qa_note":cfg.get("qa_note"),"qa_applied_in_pipeline":False,
        "summary":summary,"aggregation":cmeta.get("aggregation"),"composite_metadata":cmeta,
        "visual_gap_fill":fill_meta,
        "province_count":len(rows),"observed_province_count":observed_count,
        "interpolated_province_count":interpolated_count,
        "validation":{"validated":True,"crs":georef["crs"],
                      "bounds_difference_m":georef["bounds_difference_m"],
                      "valid_pixel_fraction":georef["valid_pixel_fraction"]},
        "stats_schema_version":2,"pipeline_version":PIPELINE_VERSION,
        "generated_at_utc":NOW.isoformat().replace("+00:00","Z")
    }
    write_json(STATS/pid/f"{key}.json",{
        "layer":layer,"province_stats":rows,"provinces":rows,"available":True,
        "province_count":len(rows),"observed_province_count":observed_count,
        "interpolated_province_count":interpolated_count,"stats_schema_version":2
    })
    return layer

def build_ts(pid,cfg,fc,country,province_catalog):
    periods = monthly_periods_from_start(cfg)
    if not periods:
        raise RuntimeError(
            f"No completed monthly periods are available for TIMESERIES_YEAR={TIMESERIES_YEAR}"
        )
    selected_labels = {label for label, _, _ in periods}
    existing_payload = read_json(TIMESERIES/f"{pid}.json", {})
    table={p["id"]:{"id":p["id"],"name_fa":p["name_fa"],"series":[]} for p in province_catalog}

    for province in existing_payload.get("provinces", []) or []:
        province_id = province.get("id")
        if not province_id:
            continue
        item = table.setdefault(
            province_id,
            {
                "id": province_id,
                "name_fa": province.get("name_fa") or province_id,
                "series": [],
            },
        )
        item["series"] = [
            point
            for point in (province.get("series") or [])
            if point.get("period") not in selected_labels
        ]

    for label,s,e in periods:
        print("Timeseries",pid,label)
        image,count,_=ordinary_composite(cfg,s,e,country)
        if image is None or count<=0:
            continue
        rows=province_stats(image,fc)
        for row in rows:
            if row["mean"] is None:
                continue
            item=table.setdefault(row["id"],{"id":row["id"],"name_fa":row["name_fa"],"series":[]})
            item["series"].append({
                "period":label,"value":row["mean"],"min":row["min"],"max":row["max"],
                "image_count":count,"interpolated":False
            })
    for item in table.values():
        item["series"] = sorted(
            item.get("series") or [],
            key=lambda point: str(point.get("period") or "")
        )
    provinces=sorted(table.values(),key=lambda x:x["name_fa"])
    write_json(TIMESERIES/f"{pid}.json",{
        "pollutant":pid,"label":cfg["label"],"name_fa":cfg["name_fa"],
        "name_en":cfg.get("name_en"),"unit":cfg["unit"],"start_year":START_YEAR,
        "generated_through":(NOW.date().replace(day=1)-timedelta(days=1)).strftime("%Y-%m"),
        "qa_band":cfg.get("qa_band"),"qa_threshold":cfg.get("qa_threshold"),
        "qa_applied_by":cfg.get("qa_applied_by"),"qa_note":cfg.get("qa_note"),
        "qa_applied_in_pipeline":False,"stats_crs":STATS_CRS,"stats_scale":STATS_SCALE,
        "stats_schema_version":2,"pipeline_version":PIPELINE_VERSION,
        "dataset_start":cfg["dataset_start"],
        "generated_at_utc":NOW.isoformat().replace("+00:00","Z"),
        "provinces":provinces
    })

def write_pollutants():
    write_json(CATALOG/"pollutants.json",{
        "pipeline_version":PIPELINE_VERSION,
        "pollutants":[{
            "id":k,"label":v["label"],"name_fa":v["name_fa"],"name_en":v.get("name_en"),
            "unit":v["unit"],"palette":v["palette"],"visual_min":v["vmin"],
            "visual_max":v["vmax"],"ticks":ticks(v["vmin"],v["vmax"]),
            "qa_band":v.get("qa_band"),"qa_threshold":v.get("qa_threshold"),
            "qa_applied_by":v.get("qa_applied_by"),"qa_note":v.get("qa_note"),
            "qa_applied_in_pipeline":False,"collection":v["collection"],"band":v["band"],
            "dataset_start":v["dataset_start"],
            "visual_gap_fill_default":VISUAL_GAP_FILL
        } for k,v in POLLUTANTS.items()]
    })

def selected_pollutants():
    wanted=os.getenv("AIRSAT_POLLUTANTS","").strip(); partial=bool(wanted and wanted.lower()!="all"); ids=[x.strip().upper() for x in wanted.split(",") if x.strip()] if partial else list(POLLUTANTS)
    unknown=[x for x in ids if x not in POLLUTANTS]
    if unknown: raise RuntimeError(f"Unknown AIRSAT_POLLUTANTS: {unknown}")
    return ids,partial

def main():
    if BUILD_MODE not in {"bootstrap","daily","ranges","timeseries"}:
        raise RuntimeError(f"Unknown AIRSAT_BUILD_MODE: {BUILD_MODE}")
    print("PIPELINE_VERSION:",PIPELINE_VERSION)
    print("BUILD_MODE:",BUILD_MODE)
    print("VISUAL_GAP_FILL:",VISUAL_GAP_FILL, VISUAL_GAP_FILL_RADII_KM)
    print("ONLY_PERIOD_KEYS:", sorted(ONLY_PERIOD_KEYS))
    print("TIMESERIES_YEAR:", TIMESERIES_YEAR)
    print("TIMESERIES_MONTHS:", sorted(TIMESERIES_MONTHS))
    print("SKIP_EXISTING:", SKIP_EXISTING)
    print("VISUAL_EXPORT_DIMENSIONS:", VISUAL_EXPORT_DIMENSIONS)
    print("DOWNLOAD_TIMEOUT:", DOWNLOAD_TIMEOUT)
    ee_init()
    asset=os.getenv("EE_PROVINCES_ASSET")
    if not asset:
        raise RuntimeError("Missing EE_PROVINCES_ASSET")
    raw=ee.FeatureCollection(asset)
    name_field=choose_name_field(raw)
    fc=normalize_fc(raw,name_field)
    country=raw.geometry().dissolve(1)
    province_catalog=build_provinces(fc,name_field)
    if len(province_catalog) < EXPECTED_PROVINCE_COUNT:
        raise RuntimeError(
            f"Only {len(province_catalog)} provinces were resolved; expected at least {EXPECTED_PROVINCE_COUNT}. "
            f"Check EE_PROVINCES_ASSET and EE_PROVINCE_NAME_FIELD."
        )
    write_pollutants()
    existing=read_json(CATALOG/"layers.json",{"layers":[]}).get("layers",[])
    ids,partial=selected_pollutants()

    if BUILD_MODE=="timeseries" or ONLY_PERIOD_KEYS:
        # Segmented builds update only their selected period and preserve the rest.
        layers=existing[:]
    elif BUILD_MODE=="daily":
        layers=[l for l in existing if l.get("period_group") in {"annual","range"} or str(l.get("period_key","")).startswith(("annual_","range_"))]
        layers += [l for l in existing if partial and l.get("pollutant") not in ids and l.get("period_group")=="dynamic"]
    elif BUILD_MODE=="ranges":
        layers=existing[:]
    else:
        # Full bootstrap rebuilding one pollutant removes stale entries for that pollutant.
        layers=[l for l in existing if l.get("pollutant") not in ids] if partial else []

    for pid in ids:
        cfg=POLLUTANTS[pid]
        print("===",pid,"===")
        validate_dataset_schema(cfg,country)
        if BUILD_MODE=="timeseries":
            build_ts(pid,cfg,fc,country,province_catalog)
            continue
        if BUILD_MODE=="ranges":
            periods=range_periods(cfg)
        else:
            latest=latest_available_date(cfg,country)
            if BUILD_MODE=="daily":
                if latest is None:
                    continue
                periods=dynamic_periods(latest)
            else:
                periods=annual_periods(cfg)
                if latest is not None and not ONLY_PERIOD_KEYS:
                    periods.update(dynamic_periods(latest))
        periods = select_periods(periods)
        print("Selected period keys:", list(periods))
        for key,pinfo in periods.items():
            if existing_layer_is_complete(pid, key, layers):
                continue
            layer=build_layer(pid,cfg,key,pinfo,country,fc)
            layers=[l for l in layers if l.get("id")!=layer["id"]]
            layers.append(layer)
        if BUILD_MODE=="bootstrap" and BUILD_TIMESERIES:
            build_ts(pid,cfg,fc,country,province_catalog)

    order={"dynamic":0,"annual":1,"range":2}
    layers=sorted(layers,key=lambda l:(l.get("pollutant",""),order.get(l.get("period_group"),9),l.get("period_key","")))
    write_json(CATALOG/"layers.json",{
        "version":PIPELINE_VERSION,"build_mode":BUILD_MODE,
        "generated_at_utc":NOW.isoformat().replace("+00:00","Z"),
        "storage_mode":"github_pages_visuals_only","web_crs":WEB_CRS,
        "stats_crs":STATS_CRS,"stats_scale":STATS_SCALE,"start_year":START_YEAR,
        "visual_gap_fill":{"enabled":VISUAL_GAP_FILL,"radii_km":VISUAL_GAP_FILL_RADII_KM,
                           "final_mean_fill":VISUAL_FINAL_MEAN_FILL},
        "layers":layers
    })
    print("Done.")

if __name__=="__main__": main()
