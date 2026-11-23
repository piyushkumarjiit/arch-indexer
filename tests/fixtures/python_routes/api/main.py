from typing import Annotated

from fastapi import Depends, FastAPI, Security

from api.deps import get_current_user, get_user_service, oauth2_scheme, require_admin
from api.routes import users

app = FastAPI()
app.include_router(users)


@app.get("/me")
def me(
    svc: Annotated[object, Depends(get_user_service)],
    user: Annotated[str, Depends(get_current_user)],
):
    return user


@app.get("/admin", dependencies=[Depends(require_admin)])
def admin(token: Annotated[str, Security(oauth2_scheme)]):
    return token


@app.get("/services")
def services(svc: Annotated[object, Depends(get_user_service)]):
    return svc
