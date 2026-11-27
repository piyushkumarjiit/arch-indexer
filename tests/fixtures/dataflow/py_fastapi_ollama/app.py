import ollama
from fastapi import FastAPI

app = FastAPI()


def sanitize(text: str) -> str:
    return text.replace("<", "").replace(">", "")


def call_llm(prompt: str) -> str:
    return ollama.chat(model="qwen3:8b", messages=[{"role": "user", "content": prompt}])


@app.post("/ask")
def ask_question(question: str) -> dict[str, str]:
    answer = call_llm(question)
    return {"answer": answer}


@app.post("/safe")
def safe_question(question: str) -> dict[str, str]:
    cleaned = sanitize(question)
    answer = call_llm(cleaned)
    return {"answer": answer}
