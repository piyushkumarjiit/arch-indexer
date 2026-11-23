from math import sqrt

from fastapi import Depends, FastAPI

app = FastAPI()


def get_token():
    return "secret"


@app.get("/items/{item_id}")
def read_item(item_id: int, token: str = Depends(get_token)):
    val = sqrt(item_id)
    return {"id": item_id, "val": val}


if __name__ == "__main__":
    read_item(16)
