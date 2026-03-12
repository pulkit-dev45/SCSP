from django.shortcuts import render
from django.http import HttpResponse
from django.http import JsonResponse
from .models import Document

# Create your views here.
def dashboard(request):
    return render(request,"dashboard.html")

def upload(request):
    return render(request,"upload.html")



def filter_data(request):
    query = request.GET.get("q")

    results = Document.objects.filter(name__icontains=query)

    data = list(results.values("id", "name"))

    return JsonResponse({"results": data})
