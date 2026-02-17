from django.http import HttpRequest

from accounts.models import CustomUser


class AuthenticatedRequest(HttpRequest):
    user: CustomUser
