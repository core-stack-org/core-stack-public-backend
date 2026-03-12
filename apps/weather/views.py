from rest_framework.views import APIView
from rest_framework.response import Response


class WeatherTestView(APIView):
    def get(self, request):
        return Response({
            "message": "Weather API is working",
            "status": "success"
        })