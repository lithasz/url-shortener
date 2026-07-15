import random
import string
import time
import uuid
from collections import defaultdict  # Added for the rate limiter buckets

from fastapi import FastAPI, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
import structlog

from app.database import engine, get_db, Base
from app.models import URL

# Tracing setup
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

# Database tables
Base.metadata.create_all(bind=engine)

# Create the FastAPI app
app = FastAPI(title="URL Shortener")

# Configure structured logging 
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
)
logger = structlog.get_logger()

# Define metrics 
REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status_code"],
)
REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["method", "path"],
)

# Tracing setup
trace.set_tracer_provider(TracerProvider())
tracer = trace.get_tracer(__name__)

span_processor = BatchSpanProcessor(
    OTLPSpanExporter(endpoint="localhost:4317", insecure=True)
)
trace.get_tracer_provider().add_span_processor(span_processor)

FastAPIInstrumentor.instrument_app(app)

# ==========================================
# RATE LIMITING SETUP 
# ==========================================
# In-memory storage: tracks tokens per client IP
buckets = defaultdict(lambda: {"tokens": 5, "last_refill": time.time()})
MAX_TOKENS = 5          # Max burst size (5 requests instantly)
REFILL_RATE = 1         # Refills 1 token per second

def check_rate_limit(client_ip: str):
    bucket = buckets[client_ip]
    now = time.time()
    elapsed = now - bucket["last_refill"]
    
    # Refill tokens based on time passed, capped at MAX_TOKENS
    bucket["tokens"] = min(MAX_TOKENS, bucket["tokens"] + elapsed * REFILL_RATE)
    bucket["last_refill"] = now
    
    if bucket["tokens"] < 1:
        # Reject if no tokens left
        raise HTTPException(status_code=429, detail="Too many requests, slow down.")
    
    # Spend one token for this request
    bucket["tokens"] -= 1
# ==========================================

# Middleware for logging and metrics
@app.middleware("http")
async def log_requests(request: Request, call_next):
    request_id = str(uuid.uuid4())
    start_time = time.time()

    response = await call_next(request)
    duration_ms = round((time.time() - start_time) * 1000, 2)

    REQUEST_COUNT.labels(
        method=request.method, path=request.url.path, status_code=response.status_code
    ).inc()
    REQUEST_LATENCY.labels(
        method=request.method, path=request.url.path
    ).observe(duration_ms / 1000)

    logger.info(
        "request_completed",
        request_id=request_id,
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
        duration_ms=duration_ms,
    )

    return response

# Metrics endpoint
@app.get("/metrics")
def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

# Helper function
def generate_short_code(length: int = 6) -> str:
    characters = string.ascii_letters + string.digits
    return "".join(random.choice(characters) for _ in range(length))

# ==========================================
# ENDPOINTS (Updated with Rate Limiting)
# ==========================================

@app.post("/shorten")
def shorten_url(request: Request, long_url: str, db: Session = Depends(get_db)):
    # Check rate limit BEFORE doing any database work
    check_rate_limit(request.client.host) 
    
    with tracer.start_as_current_span("generate_unique_code"):
        code = generate_short_code()
        while db.query(URL).filter(URL.short_code == code).first():
            code = generate_short_code()

    new_url = URL(long_url=long_url, short_code=code)
    db.add(new_url)
    db.commit()
    db.refresh(new_url)

    return {"short_code": code, "short_url": f"http://localhost:8000/{code}"}

@app.get("/{code}")
def redirect_to_long_url(request: Request, code: str, db: Session = Depends(get_db)):
    # Protect redirects too so people can't spam your redirect endpoint
    check_rate_limit(request.client.host)
    
    url_entry = db.query(URL).filter(URL.short_code == code).first()

    if url_entry is None:
        raise HTTPException(status_code=404, detail="Short URL not found")

    url_entry.click_count += 1
    db.commit()

    return RedirectResponse(url=url_entry.long_url)

@app.get("/stats/{code}")
def get_stats(request: Request, code: str, db: Session = Depends(get_db)):
    # Protect stats endpoint as well
    check_rate_limit(request.client.host)
    
    url_entry = db.query(URL).filter(URL.short_code == code).first()
    if url_entry is None:
        raise HTTPException(status_code=404, detail="Short URL not found")
    return {
        "long_url": url_entry.long_url,
        "click_count": url_entry.click_count,
        "created_at": url_entry.created_at,
    }