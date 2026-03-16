from rest_framework.decorators import schema
from rest_framework.response import Response
from rest_framework import status
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.permissions import AllowAny

from utilities.auth_check_decorator import api_security_check


@csrf_exempt
@api_view(["POST"])
@permission_classes([AllowAny])
def search(request):
    """
    Retrieve admin data based on given latitude and longitude coordinates.
    """
    return Response(
        {
            "status": "Success",
            "message": "Search API HIT",
        }
    )


@csrf_exempt
@api_view(["POST"])
@permission_classes([AllowAny])
def discover(request):
    """
    Retrieve admin data based on given latitude and longitude coordinates.
    """
    return Response(
        {
            "status": "Success",
            "message": "Search API HIT",
        }
    )
