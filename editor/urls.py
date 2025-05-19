from django.urls import path
from . import views

urlpatterns = [
    path('snippets/', views.SnippetList.as_view(), name='snippet-list'),
    path('hints/', views.HintList.as_view(), name='hint-list'),
]
