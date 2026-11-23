from unittest import mock

from api.routes import users


@mock.patch("os.path.exists")
def check_exists(m):
    return m


@users.get("/fake")
def fake_route():
    return None


if __name__ == "__main__":
    check_exists()
