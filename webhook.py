from fastapi import FastAPI, Request

app = FastAPI()

@app.get("/healthz")
async def healthz():
    return {"ok": True}

@app.post("/stub")
async def stub(request: Request):
    data = await request.json()
    return {"ok": True, "received": data}
