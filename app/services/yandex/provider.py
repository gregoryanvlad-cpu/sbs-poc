
class MockYandexProvider:
    async def create_invite_link(self, credentials_ref=None):
        return "https://yandex.ru/invite/mock"

    async def fetch_family_members(self, credentials_ref=None):
        return []

    async def kick_member(self, credentials_ref=None, login=None):
        return
