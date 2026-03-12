import os
from django.conf import settings

import pandas as pd
from datetime import datetime
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

CSV_PATH = os.path.join(
    settings.BASE_DIR,
    "apps",
    "rule_engine",
    "data",
    "advisory_rules.csv"
)

#? MARK: Helpers
def parse_das_range(text):
    text = str(text).strip()

    if "to" in text:
        start, end = text.split("to")
        start = int(start.replace("t+", "").strip())
        end = int(end.replace("t+", "").strip())
        return start, end

    if "t+" in text:
        val = int(text.replace("t+", "").strip())
        return val, val

    return None

def safe_value(row, column):
    val = row[column]

    # if duplicate columns return a Series, take first value
    if isinstance(val, pd.Series):
        val = val.iloc[0]

    if pd.isna(val):
        return None

    return val


#* MARK: API Endpoints
@api_view(["GET"])
def crop_rule_engine(request):

    try:
        crop = request.GET.get("crop")
        sowing_date = request.GET.get("sowing_date")

        sowing_date = datetime.strptime(sowing_date, "%Y-%m-%d").date()
        today = datetime.utcnow().date()

        days_after_sowing = (today - sowing_date).days

    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    # Load CSV
    df = pd.read_csv(CSV_PATH)

    # Fix header row
    header = df.iloc[4]
    df = df[5:]
    df.columns = header

    for _, row in df.iterrows():

        das_text = row["Days After Sowing (DAS)\n(t-n < t0 < t+n)"]

        rng = parse_das_range(das_text)

        if rng is None:
            continue

        start, end = rng

        if start <= days_after_sowing <= end:

            return Response({
                "crop": crop,
                "days_after_sowing": days_after_sowing,
                "matched_block": das_text,
                "advisory": {
                    "no_rainfall": safe_value(row, "No Rainfall (t+x)"),
                    "light_rainfall": safe_value(row, "Light Rainfall (t+x)"),
                    "moderate_rainfall": safe_value(row, "Moderate Rainfall (t+x)"),
                    "heavy_rainfall": safe_value(row, "Heavy Rainfall (t+x)"),
                    "risk_level": safe_value(row, "Risk Level")
                }
            })

    return Response({"message": "No advisory block found"}, status=404)

