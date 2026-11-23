from typing import Annotated

from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")


def get_user_service():
    return object()


def get_current_user(token: Annotated[str, Depends(oauth2_scheme)]):
    return token


def require_admin(user: Annotated[str, Depends(get_current_user)]):
    return user
