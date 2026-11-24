import requests
from yaml import safe_load


def fetch(url: str):
    return requests.get(url)


def parse(text: str):
    return safe_load(text)
