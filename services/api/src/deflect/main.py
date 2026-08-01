from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from deflect.config import get_settings
from deflect.db import SessionDep
from deflect.routes import ask, evals, traces

app = FastAPI(title="Deflect")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in get_settings().web_origin.split(",")],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(ask.router)
app.include_router(evals.router)
app.include_router(traces.router)


@app.get("/health")
async def health(session: SessionDep) -> dict[str, str]:
    await session.execute(text("select 1"))
    return {"status": "ok", "database": "connected"}
