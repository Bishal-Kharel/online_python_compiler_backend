from django.contrib import admin
from .models import Snippet, Hint

admin.site.register(Snippet)
admin.site.register(Hint)