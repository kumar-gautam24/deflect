from fastapi import FastAPI
from sqlalchemy import text

from deflect.db import SessionDep

app = FastAPI(title="Deflect")


@app.get("/health")
async def health(session: SessionDep) -> dict[str, str]:
    await session.execute(text("select 1"))
    return {"status": "ok", "database": "connected"}
