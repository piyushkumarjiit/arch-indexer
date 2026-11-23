from . import users


@users.get("/users")
def list_users():
    return []
