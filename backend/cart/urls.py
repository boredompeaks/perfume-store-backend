from django.urls import path

from .views import cart_detail, cart_item_detail


urlpatterns = [
    path('', cart_detail, name='cart-detail'),
    path('<int:item_id>/', cart_item_detail, name='cart-item-detail'),
]