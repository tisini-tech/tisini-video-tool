from django.urls import path

from . import views

app_name = "videotool"

urlpatterns = [
    path("", views.index, name="index"),
    path("api/config", views.config, name="config"),
    path("api/uploads", views.upload, name="upload"),
    path("api/uploads/<str:upload_id>", views.delete_upload, name="delete_upload"),
    path("api/merges", views.start_merge, name="start_merge"),
    path("api/merges/<str:job_id>", views.merge_status, name="merge_status"),
    path("api/merges/<str:job_id>/download", views.merge_file, name="merge_file"),
    path("api/downloads", views.start_download, name="start_download"),
    path("api/downloads/<str:job_id>", views.download_status, name="download_status"),
    path("api/downloads/<str:job_id>/download", views.download_file, name="download_file"),
]
