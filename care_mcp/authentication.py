import re

from rest_framework.authentication import TokenAuthentication, get_authorization_header

# Care's service-account tokens are DRF auth tokens: 40 lowercase hex characters.
SERVICE_ACCOUNT_TOKEN = re.compile(r"^[0-9a-f]{40}$")


class ServiceAccountBearerAuthentication(TokenAuthentication):
    """Accept a Care service-account token as ``Authorization: Bearer <token>``.

    Care issues service-account tokens for ``Authorization: Token <token>``, which
    works here too. Many MCP clients only offer a bearer-token field, so the same
    token is also accepted with the Bearer keyword. Bearer values that are not
    service-account tokens (Care's JWTs) are left for the JWT authenticator.
    """

    def authenticate(self, request):
        auth = get_authorization_header(request).split()
        if len(auth) != 2 or auth[0].lower() != b"bearer":  # noqa: PLR2004
            return None
        try:
            key = auth[1].decode()
        except UnicodeError:
            return None
        if not SERVICE_ACCOUNT_TOKEN.match(key):
            return None
        return self.authenticate_credentials(key)

    def authenticate_header(self, request):
        return 'Bearer realm="care"'
