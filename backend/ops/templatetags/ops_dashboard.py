from django import template

from ..services import get_health, get_stats

register = template.Library()


@register.inclusion_tag("ops/dashboard_cards.html")
def ops_dashboard_cards():
    return {"health": get_health(), "stats": get_stats()}
