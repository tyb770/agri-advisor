# app/main.py

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.v1 import farmers, webhooks, fields, auth, dashboard, detections, advisory

app = FastAPI(
    title="Agri Advisory API",
    description="AI-powered agricultural advisory for Punjab farmers",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:3000",
        "http://localhost:3001",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router,       prefix="/api/v1/auth",       tags=["Auth"])
app.include_router(farmers.router,    prefix="/api/v1/farmers",    tags=["Farmers"])
app.include_router(fields.router,     prefix="/api/v1/fields",     tags=["Fields"])
app.include_router(advisory.router,   prefix="/api/v1/advisory",   tags=["Advisory"])
app.include_router(detections.router, prefix="/api/v1/detections", tags=["Detections"])
app.include_router(webhooks.router,   prefix="/api/v1/webhooks",   tags=["Webhooks"])
app.include_router(dashboard.router,  prefix="/api/v1/dashboard",  tags=["Dashboard"])


@app.get("/health")
async def health_check():
    return {"status": "ok", "version": "0.1.0"}