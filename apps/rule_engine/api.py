import os
from django.conf import settings

import pandas as pd
from datetime import datetime
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status


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


def safe_value(row, column_index):
    val = row.iloc[column_index]

    if pd.isna(val):
        return None

    return val


#* MARK: API Endpoints
@api_view(["POST"])
def crop_rule_engine(request):

    try:
        crop = request.GET.get("crop")
        sowing_date = request.GET.get("sowing_date")

        if not crop:
            return Response(
                {"error": "crop parameter is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        sowing_date = datetime.strptime(sowing_date, "%Y-%m-%d").date()
        today = datetime.utcnow().date()

        days_after_sowing = (today - sowing_date).days

    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    # Build CSV path dynamically from crop name
    csv_path = os.path.join(
        settings.BASE_DIR,
        "apps",
        "rule_engine",
        "data",
        f"{crop}.csv"
    )

    if not os.path.exists(csv_path):
        return Response(
            {"error": f"Rules file not found for crop: {crop}"},
            status=status.HTTP_404_NOT_FOUND
        )

    # Load CSV
    df = pd.read_csv(csv_path)

    # remove metadata rows
    df = df.iloc[4:].reset_index(drop=True)


    for _, row in df.iterrows():

        das_text = row.iloc[1]

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
                    "no_rainfall": safe_value(row, 3),
                    "light_rainfall": safe_value(row, 4),
                    "moderate_rainfall": safe_value(row, 5),
                    "heavy_rainfall": safe_value(row, 6),
                    "risk_level": safe_value(row, 9)

                }
            })

    return Response({"message": "No advisory block found"}, status=404)