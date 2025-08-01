from django.conf import settings

from apps.users.models import User


def is_user_registration_open() -> bool:
    return settings.ENABLE_USER_REGISTRATION or not User.objects.exists()


async def ais_user_registration_open() -> bool:
    return settings.ENABLE_USER_REGISTRATION or not await User.objects.aexists()
