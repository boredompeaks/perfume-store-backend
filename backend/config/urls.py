from django.contrib import admin
from django.urls import path, include

from django.conf import settings
from django.conf.urls.static import static

from ops.views import api_settings, audit_log, dashboard, health


urlpatterns = [

    # Store dashboard (staff-only) — must be registered BEFORE the admin
    # include, or the admin's URLconf swallows it and returns 404.
    path('admin/dashboard/', dashboard, name='admin-dashboard'),

    # Audit log (spec 6.12 route /admin/audit-log) — same before-the-admin
    # include rule as the dashboard above.
    path('admin/audit-log/', audit_log, name='admin-audit-log'),

    path('admin/', 
        admin.site.urls),

    path('health/', health, name='health'),
    path('api/settings/', api_settings, name='api-settings'),

    path('api/products/', 
        include('products.urls')),

    path(
        'api/cart/',
        include('cart.urls')
    ),

    path(
    'api/orders/',
    include('orders.urls')
),

path(
    'api/accounts/',
    include('accounts.urls')
),

]


urlpatterns += static(
    settings.MEDIA_URL,
    document_root=settings.MEDIA_ROOT
)