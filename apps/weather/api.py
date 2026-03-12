import json
import logging
import requests
from datetime import datetime, timedelta
import time

import numpy as np
import pandas as pd
import pytz
import xarray as xr
import boto3
from botocore.exceptions import ClientError

from django.conf import settings
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

logger = logging.getLogger(__name__)

# India bounding box constants
MIN_LAT = 8
MAX_LAT = 37
MIN_LON = 68
MAX_LON = 97
FORECAST_DAYS_LIMIT = 15
HOURLY_FORECAST_HOURS = 120   # 5 days
EXTENDED_FORECAST_HOURS = 360 # 15 days
S3_BUCKET = "corestack-weather-data"


def get_zarr_path(date: datetime) -> str:
    return f"s3://{S3_BUCKET}/india_gfs_{date.strftime('%Y%m%d')}.zarr"


def zarr_exists_on_s3(date: datetime) -> bool:
    s3 = boto3.client(
        "s3",
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
    )
    prefix = f"india_gfs_{date.strftime('%Y%m%d')}.zarr/"
    try:
        response = s3.list_objects_v2(
            Bucket=S3_BUCKET,
            Prefix=prefix,
            MaxKeys=1
        )
        return response.get("KeyCount", 0) > 0
    except ClientError:
        return False


def resolve_zarr_path() -> str | None:
    """Try today's Zarr first, fall back to yesterday's."""
    today = datetime.utcnow()
    yesterday = today - timedelta(days=1)

    if zarr_exists_on_s3(today):
        zarr_path = get_zarr_path(today)
        logger.info(f"Using today's Zarr: {zarr_path}")
        return zarr_path
    elif zarr_exists_on_s3(yesterday):
        zarr_path = get_zarr_path(yesterday)
        logger.warning(f"Today's Zarr not found, falling back to yesterday's: {zarr_path}")
        return zarr_path
    return None


def open_zarr(zarr_path: str) -> xr.Dataset:
    return xr.open_zarr(
        zarr_path,
        storage_options={
            "key": settings.AWS_ACCESS_KEY_ID,
            "secret": settings.AWS_SECRET_ACCESS_KEY,
        },
        consolidated=True,
    )


def get_daily_precip_from_zarr(zarr_date: datetime, target_date: datetime, lat: float, lon: float) -> float | None:
    """
    Open a Zarr for zarr_date, select lat/lon, and sum 24 hourly lead times
    corresponding to target_date (00:00–23:00 UTC) to get total mm for that day.
    """
    try:
        zarr_path = get_zarr_path(zarr_date)
        ds = open_zarr(zarr_path)

        point_ds = ds.sel(latitude=lat, longitude=lon, method="nearest")
        init_time = pd.Timestamp(point_ds.init_time.values)

        # Calculate lead time offset for target_date 00:00 UTC
        target_midnight = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
        start_offset = int((target_midnight - init_time.to_pydatetime().replace(tzinfo=None)).total_seconds() / 3600)

        # Sum lead_times for all 24 hours of target_date
        lead_times = [pd.Timedelta(hours=h) for h in range(start_offset, start_offset + 24)]
        day_ds = point_ds.sel(lead_time=lead_times, method="nearest").compute()

        precip_values = day_ds["precipitation_surface"].values
        # Convert mm/s → mm/hour, sum 24 hours → mm/day
        total_precip = float(np.nansum(precip_values * 3600))
        return round(total_precip, 4)

    except Exception as e:
        logger.warning(f"Could not get precip from Zarr {zarr_date.strftime('%Y%m%d')} for {target_date.strftime('%Y-%m-%d')}: {e}")
        return None


@api_view(["GET"])
def get_current_weather(request):

    IST = pytz.timezone("Asia/Kolkata")

    try:
        lat = float(request.GET.get("lat"))
        lon = float(request.GET.get("lon"))
        user_datetime = request.GET.get("datetime")

        if user_datetime:
            user_dt = pd.to_datetime(user_datetime)
            user_dt = IST.localize(user_dt)
            user_dt = user_dt.astimezone(pytz.UTC).replace(tzinfo=None)
        else:
            user_dt = datetime.utcnow()

    except Exception as e:
        return Response(
            {"error": f"Invalid parameters: {str(e)}"},
            status=status.HTTP_400_BAD_REQUEST
        )

    if not (MIN_LAT <= lat <= MAX_LAT and MIN_LON <= lon <= MAX_LON):
        return Response(
            {"error": "Coordinates outside India domain"},
            status=status.HTTP_400_BAD_REQUEST
        )

    now = datetime.utcnow()
    max_forecast_date = now + timedelta(days=FORECAST_DAYS_LIMIT)

    if user_dt > max_forecast_date:
        return Response(
            {"error": f"Date outside {FORECAST_DAYS_LIMIT}-day forecast range"},
            status=status.HTTP_400_BAD_REQUEST
        )

    lead_time_hours = (user_dt - now).total_seconds() / 3600

    if lead_time_hours <= 120:
        lead_time_hours = round(lead_time_hours)
    else:
        lead_time_hours = round(lead_time_hours / 3) * 3

    lead_time_td = pd.Timedelta(hours=int(lead_time_hours))

    zarr_path = resolve_zarr_path()
    if not zarr_path:
        return Response(
            {"error": "Forecast data not available. Please try again later."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE
        )

    try:
        ds = open_zarr(zarr_path)

        point_data = ds.sel(
            lead_time=lead_time_td,
            latitude=lat,
            longitude=lon,
            method="nearest"
        )

        def get_var(name):
            if name not in point_data:
                return None
            value = point_data[name].values
            if value is None:
                return None
            value = float(value)
            if np.isnan(value):
                return None
            return value

        temp_2m = get_var("temperature_2m")
        max_temp_2m = get_var("maximum_temperature_2m")
        min_temp_2m = get_var("minimum_temperature_2m")

        precip_raw = get_var("precipitation_surface")
        precip = round(precip_raw * 3600, 4) if precip_raw is not None else None

        u = float(point_data.wind_u_10m.values) if "wind_u_10m" in point_data else None
        v = float(point_data.wind_v_10m.values) if "wind_v_10m" in point_data else None

        if u is not None and v is not None:
            wind_speed = round(float(np.sqrt(u**2 + v**2)), 4)
            wind_direction = round(float(np.degrees(np.arctan2(v, u)) % 360), 4)
        else:
            wind_speed = None
            wind_direction = None

        result = {
            "requested": {
                "latitude": lat,
                "longitude": lon,
                "datetime": user_dt.isoformat() + "Z",
            },
            "forecast": {
                "temperature_2m_c": temp_2m,
                "maximum_temperature_2m_c": max_temp_2m,
                "minimum_temperature_2m_c": min_temp_2m,
                "precipitation_mm_per_hour": precip,
                "wind_speed_mps": wind_speed,
                "wind_direction_deg": wind_direction,
            },
            "units": {
                "time": "iso8601",
                "temperature_2m": "°C",
                "wind_speed": "m/s",
                "precipitation": "mm/h",
                "wind_direction": "degree"
            }
        }

        return Response(result, status=status.HTTP_200_OK)

    except Exception as e:
        logger.exception("Forecast data error")
        return Response(
            {"error": f"Forecast data not available: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(["GET"])
def get_forecast_5day(request):
    try:
        lat = float(request.GET.get("lat"))
        lon = float(request.GET.get("lon"))

    except Exception as e:
        return Response(
            {"error": f"Invalid parameters: {str(e)}"},
            status=status.HTTP_400_BAD_REQUEST
        )

    if not (MIN_LAT <= lat <= MAX_LAT and MIN_LON <= lon <= MAX_LON):
        return Response(
            {"error": "Coordinates outside India domain"},
            status=status.HTTP_400_BAD_REQUEST
        )

    zarr_path = resolve_zarr_path()
    if not zarr_path:
        return Response(
            {"error": "Forecast data not available. Please try again later."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE
        )

    try:
        ds = open_zarr(zarr_path)

        point_ds = ds.sel(
            latitude=lat,
            longitude=lon,
            method="nearest"
        )

        actual_lat = float(point_ds.latitude.values)
        actual_lon = float(point_ds.longitude.values)
        init_time = pd.Timestamp(point_ds.init_time.values)

        # Start from midnight UTC of current day
        today_midnight = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        start_offset_hours = int((today_midnight - init_time.to_pydatetime().replace(tzinfo=None)).total_seconds() / 3600)

        hourly_lead_times = [
            pd.Timedelta(hours=h)
            for h in range(start_offset_hours, start_offset_hours + HOURLY_FORECAST_HOURS + 1)
        ]

        hourly_ds = point_ds.sel(
            lead_time=hourly_lead_times,
            method="nearest"
        ).compute()

        valid_times = [
            (init_time + lt).strftime("%Y-%m-%dT%H:%M")
            for lt in hourly_ds.lead_time.values
        ]

        def extract_array(name: str) -> list:
            if name not in hourly_ds:
                return [None] * len(valid_times)
            arr = hourly_ds[name].values.tolist()
            return [None if (v is None or np.isnan(v)) else round(v, 4) for v in arr]

        def extract_precip() -> list:
            if "precipitation_surface" not in hourly_ds:
                return [None] * len(valid_times)
            arr = hourly_ds["precipitation_surface"].values
            return [None if (v is None or np.isnan(v)) else round(float(v) * 3600, 4) for v in arr]

        temp_arr = extract_array("temperature_2m")
        precip_arr = extract_precip()

        u_arr = hourly_ds["wind_u_10m"].values if "wind_u_10m" in hourly_ds else None
        v_arr = hourly_ds["wind_v_10m"].values if "wind_v_10m" in hourly_ds else None

        if u_arr is not None and v_arr is not None:
            wind_speed_arr = [
                round(float(np.sqrt(u**2 + v**2)), 4) if not (np.isnan(u) or np.isnan(v)) else None
                for u, v in zip(u_arr, v_arr)
            ]
            wind_direction_arr = [
                round(float(np.degrees(np.arctan2(v, u)) % 360), 4) if not (np.isnan(u) or np.isnan(v)) else None
                for u, v in zip(u_arr, v_arr)
            ]
        else:
            wind_speed_arr = [None] * len(valid_times)
            wind_direction_arr = [None] * len(valid_times)

        # Current: closest timestep to now
        now_ts = pd.Timestamp(datetime.utcnow())
        valid_timestamps = [pd.Timestamp(t) for t in valid_times]
        current_idx = int(np.argmin([abs((ts - now_ts).total_seconds()) for ts in valid_timestamps]))

        current = {
            "time": valid_times[current_idx],
            "temperature_2m_c": temp_arr[current_idx],
            "precipitation_mm_per_hour": precip_arr[current_idx],
            "wind_speed_mps": wind_speed_arr[current_idx],
            "wind_direction_deg": wind_direction_arr[current_idx],
        }

        hourly = {
            "time": valid_times,
            "temperature_2m_c": temp_arr,
            "precipitation_mm_per_hour": precip_arr,
            "wind_speed_mps": wind_speed_arr,
            "wind_direction_deg": wind_direction_arr,
        }

        result = {
            "requested": {
                "latitude": lat,
                "longitude": lon,
            },
            "nearest_grid_point": {
                "latitude": actual_lat,
                "longitude": actual_lon,
            },
            "units": {
                "time": "iso8601",
                "temperature_2m": "°C",
                "wind_speed": "m/s",
                "precipitation": "mm/h",
                "wind_direction": "degree"
            },
            "current": current,
            "hourly": hourly,
        }

        return Response(result, status=status.HTTP_200_OK)

    except Exception as e:
        logger.exception("5-day forecast data error")
        return Response(
            {"error": f"Forecast data not available: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(["GET"])
def get_forecast_15day(request):
    try:
        lat = float(request.GET.get("lat"))
        lon = float(request.GET.get("lon"))

    except Exception as e:
        return Response(
            {"error": f"Invalid parameters: {str(e)}"},
            status=status.HTTP_400_BAD_REQUEST
        )

    if not (MIN_LAT <= lat <= MAX_LAT and MIN_LON <= lon <= MAX_LON):
        return Response(
            {"error": "Coordinates outside India domain"},
            status=status.HTTP_400_BAD_REQUEST
        )

    zarr_path = resolve_zarr_path()
    if not zarr_path:
        return Response(
            {"error": "Forecast data not available. Please try again later."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE
        )

    try:
        ds = open_zarr(zarr_path)

        point_ds = ds.sel(
            latitude=lat,
            longitude=lon,
            method="nearest"
        )

        actual_lat = float(point_ds.latitude.values)
        actual_lon = float(point_ds.longitude.values)
        init_time = pd.Timestamp(point_ds.init_time.values)

        # Start from midnight UTC of current day
        today_midnight = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        start_offset_hours = int((today_midnight - init_time.to_pydatetime().replace(tzinfo=None)).total_seconds() / 3600)

        # BLOCK 1: today_midnight to today_midnight+120h (hourly) → 3-hourly
        hourly_lead_times = [
            pd.Timedelta(hours=h)
            for h in range(start_offset_hours, start_offset_hours + HOURLY_FORECAST_HOURS + 1)
        ]
        hourly_ds = point_ds.sel(
            lead_time=hourly_lead_times,
            method="nearest"
        ).compute()

        hourly_temp = hourly_ds["temperature_2m"].values
        hourly_precip = hourly_ds["precipitation_surface"].values
        hourly_u = hourly_ds["wind_u_10m"].values if "wind_u_10m" in hourly_ds else None
        hourly_v = hourly_ds["wind_v_10m"].values if "wind_v_10m" in hourly_ds else None
        hourly_lead = hourly_ds.lead_time.values

        block1_times = []
        block1_temp = []
        block1_precip = []
        block1_wind_speed = []
        block1_wind_dir = []

        for i in range(0, HOURLY_FORECAST_HOURS, 3):
            window_temp = hourly_temp[i:i+3]
            window_precip = hourly_precip[i:i+3]

            block1_times.append((init_time + hourly_lead[i]).strftime("%Y-%m-%dT%H:%M"))

            t_val = float(window_temp[0])
            block1_temp.append(None if np.isnan(t_val) else round(t_val, 4))

            p_vals = [float(v) * 3600 for v in window_precip if not np.isnan(v)]
            block1_precip.append(round(sum(p_vals), 4) if p_vals else None)

            if hourly_u is not None and hourly_v is not None:
                window_u = hourly_u[i:i+3]
                window_v = hourly_v[i:i+3]
                speeds = [
                    float(np.sqrt(u**2 + v**2))
                    for u, v in zip(window_u, window_v)
                    if not (np.isnan(u) or np.isnan(v))
                ]
                dirs = [
                    float(np.degrees(np.arctan2(v, u)) % 360)
                    for u, v in zip(window_u, window_v)
                    if not (np.isnan(u) or np.isnan(v))
                ]
                block1_wind_speed.append(round(float(np.mean(speeds)), 4) if speeds else None)
                block1_wind_dir.append(round(float(np.mean(dirs)), 4) if dirs else None)
            else:
                block1_wind_speed.append(None)
                block1_wind_dir.append(None)

        # BLOCK 2: today_midnight+123h to today_midnight+360h (already 3-hourly)
        extended_lead_times = [
            pd.Timedelta(hours=h)
            for h in range(start_offset_hours + 123, start_offset_hours + EXTENDED_FORECAST_HOURS + 1, 3)
        ]
        extended_ds = point_ds.sel(
            lead_time=extended_lead_times,
            method="nearest"
        ).compute()

        ext_temp = extended_ds["temperature_2m"].values
        ext_precip = extended_ds["precipitation_surface"].values
        ext_u = extended_ds["wind_u_10m"].values if "wind_u_10m" in extended_ds else None
        ext_v = extended_ds["wind_v_10m"].values if "wind_v_10m" in extended_ds else None
        ext_lead = extended_ds.lead_time.values

        block2_times = [
            (init_time + lt).strftime("%Y-%m-%dT%H:%M") for lt in ext_lead
        ]
        block2_temp = [
            None if np.isnan(float(v)) else round(float(v), 4) for v in ext_temp
        ]
        block2_precip = [
            None if np.isnan(float(v)) else round(float(v) * 3600 * 3, 4) for v in ext_precip
        ]

        if ext_u is not None and ext_v is not None:
            block2_wind_speed = [
                None if (np.isnan(u) or np.isnan(v)) else round(float(np.sqrt(u**2 + v**2)), 4)
                for u, v in zip(ext_u, ext_v)
            ]
            block2_wind_dir = [
                None if (np.isnan(u) or np.isnan(v)) else round(float(np.degrees(np.arctan2(v, u)) % 360), 4)
                for u, v in zip(ext_u, ext_v)
            ]
        else:
            block2_wind_speed = [None] * len(block2_times)
            block2_wind_dir = [None] * len(block2_times)

        all_times = block1_times + block2_times
        all_temp = block1_temp + block2_temp
        all_precip = block1_precip + block2_precip
        all_wind_speed = block1_wind_speed + block2_wind_speed
        all_wind_dir = block1_wind_dir + block2_wind_dir

        now_ts = pd.Timestamp(datetime.utcnow())
        valid_timestamps = [pd.Timestamp(t) for t in all_times]
        current_idx = int(np.argmin([abs((ts - now_ts).total_seconds()) for ts in valid_timestamps]))

        current = {
            "time": all_times[current_idx],
            "temperature_2m_c": all_temp[current_idx],
            "precipitation_mm_per_3h": all_precip[current_idx],
            "wind_speed_mps": all_wind_speed[current_idx],
            "wind_direction_deg": all_wind_dir[current_idx],
        }

        forecast_3hourly = {
            "time": all_times,
            "temperature_2m_c": all_temp,
            "precipitation_mm_per_3h": all_precip,
            "wind_speed_mps": all_wind_speed,
            "wind_direction_deg": all_wind_dir,
        }

        result = {
            "requested": {"latitude": lat, "longitude": lon},
            "nearest_grid_point": {"latitude": actual_lat, "longitude": actual_lon},
            "units": {
                "time": "iso8601",
                "temperature_2m": "°C",
                "wind_speed": "m/s",
                "precipitation": "mm/3h",
                "wind_direction": "degree"
            },
            "current": current,
            "forecast_3hourly": forecast_3hourly,
        }

        return Response(result, status=status.HTTP_200_OK)

    except Exception as e:
        logger.exception("15-day forecast data error")
        return Response(
            {"error": f"Forecast data not available: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(["GET"])
def get_historic_forecast(request):
    try:
        lat = float(request.GET.get("lat"))
        lon = float(request.GET.get("lon"))

    except Exception as e:
        return Response(
            {"error": f"Invalid parameters: {str(e)}"},
            status=status.HTTP_400_BAD_REQUEST
        )

    if not (MIN_LAT <= lat <= MAX_LAT and MIN_LON <= lon <= MAX_LON):
        return Response(
            {"error": "Coordinates outside India domain"},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    timing = {}
    # ----------------------------------------------------------------
    # STEP 1: Define 15-day window (today-5 to today+10)
    # ----------------------------------------------------------------
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    window_start = today - timedelta(days=5)  # 5 days ago
    window_end = today + timedelta(days=10)   # 10 days ahead
    window_days = [window_start + timedelta(days=i) for i in range(15)]

    # ----------------------------------------------------------------
    # STEP 2: Fetch MWS ID and GeoServer fortnight data
    # ----------------------------------------------------------------
    t0 = time.perf_counter()
    try:
        mws_id_url = f"https://geoserver.core-stack.org/api/v1/get_mwsid_by_latlon/?latitude={lat}&longitude={lon}"
        response_mws_id = requests.get(mws_id_url, headers={"X-API-Key": settings.X_API_KEY}, timeout=15)
        response_mws_id = response_mws_id.json()
    except Exception as e:
        return Response(
            {"error": f"Not able to fetch MWS Data: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )

    try:
        base_url = f"{settings.GEOSERVER_URL}/mws_layers/ows"
        params = {
            "service": "WFS",
            "version": "1.0.0",
            "request": "GetFeature",
            "typeName": f"mws_layers:deltaG_fortnight_{response_mws_id['District'].lower()}_{response_mws_id['Tehsil'].lower()}",
            "outputFormat": "application/json",
            "CQL_FILTER": f"uid='{response_mws_id['mws_id']}'",
        }
        response = requests.get(base_url, params=params, timeout=30)
        geoserver_data = response.json()
    except Exception as e:
        return Response(
            {"error": f"Not able to fetch Fortnight MWS layer data: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )

    timing["mws_id_fetch_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    print(f"[PROFILING] MWS ID fetch: {timing['mws_id_fetch_ms']} ms")

    # ----------------------------------------------------------------
    # STEP 3: Parse fortnight properties into a structured dict
    # Fortnight key format: "YYYY-MM-DD", value is JSON string with Precipitation
    # Each entry covers 14 days starting from its key date
    # ----------------------------------------------------------------
    t0 = time.perf_counter()
    try:
        properties = geoserver_data["features"][0]["properties"]

        # Extract all fortnight entries (skip non-date keys)
        fortnight_entries = {}
        for key, value in properties.items():
            try:
                entry_date = datetime.strptime(key, "%Y-%m-%d")
                parsed = json.loads(value)
                fortnight_entries[entry_date] = parsed.get("Precipitation", 0.0)
            except (ValueError, TypeError):
                continue  # skip non-date keys like area_in_ha, bacode etc.

        # Sort by date
        fortnight_entries = dict(sorted(fortnight_entries.items()))
        fortnight_dates = list(fortnight_entries.keys())

    except Exception as e:
        return Response(
            {"error": f"Failed to parse GeoServer fortnight data: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )

    timing["geoserver_fetch_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    print(f"[PROFILING] GeoServer fetch: {timing['geoserver_fetch_ms']} ms")

    # ----------------------------------------------------------------
    # STEP 4: Find fortnight entries that OVERLAP with our 15-day window
    # Each fortnight covers [entry_date, entry_date + 14 days)
    # ----------------------------------------------------------------
    def get_overlapping_fortnights(year_offset: int) -> list[dict]:
        """
        Find fortnight entries for a given year that overlap with
        our 15-day window (month/day shifted to target year).
        Returns list of {date, precipitation, overlap_days}
        """
        # Shift our window to the target year
        t0 = time.perf_counter()
        target_year = window_start.year + year_offset
        try:
            shifted_start = window_start.replace(year=target_year)
            shifted_end = window_end.replace(year=target_year)
        except ValueError:
            # Handle Feb 29 edge case
            shifted_start = window_start.replace(year=target_year, day=28)
            shifted_end = window_end.replace(year=target_year, day=28)

        overlapping = []
        for ft_date, precip in fortnight_entries.items():
            if ft_date.year != target_year:
                continue
            ft_end = ft_date + timedelta(days=14)

            # Check overlap: fortnight window overlaps our shifted window
            overlap_start = max(ft_date, shifted_start)
            overlap_end = min(ft_end, shifted_end)
            overlap_days = (overlap_end - overlap_start).days

            if overlap_days > 0:
                overlapping.append({
                    "fortnight_date": ft_date.strftime("%Y-%m-%d"),
                    "precipitation_total_mm": precip,
                    "overlap_days": overlap_days,
                    # Proportional precipitation for the overlapping days
                    "precipitation_overlap_mm": round(precip * (overlap_days / 14), 4)
                }) 
        
        timing["fortnight_parse_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        print(f"[PROFILING] Fortnight parsing: {timing['fortnight_parse_ms']} ms")

        return overlapping


    # ----------------------------------------------------------------
    # STEP 5: Build historic year-by-year comparison (2018–2025)
    # ----------------------------------------------------------------
    historic_years = {}
    base_year = window_start.year  # e.g. 2026

    for year in range(2018, base_year):
        year_offset = year - base_year
        overlapping = get_overlapping_fortnights(year_offset)

        if not overlapping:
            historic_years[str(year)] = {
                "fortnight_blocks": [],
                "total_precipitation_mm": None,
            }
            continue

        total_precip = round(sum(f["precipitation_overlap_mm"] for f in overlapping), 4)
        historic_years[str(year)] = {
            "fortnight_blocks": overlapping,
            "total_precipitation_mm": total_precip,
        }

    # ----------------------------------------------------------------
    # STEP 6: Build 2026 actual precipitation from Zarrs
    # Past 5 days: use each day's own Zarr
    # Today + next 10 days: use today's Zarr
    # ----------------------------------------------------------------
    today_zarr_date = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    daily_precip_2026 = {}

    for day in window_days:
        date_str = day.strftime("%Y-%m-%d")

        if day < today_zarr_date:
            # Past days: use that day's own Zarr
            precip = get_daily_precip_from_zarr(
                zarr_date=day,
                target_date=day,
                lat=lat,
                lon=lon
            )
        else:
            # Today and future: use today's Zarr
            precip = get_daily_precip_from_zarr(
                zarr_date=today_zarr_date,
                target_date=day,
                lat=lat,
                lon=lon
            )

        daily_precip_2026[date_str] = precip

    # Sum all available daily precip values for 2026 total
    precip_values_2026 = [v for v in daily_precip_2026.values() if v is not None]
    total_precip_2026 = round(sum(precip_values_2026), 4) if precip_values_2026 else None

    # ----------------------------------------------------------------
    # STEP 7: Build response
    # ----------------------------------------------------------------
    total_api_time = sum(timing.values())
    print(f"[PROFILING] Total API time (excl. Zarr): {round(total_api_time, 2)} ms | breakdown: {timing}")

    result = {
        "requested": {
            "latitude": lat,
            "longitude": lon,
        },
        "mws_info": {
            "mws_id": response_mws_id.get("mws_id"),
            "district": response_mws_id.get("District"),
            "tehsil": response_mws_id.get("Tehsil"),
        },
        "window": {
            "start": window_start.strftime("%Y-%m-%d"),
            "end": window_end.strftime("%Y-%m-%d"),
            "days": 15,
        },
        "units": {
            "precipitation": "mm",
        },
        "forecast_2026": {
            "daily_precipitation_mm": daily_precip_2026,
            "total_precipitation_mm": total_precip_2026,
        },
        "historic_comparison": historic_years,
    }

    return Response(result, status=status.HTTP_200_OK)