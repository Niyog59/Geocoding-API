"""
Nominatim Geocoding API
A Mapbox-compatible geocoding API using Nominatim.

Features:
- Forward geocoding (address → coordinates)
- Reverse geocoding (coordinates → address)
- Autocomplete/suggestions endpoint
- API Key authentication
- Request ID tracking
- Mapbox-compatible response format
- Response caching with TTL
- Rate limiting per IP/API key
- Comprehensive logging
"""

import os
import uuid
import secrets
import hashlib
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from functools import wraps

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

# =============================================================================
# Configuration
# =============================================================================

NOMINATIM_BASE_URL = os.getenv("NOMINATIM_BASE_URL", "https://nominatim.openstreetmap.org")
CACHE_TTL = int(os.getenv("CACHE_TTL", "3600"))  # 1 hour default
RATE_LIMIT = os.getenv("RATE_LIMIT", "30/minute")
RATE_LIMIT_AUTHENTICATED = os.getenv("RATE_LIMIT_AUTHENTICATED", "100/minute")
CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "rentpasainc@gmail.com")
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")
REQUIRE_API_KEY = os.getenv("REQUIRE_API_KEY", "false").lower() == "true"

# Master API key (set this in production!)
MASTER_API_KEY = os.getenv("MASTER_API_KEY", "")

# User-Agent header (Nominatim requires a valid User-Agent with contact info)
USER_AGENT = f"GeocodingAPI/1.0 ({CONTACT_EMAIL})"

# =============================================================================
# Logging Configuration
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - [%(request_id)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Custom log filter to add request_id
class RequestIdFilter(logging.Filter):
    def filter(self, record):
        if not hasattr(record, 'request_id'):
            record.request_id = 'no-request'
        return True

logger = logging.getLogger(__name__)
logger.addFilter(RequestIdFilter())

# =============================================================================
# API Key Management (In-Memory Store)
# =============================================================================

# In production, use a database (PostgreSQL, Redis, etc.)
_api_keys: Dict[str, Dict[str, Any]] = {}
_api_usage: Dict[str, Dict[str, Any]] = {}

def _generate_api_key() -> str:
    """Generate a secure API key."""
    return f"geo_{secrets.token_urlsafe(32)}"

def _hash_api_key(api_key: str) -> str:
    """Hash API key for secure storage."""
    return hashlib.sha256(api_key.encode()).hexdigest()

def _init_default_api_keys():
    """Initialize default API keys for testing."""
    # Create a demo API key for testing
    demo_key = "geo_demo_key_for_testing_only"
    _api_keys[_hash_api_key(demo_key)] = {
        "name": "Demo Key",
        "created_at": datetime.now().isoformat(),
        "rate_limit": "60/minute",
        "active": True
    }
    
    # If master key is set, add it
    if MASTER_API_KEY:
        _api_keys[_hash_api_key(MASTER_API_KEY)] = {
            "name": "Master Key",
            "created_at": datetime.now().isoformat(),
            "rate_limit": "1000/minute",
            "active": True
        }
    
    logger.info(f"Initialized {len(_api_keys)} API keys")
    logger.info(f"Demo API key for testing: {demo_key}")

def _validate_api_key(api_key: str) -> Optional[Dict[str, Any]]:
    """Validate an API key and return its metadata."""
    if not api_key:
        return None
    
    key_hash = _hash_api_key(api_key)
    key_data = _api_keys.get(key_hash)
    
    if key_data and key_data.get("active", False):
        return key_data
    return None

def _track_usage(api_key: str, endpoint: str):
    """Track API usage per key."""
    if not api_key:
        return
    
    key_hash = _hash_api_key(api_key)
    today = datetime.now().strftime("%Y-%m-%d")
    
    if key_hash not in _api_usage:
        _api_usage[key_hash] = {}
    
    if today not in _api_usage[key_hash]:
        _api_usage[key_hash][today] = {"total": 0, "endpoints": {}}
    
    _api_usage[key_hash][today]["total"] += 1
    _api_usage[key_hash][today]["endpoints"][endpoint] = \
        _api_usage[key_hash][today]["endpoints"].get(endpoint, 0) + 1

def _get_usage(api_key: str) -> Dict[str, Any]:
    """Get usage statistics for an API key."""
    key_hash = _hash_api_key(api_key)
    today = datetime.now().strftime("%Y-%m-%d")
    
    usage = _api_usage.get(key_hash, {})
    today_usage = usage.get(today, {"total": 0, "endpoints": {}})
    
    return {
        "today": today_usage,
        "total_days": len(usage),
        "all_time_requests": sum(day.get("total", 0) for day in usage.values())
    }

# =============================================================================
# Rate Limiter with API Key support
# =============================================================================

def _get_rate_limit_key(request: Request) -> str:
    """Get rate limit key - use API key if provided, otherwise IP."""
    api_key = request.headers.get("X-API-Key") or request.query_params.get("api_key")
    if api_key and _validate_api_key(api_key):
        return f"apikey:{_hash_api_key(api_key)[:16]}"
    return get_remote_address(request)

limiter = Limiter(key_func=_get_rate_limit_key)

# =============================================================================
# FastAPI Application
# =============================================================================

app = FastAPI(
    title="Nominatim Geocoding API",
    description="""
A production-ready, Mapbox-compatible geocoding API.

## Features
- **Forward Geocoding**: Convert addresses to coordinates
- **Reverse Geocoding**: Convert coordinates to addresses  
- **Autocomplete**: Real-time search suggestions
- **API Key Authentication**: Secure access control
- **Request Tracking**: Every request has a unique ID

## Authentication
Include your API key in requests:
- Header: `X-API-Key: your_api_key`
- Query param: `?api_key=your_api_key`

Demo key for testing: `geo_demo_key_for_testing_only`
    """,
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

# Add rate limiter
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID", "X-RateLimit-Limit", "X-RateLimit-Remaining"],
)

# =============================================================================
# Middleware for Request ID
# =============================================================================

@app.middleware("http")
async def add_request_id(request: Request, call_next):
    """Add unique request ID to every request/response."""
    request_id = request.headers.get("X-Request-ID") or f"req_{uuid.uuid4().hex[:12]}"
    
    # Store in request state for use in endpoints
    request.state.request_id = request_id
    
    # Add to logging context
    logger_adapter = logging.LoggerAdapter(logger, {"request_id": request_id})
    
    # Process request
    start_time = time.time()
    response = await call_next(request)
    duration = time.time() - start_time
    
    # Add headers to response
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Response-Time"] = f"{duration:.3f}s"
    
    return response

# =============================================================================
# Dependencies
# =============================================================================

async def get_api_key(
    request: Request,
    x_api_key: Optional[str] = Header(None),
    api_key: Optional[str] = Query(None)
) -> Optional[str]:
    """Extract and validate API key from request."""
    key = x_api_key or api_key
    
    if REQUIRE_API_KEY and not key:
        raise HTTPException(
            status_code=401,
            detail="API key required. Include X-API-Key header or api_key query parameter."
        )
    
    if key:
        key_data = _validate_api_key(key)
        if not key_data:
            raise HTTPException(status_code=401, detail="Invalid API key")
        request.state.api_key_data = key_data
    
    return key

def get_request_id(request: Request) -> str:
    """Get request ID from request state."""
    return getattr(request.state, 'request_id', 'unknown')

# =============================================================================
# In-Memory Cache
# =============================================================================

_cache: dict = {}
_cache_max_size = 10000


def _get_cache_key(prefix: str, params: dict) -> str:
    """Generate a deterministic cache key from parameters."""
    key_str = json.dumps(params, sort_keys=True)
    return f"{prefix}:{hashlib.sha256(key_str.encode()).hexdigest()[:16]}"


def _get_cached_response(cache_key: str):
    """Get cached response if it exists and is not expired."""
    if cache_key in _cache:
        cached_data, timestamp = _cache[cache_key]
        if time.time() - timestamp < CACHE_TTL:
            return cached_data
        else:
            del _cache[cache_key]
    return None


def _set_cached_response(cache_key: str, data):
    """Store response in cache with size limit enforcement."""
    if len(_cache) >= _cache_max_size:
        oldest_keys = sorted(_cache.keys(), key=lambda k: _cache[k][1])[:100]
        for key in oldest_keys:
            del _cache[key]
    _cache[cache_key] = (data, time.time())


# =============================================================================
# Input Validation
# =============================================================================

def _validate_query(q: str) -> str:
    """Validate and sanitize the query string."""
    if not q or not q.strip():
        raise HTTPException(status_code=400, detail="Query parameter 'q' cannot be empty")
    if len(q) > 500:
        raise HTTPException(status_code=400, detail="Query too long (max 500 characters)")
    return q.strip()


def _validate_coordinates(lat: float, lon: float) -> tuple:
    """Validate latitude and longitude values."""
    if not (-90 <= lat <= 90):
        raise HTTPException(status_code=400, detail="Latitude must be between -90 and 90")
    if not (-180 <= lon <= 180):
        raise HTTPException(status_code=400, detail="Longitude must be between -180 and 180")
    return lat, lon


# =============================================================================
# Nominatim to Mapbox Format Conversion
# =============================================================================

def _nominatim_to_mapbox_feature(nominatim_item: dict, is_reverse: bool = False) -> dict:
    """Convert Nominatim response to Mapbox GeoJSON Feature format."""
    try:
        lon = float(nominatim_item["lon"])
        lat = float(nominatim_item["lat"])
    except (KeyError, ValueError, TypeError) as e:
        raise HTTPException(status_code=502, detail="Invalid response from geocoding service")
    
    address = nominatim_item.get("address", {})
    display_name = nominatim_item.get("display_name", "")
    place_id = nominatim_item.get("place_id", hash(display_name))
    
    # Determine place type
    place_type = ["place"]
    if "house_number" in address or "road" in address:
        place_type = ["address"]
    elif "city" in address or "town" in address or "village" in address:
        place_type = ["place"]
    elif "state" in address or "county" in address:
        place_type = ["region"]
    elif "country" in address:
        place_type = ["country"]
    
    # Build short text name
    text = (
        address.get("name") or 
        address.get("road") or 
        address.get("city") or 
        address.get("town") or 
        address.get("village") or
        display_name.split(",")[0] if display_name else "Unknown"
    )
    
    # Build context array
    context = []
    context_mappings = [
        ("postcode", "postcode"),
        ("city", "place"),
        ("town", "place"),
        ("village", "place"),
        ("county", "district"),
        ("state", "region"),
        ("country", "country"),
    ]
    
    for address_key, context_type in context_mappings:
        if address_key in address:
            context.append({
                "id": f"{context_type}.{place_id}",
                "text": address[address_key]
            })
    
    return {
        "id": f"place.{place_id}",
        "type": "Feature",
        "place_type": place_type,
        "relevance": 1.0,
        "properties": {"accuracy": "point"},
        "text": text,
        "place_name": display_name,
        "center": [lon, lat],
        "geometry": {
            "type": "Point",
            "coordinates": [lon, lat]
        },
        "context": context
    }


def _nominatim_to_autocomplete(nominatim_item: dict) -> dict:
    """Convert Nominatim response to autocomplete suggestion format."""
    try:
        lon = float(nominatim_item["lon"])
        lat = float(nominatim_item["lat"])
    except (KeyError, ValueError, TypeError):
        return None
    
    display_name = nominatim_item.get("display_name", "")
    place_id = nominatim_item.get("place_id", hash(display_name))
    address = nominatim_item.get("address", {})
    
    # Build short text
    text = (
        address.get("name") or 
        address.get("road") or 
        address.get("city") or 
        display_name.split(",")[0] if display_name else "Unknown"
    )
    
    return {
        "id": f"place.{place_id}",
        "text": text,
        "place_name": display_name,
        "center": [lon, lat]
    }


# =============================================================================
# HTTP Client Helper
# =============================================================================

async def _make_nominatim_request(endpoint: str, params: dict, request_id: str = "") -> dict:
    """Make a request to Nominatim API with proper error handling."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json"
    }
    
    url = f"{NOMINATIM_BASE_URL}/{endpoint}"
    
    try:
        async with httpx.AsyncClient(headers=headers, timeout=15.0) as client:
            response = await client.get(url, params=params)
            
            if response.status_code == 403:
                logger.error(f"[{request_id}] Nominatim 403 Forbidden")
                raise HTTPException(status_code=503, detail="Geocoding service temporarily unavailable")
            elif response.status_code == 429:
                raise HTTPException(status_code=429, detail="Geocoding service rate limit exceeded")
            
            response.raise_for_status()
            return response.json()
            
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Geocoding service timeout")
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail="Geocoding service error")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail="Failed to connect to geocoding service")


# =============================================================================
# API Endpoints
# =============================================================================

@app.get("/", tags=["Health"])
async def health_check():
    """Health check endpoint."""
    return {
        "status": "ok",
        "service": "Nominatim Geocoding API",
        "version": "2.0.0",
        "cache_entries": len(_cache),
        "api_keys_loaded": len(_api_keys)
    }


@app.get("/geocode", tags=["Geocoding"])
@limiter.limit(RATE_LIMIT)
async def geocode(
    request: Request,
    q: str = Query(..., min_length=1, max_length=500, description="Address or place name to geocode"),
    limit: int = Query(5, ge=1, le=50, description="Maximum number of results"),
    countrycodes: Optional[str] = Query(None, description="Limit to countries (ISO 3166-1 alpha-2 codes, comma-separated)"),
    viewbox: Optional[str] = Query(None, description="Preferred area (format: min_lon,min_lat,max_lon,max_lat)"),
    api_key: Optional[str] = Depends(get_api_key),
):
    """
    Forward geocoding: Convert an address or place name to coordinates.
    
    Returns a Mapbox-compatible GeoJSON FeatureCollection.
    """
    request_id = get_request_id(request)
    start_time = time.time()
    
    q = _validate_query(q)
    
    # Track usage
    _track_usage(api_key, "geocode")
    
    logger.info(f"[{request_id}] Geocode: q='{q[:50]}', limit={limit}")
    
    # Check cache
    cache_params = {"q": q.lower(), "limit": limit, "countrycodes": countrycodes}
    cache_key = _get_cache_key("geocode", cache_params)
    
    cached = _get_cached_response(cache_key)
    if cached is not None:
        logger.info(f"[{request_id}] Geocode completed: {time.time() - start_time:.3f}s (cached)")
        return cached
    
    # Build Nominatim request
    params = {
        "q": q,
        "format": "json",
        "limit": limit,
        "addressdetails": 1,
    }
    if countrycodes:
        params["countrycodes"] = countrycodes.lower()
    if viewbox:
        params["viewbox"] = viewbox
        params["bounded"] = 1
    
    data = await _make_nominatim_request("search", params, request_id)
    
    features = [_nominatim_to_mapbox_feature(item) for item in data] if data else []
    
    result = {
        "type": "FeatureCollection",
        "query": q.split(),
        "features": features,
        "attribution": "© OpenStreetMap contributors"
    }
    
    _set_cached_response(cache_key, result)
    
    logger.info(f"[{request_id}] Geocode completed: {time.time() - start_time:.3f}s, results={len(features)}")
    return result


@app.get("/reverse", tags=["Geocoding"])
@limiter.limit(RATE_LIMIT)
async def reverse_geocode(
    request: Request,
    lat: float = Query(..., ge=-90, le=90, description="Latitude"),
    lon: float = Query(..., ge=-180, le=180, description="Longitude"),
    zoom: int = Query(18, ge=0, le=18, description="Zoom level (0-18, higher = more detailed)"),
    api_key: Optional[str] = Depends(get_api_key),
):
    """
    Reverse geocoding: Convert coordinates to an address.
    
    Returns a Mapbox-compatible GeoJSON FeatureCollection.
    """
    request_id = get_request_id(request)
    start_time = time.time()
    
    lat, lon = _validate_coordinates(lat, lon)
    
    # Track usage
    _track_usage(api_key, "reverse")
    
    logger.info(f"[{request_id}] Reverse: lat={lat}, lon={lon}")
    
    # Check cache
    cache_params = {"lat": round(lat, 5), "lon": round(lon, 5), "zoom": zoom}
    cache_key = _get_cache_key("reverse", cache_params)
    
    cached = _get_cached_response(cache_key)
    if cached is not None:
        logger.info(f"[{request_id}] Reverse completed: {time.time() - start_time:.3f}s (cached)")
        return cached
    
    params = {
        "lat": lat,
        "lon": lon,
        "format": "json",
        "zoom": zoom,
        "addressdetails": 1,
    }
    
    data = await _make_nominatim_request("reverse", params, request_id)
    
    if isinstance(data, dict) and "error" in data:
        raise HTTPException(status_code=404, detail=data["error"])
    
    feature = _nominatim_to_mapbox_feature(data, is_reverse=True)
    
    result = {
        "type": "FeatureCollection",
        "query": [str(lon), str(lat)],
        "features": [feature],
        "attribution": "© OpenStreetMap contributors"
    }
    
    _set_cached_response(cache_key, result)
    
    logger.info(f"[{request_id}] Reverse completed: {time.time() - start_time:.3f}s")
    return result


@app.get("/autocomplete", tags=["Geocoding"])
@limiter.limit(RATE_LIMIT_AUTHENTICATED)
async def autocomplete(
    request: Request,
    q: str = Query(..., min_length=2, max_length=200, description="Search query (min 2 characters)"),
    limit: int = Query(5, ge=1, le=10, description="Maximum suggestions"),
    countrycodes: Optional[str] = Query(None, description="Limit to countries"),
    api_key: Optional[str] = Depends(get_api_key),
):
    """
    Autocomplete/suggestions: Get real-time search suggestions as user types.
    
    Optimized for speed - returns lightweight suggestions.
    
    **Example:** `/autocomplete?q=8181 Fan`
    """
    request_id = get_request_id(request)
    start_time = time.time()
    
    q = _validate_query(q)
    
    # Track usage
    _track_usage(api_key, "autocomplete")
    
    logger.info(f"[{request_id}] Autocomplete: q='{q}'")
    
    # Check cache (shorter TTL for autocomplete)
    cache_params = {"q": q.lower(), "limit": limit, "cc": countrycodes}
    cache_key = _get_cache_key("autocomplete", cache_params)
    
    cached = _get_cached_response(cache_key)
    if cached is not None:
        return cached
    
    # Nominatim search params optimized for autocomplete
    params = {
        "q": q,
        "format": "json",
        "limit": limit,
        "addressdetails": 1,
        "dedupe": 1,  # Remove duplicates
    }
    if countrycodes:
        params["countrycodes"] = countrycodes.lower()
    
    data = await _make_nominatim_request("search", params, request_id)
    
    # Convert to lightweight autocomplete format
    suggestions = []
    for item in (data or []):
        suggestion = _nominatim_to_autocomplete(item)
        if suggestion:
            suggestions.append(suggestion)
    
    result = {
        "query": q,
        "suggestions": suggestions,
        "attribution": "© OpenStreetMap contributors"
    }
    
    _set_cached_response(cache_key, result)
    
    logger.info(f"[{request_id}] Autocomplete completed: {time.time() - start_time:.3f}s, results={len(suggestions)}")
    return result


@app.get("/usage", tags=["Account"])
async def get_usage_stats(
    request: Request,
    api_key: str = Depends(get_api_key),
):
    """
    Get usage statistics for your API key.
    
    Requires authentication.
    """
    if not api_key:
        raise HTTPException(status_code=401, detail="API key required")
    
    usage = _get_usage(api_key)
    key_data = _validate_api_key(api_key)
    
    return {
        "api_key": f"{api_key[:10]}...{api_key[-4:]}",
        "name": key_data.get("name", "Unknown") if key_data else "Unknown",
        "rate_limit": key_data.get("rate_limit", RATE_LIMIT) if key_data else RATE_LIMIT,
        "usage": usage
    }


@app.post("/keys/generate", tags=["Admin"])
async def generate_api_key(
    request: Request,
    name: str = Query(..., min_length=1, max_length=100, description="Name for this API key"),
    master_key: str = Query(..., description="Master API key for authorization"),
):
    """
    Generate a new API key (requires master key).
    
    **Note:** Store the returned key securely - it cannot be retrieved later.
    """
    # Verify master key
    if not MASTER_API_KEY or master_key != MASTER_API_KEY:
        raise HTTPException(status_code=403, detail="Invalid master key")
    
    # Generate new key
    new_key = _generate_api_key()
    key_hash = _hash_api_key(new_key)
    
    _api_keys[key_hash] = {
        "name": name,
        "created_at": datetime.now().isoformat(),
        "rate_limit": RATE_LIMIT_AUTHENTICATED,
        "active": True
    }
    
    logger.info(f"New API key generated: {name}")
    
    return {
        "api_key": new_key,
        "name": name,
        "rate_limit": RATE_LIMIT_AUTHENTICATED,
        "message": "Store this key securely - it cannot be retrieved later!"
    }


# =============================================================================
# Startup Event
# =============================================================================

@app.on_event("startup")
async def startup_event():
    """Initialize API keys on startup."""
    _init_default_api_keys()
    logger.info("Geocoding API started successfully")


# =============================================================================
# Application Entry Point
# =============================================================================

if __name__ == "__main__":
    import uvicorn
    
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    
    logger.info(f"Starting Nominatim Geocoding API on {host}:{port}")
    logger.info(f"User-Agent: {USER_AGENT}")
    logger.info(f"API Key Required: {REQUIRE_API_KEY}")
    
    uvicorn.run(app, host=host, port=port, log_level="info")
