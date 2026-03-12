from django.contrib import admin
from django.urls import path
from portal import views

urlpatterns = [
    path('',views.dashboard,name="dashboard"),
    path("/upload",views.upload,name="upload")
]