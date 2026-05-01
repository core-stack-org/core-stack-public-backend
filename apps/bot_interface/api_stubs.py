
import random
import requests
from django.conf import settings
import math
from datetime import datetime, timedelta
from config.settings import GEOSERVER_URL, PRODUCTION_API_SERVER_URL
import numpy as np
from django.db.models import Q
from scipy.spatial import cKDTree
from rest_framework.response import Response
from rest_framework import status


def get_location_based_plan_properties(lon, lat):
    base_url = "https://geoserver.core-stack.org:8443/geoserver/wfs"
    layer_name = "pan_india_asset:plans_plan"

    RADIUS_METERS = 1500  # 11km radius

    cql_filter = f"DWITHIN(geom, POINT({lon} {lat}), {RADIUS_METERS}, meters)"

    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeName": layer_name,
        "outputFormat": "application/json",
        "CQL_FILTER": cql_filter,
    }

    try:
        r = requests.get(base_url, params=params)
        r.raise_for_status()

        features = r.json().get("features", [])

        if not features:
            return Response([], status=status.HTTP_200_OK)

        coords, valid_features = [], []
        for f in features:
            geometry = f.get("geometry")
            if not geometry:
                continue
            point_lon, point_lat = geometry["coordinates"]
            coords.append([point_lat, point_lon])
            valid_features.append(f)

        if not coords:
            return Response([], status=status.HTTP_200_OK)

        coords_np = np.array(coords)
        tree = cKDTree(coords_np)

        k = min(5, len(coords_np))
        _, indices = tree.query([lat, lon], k=k)

        if k == 1:
            indices = [indices]

        result = [valid_features[i]["properties"] for i in indices]
        return Response(result, status=status.HTTP_200_OK)

    except Exception as e:
        print("Error:", str(e))
        return Response({"error": "Failed to fetch nearest plans"}, status=500)


def get_villages_by_location(lat, lon):
    """Fetch nearest 5 villages from API, fallback to GeoServer if API fails."""
    url = f"{PRODUCTION_API_SERVER_URL}/api/v1/get_plan_by_lat_lon/"
    params = {"latitude": lat, "longitude": lon}

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        return [
            {**v, "id": v.get("id"), "name": v.get("village_na")}
            for v in data[:5]
        ]

    except Exception as e:
        print(f"DEBUG: Primary API failed, switching to fallback: {e}")

        try:
            fallback_response = get_location_based_plan_properties(lon, lat)
            fallback_data = getattr(fallback_response, "data", [])

            return [
                {**v, "id": v.get("id"), "name": v.get("village_na")}
                for v in fallback_data[:5]
            ]

        except Exception as fallback_error:
            print(f"DEBUG: Fallback also failed: {fallback_error}")
            return []

def check_user_village(phone):
    """Check if user exists in a community via API."""
    url = f"{PRODUCTION_API_SERVER_URL}/api/v1/is_user_in_community/"
    # Form data request as seen in Postman screenshot
    data = {"number": phone}
    
    try:
        response = requests.post(url, data=data, timeout=10)
        response.raise_for_status()
        res_json = response.json()

        print("is user in community response", res_json)
        
        if res_json.get("success") and res_json.get("data", {}).get("is_in_community"):
            data_dict = res_json.get("data", {})
            # Try to get community_id from the nested data list
            nested_data = data_dict.get("data", [])
            if nested_data and isinstance(nested_data, list) and len(nested_data) > 0:
                community_id = nested_data[0].get("community_id")
                community_name = nested_data[0].get("name")
                plan_id = nested_data[0].get("id")
                plan_name = nested_data[0].get("plan")
                if community_id:
                    return {
                        "id": community_id, 
                        "name": community_name or "registered",
                        "plan_id": plan_id,
                        "plan_name": plan_name
                    }
            
            # Fallback to misc data
            community_id = data_dict.get("misc", {}).get("last_accessed_community_id")
            if community_id:
                return {"id": community_id, "name": "registered"}
                
            # Final fallback
            return {"id": "registered", "name": "registered"}
        return None
    except Exception as e:
        print(f"DEBUG: check_user_village API call failed: {e}")
        return None

def join_village(phone, village_id):
    """Hardcoded join operation."""
    print(f"DEBUG: User {phone} joined village {village_id}")
    return True

def create_asset_demand(user_id, village_id):
    """Hardcoded asset demand creation response."""
    return {
        "status": "success",
        "message": f"Asset demand request created successfully for village {village_id}!"
    }

def create_story(user_id, village_id):
    """Hardcoded story creation response."""
    return {
        "status": "success",
        "message": f"Story created successfully in village {village_id}!"
    }

def get_weather_forecast(lat, lon, days=5):
    """Call weather API for forecast data."""
    
    # User provided API URL
    url = "https://onix.core-stack.org/api/v1/weather/forecast/5-day/"
    params = {"lat": lat, "lon": lon}
    
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"DEBUG: Weather API call failed: {e}. Returning dummy data.")
        
        # Fallback dummy data
        base_time = datetime.now()
        times = [(base_time + timedelta(hours=i)).isoformat() for i in range(days * 24)]
        
        temperatures = [
            round(25 + 5 * math.sin(i * math.pi / 12) + random.uniform(-1, 1), 1)
            for i in range(days * 24)
        ]
        
        precipitation = [
            round(random.uniform(0, 2) if random.random() > 0.8 else 0, 1)
            for i in range(days * 24)
        ]
        
        return {
            "hourly": {
                "time": times,
                "temperature_2m_c": temperatures,
                "precipitation_mm_per_hour": precipitation
            }
        }

def get_crop_advisory(crop_name, sowing_date, lat, lon):
    """Hardcoded crop advisory."""
    return f"Advisory for {crop_name} (Sown: {sowing_date}): Your crop is in its early growth stage. Ensure adequate moisture and monitor for aphids."

def fetch_asset_demands(village_id):
    """Hardcoded list of demands."""
    return [
        {"id": 1, "asset": "Borewell", "status": "Pending"},
        {"id": 2, "asset": "Solar Pump", "status": "Approved"}
    ]

def fetch_stories(village_id):
    """Hardcoded stories."""
    return [
        {"id": 10, "title": "Success Story", "content": "Farmer X improved yield by 20% using solar pumps."},
        {"id": 11, "title": "Community Effort", "content": "Village DEF cleaned the local pond together."}
    ]
