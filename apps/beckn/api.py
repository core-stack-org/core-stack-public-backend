from rest_framework.decorators import api_view
from rest_framework.response import Response

@api_view(['POST'])
def dummy_api(request):

    print("request.data:", request.data)

    return Response({
        "status": "success"
    })
