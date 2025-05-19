from rest_framework import generics
from .models import Snippet, Hint
from .serializers import SnippetSerializer, HintSerializer

class SnippetList(generics.ListAPIView):
    queryset = Snippet.objects.all()
    serializer_class = SnippetSerializer

class HintList(generics.ListAPIView):
    queryset = Hint.objects.all()
    serializer_class = HintSerializer
