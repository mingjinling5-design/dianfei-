from django.urls import path
from portal.accounts import views

urlpatterns=[
    path('source',views.source),
    path('admin/users',views.admin_users),
    path('api/site/session',views.site_session),
    path('api/site/logout',views.logout),
    path('api/site/disconnect',views.disconnect),
    path('api/eschool/<str:action>',views.eschool),
    path('api/<path:path>',views.api),
    path('',views.page),
    path('<path:path>',views.page),
]
