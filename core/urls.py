from django.urls import path
from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('navigator/', views.navigator, name='navigator'),
    path('mentor/', views.mentor, name='mentor'),
    path('mentor/toggle-step/', views.mentor_toggle_step, name='mentor_toggle_step'),
    path('issue/', views.issue2contribution, name='issue2contribution'),
]
