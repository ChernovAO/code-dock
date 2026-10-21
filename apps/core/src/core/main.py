from fastapi import FastAPI

app = FastAPI(title="Code Dock")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
