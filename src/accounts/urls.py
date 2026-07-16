from django.contrib.auth import views as auth_views
from django.urls import path

from .views import DashboardView

app_name = 'accounts'

urlpatterns = [
    path('', DashboardView.as_view(), name='dashboard'),
    path(
        'accounts/login/',
        auth_views.LoginView.as_view(template_name='registration/login.html'),
        name='login',
    ),
    path('accounts/logout/', auth_views.LogoutView.as_view(), name='logout'),
]
