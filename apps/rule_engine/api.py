import os
import json
from datetime import datetime

from django.conf import settings

from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status


RULES_DIR = os.path.join(
    settings.BASE_DIR,
    "apps",
    "rule_engine",
    "data",
)


# MARK: Helper - Evaluate Rule Condition
def evaluate_condition(value, op_type, low, high):

    if op_type in ("between", "1"):
        return low <= value <= high

    elif op_type == "<":
        return value < high

    elif op_type == ">":
        return value > low

    elif op_type == "<=":
        return value <= high

    elif op_type == ">=":
        return value >= low

    elif op_type == "=":
        return value == low

    return False


# MARK: API Endpoint
@api_view(["POST"])
def crop_rule_engine(request):

    try:
        crop = request.GET.get("crop")
        district = request.GET.get("district")
        sowing_date_str = request.GET.get("sowing_date")
        state = request.GET.get("state")

        weather_data = request.data

        if not state:
            return Response(
                {"error": "state parameter is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not crop:
            return Response(
                {"error": "crop parameter is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not district:
            return Response(
                {"error": "district parameter is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not sowing_date_str:
            return Response(
                {"error": "sowing_date parameter is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        sowing_date = datetime.strptime(sowing_date_str, "%Y-%m-%d").date()
        today = datetime.utcnow().date()

        days_after_sowing = (today - sowing_date).days

    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


    # MARK: Load State Rules Dynamically
    rules_path = os.path.join(RULES_DIR, f"{state}.json")

    if not os.path.exists(rules_path):
        return Response(
            {"error": f"No rules found for state: {state}"},
            status=status.HTTP_404_NOT_FOUND
        )

    with open(rules_path) as f:
        rules = json.load(f)


    # MARK: Weather Section
    forecast = weather_data.get("forecast_3hourly", {})
    precip_list = forecast.get("precipitation_mm_per_3h", [])

    # 10 days = 80 intervals of 3hr forecasts
    precip_10_days = precip_list[:80]
    total_precip_10_days = sum(precip_10_days)


    # MARK: Rule Lookup
    if district not in rules:
        return Response(
            {"error": f"District not found: {district}"},
            status=status.HTTP_404_NOT_FOUND
        )

    if crop not in rules[district]:
        return Response(
            {"error": f"Crop not found for district: {crop}"},
            status=status.HTTP_404_NOT_FOUND
        )

    crop_rules = rules[district][crop]["time_blocks"]


    # MARK: Rule Evaluation
    for block_index, block in enumerate(crop_rules):

        start = block["start_das"]
        end = block["end_das"]

        if not (start <= days_after_sowing <= end):
            continue

        rainfall_rules = block["patterns"].get("rainfall", [])

        for rule in rainfall_rules:

            op_type = rule.get("op_type")
            low = rule.get("low")
            high = rule.get("high")

            if not evaluate_condition(total_precip_10_days, op_type, low, high):
                continue

            # MARK: Transition Block
            transition_stage = block.get("transition_stage") or None
            transition_advisory = rule.get("transition_advisory") or None

            # MARK: Next Block Advisories
            next_block_advisories = None

            if block_index + 1 < len(crop_rules):
                next_block = crop_rules[block_index + 1]
                next_rainfall_rules = next_block["patterns"].get("rainfall", [])

                next_block_advisories = {
                    r.get("pt_type"): r.get("advisory")
                    for r in next_rainfall_rules
                }

            return Response({
                "state": state,
                "district": district,
                "crop": crop,

                "days_after_sowing": days_after_sowing,
                "matched_block": f"DAS {start}–{end}",

                "active_stage": {
                    "name": block.get("active_stage"),
                    "risk_level": block.get("risk_level"),
                    "pattern_type": rule.get("pt_type"),
                    "advisory": rule.get("advisory"),
                },

                "transition_stage": {
                    "name": transition_stage,
                    "transition_advisory": transition_advisory,
                } if transition_stage else None,

                "next_block_advisories": next_block_advisories,

                "rainfall_next_10_days_mm": round(total_precip_10_days, 4),
                "pattern_type": rule.get("pt_type"),
            })

    return Response(
        {"message": "No advisory rule matched for the given DAS and rainfall"},
        status=status.HTTP_404_NOT_FOUND
    )