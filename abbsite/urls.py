from django.urls import path
from shortage import views

urlpatterns = [
    path("", views.shortages, name="shortages"),
    path("input/", views.input_page, name="input"),
    path("vendor-track/", views.vendor_track, name="vendor_track"),
    path("vendor-import/", views.vendor_import, name="vendor_import"),
    path("process/<str:project_id>/", views.process_project, name="process_project"),
    path("upload/<str:project_id>/", views.upload_project_report, name="upload_project_report"),
    path("register/<str:project_id>/export/", views.export_register, name="export_register"),
    path("register/<str:project_id>/delete/", views.delete_register, name="delete_register"),
    path("remarks/", views.save_remarks, name="save_remarks"),
    path("shortages/export/", views.export_shortages, name="export_shortages"),
    path("vendor-track/process/", views.process_vendor_track, name="process_vendor_track"),
    path("vendor-track/export/", views.export_vendor_request, name="export_vendor_request"),
    path("vendor-track/open-date-request/", views.open_vendor_date_request, name="open_vendor_date_request"),
    path("vendor-import/open/", views.open_vendor_register, name="open_vendor_register"),
    path("vendor-import/open-second-request/", views.open_second_vendor_date_request, name="open_second_vendor_date_request"),
    path("vendor-import/proposed-date/", views.save_vendor_proposed_date, name="save_vendor_proposed_date"),
    path("vendor-import/apply/", views.apply_vendor_dates, name="apply_vendor_dates"),
    path("register/<str:project_id>/download/", views.download_register, name="download_register"),
    path("register/<str:project_id>/open/", views.open_project_register, name="open_project_register"),
    path("health/", views.health, name="health"),
]
