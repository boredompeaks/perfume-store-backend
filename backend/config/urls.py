from django.contrib import admin
from django.urls import path, include

from django.conf import settings
from django.conf.urls.static import static


urlpatterns = [

    path('admin/', 
        admin.site.urls),

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