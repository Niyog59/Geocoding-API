# Nominatim Geocoding API v2.0

A production-ready, Mapbox-compatible geocoding API with authentication, autocomplete, and request tracking.

## 🚀 New in v2.0

✅ **API Key Authentication** - Secure access control with usage tracking  
✅ **Autocomplete Endpoint** - Real-time search suggestions  
✅ **Request ID Tracking** - Every request has a unique ID for debugging  
✅ **Usage Analytics** - Track API usage per key  
✅ **Admin Endpoints** - Generate new API keys  

## Features

| Feature | Description |
|---------|-------------|
| Forward Geocoding | Address → Coordinates |
| Reverse Geocoding | Coordinates → Address |
| Autocomplete | Real-time search suggestions |
| API Key Auth | Secure access with rate limits |
| Request Tracking | Unique ID per request |
| Caching | 1-hour TTL response cache |
| Rate Limiting | Per IP or API key |

## Quick Start

### 1. Install Dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Run the Server

```bash
python main.py
```

### 3. Test the API

Open: http://localhost:8000/docs

**Demo API key for testing:** `geo_demo_key_for_testing_only`

## API Endpoints

### Geocoding

#### Forward Geocoding
```
GET /geocode?q={address}&api_key={key}
```

| Parameter | Required | Description |
|-----------|----------|-------------|
| `q` | Yes | Address or place name |
| `limit` | No | Max results (1-50, default: 5) |
| `countrycodes` | No | Limit to countries (e.g., "us,ca") |
| `api_key` | No* | Your API key |

**Example:**
```bash
curl "http://localhost:8000/geocode?q=New%20York&api_key=geo_demo_key_for_testing_only"
```

#### Reverse Geocoding
```
GET /reverse?lat={lat}&lon={lon}&api_key={key}
```

| Parameter | Required | Description |
|-----------|----------|-------------|
| `lat` | Yes | Latitude (-90 to 90) |
| `lon` | Yes | Longitude (-180 to 180) |
| `zoom` | No | Detail level (0-18, default: 18) |
| `api_key` | No* | Your API key |

**Example:**
```bash
curl "http://localhost:8000/reverse?lat=40.7128&lon=-74.0060&api_key=geo_demo_key_for_testing_only"
```

#### Autocomplete (NEW!)
```
GET /autocomplete?q={query}&api_key={key}
```

Real-time search suggestions as users type (min 2 characters).

| Parameter | Required | Description |
|-----------|----------|-------------|
| `q` | Yes | Search query (min 2 chars) |
| `limit` | No | Max suggestions (1-10, default: 5) |
| `api_key` | No* | Your API key |

**Example:**
```bash
curl "http://localhost:8000/autocomplete?q=8181%20Fan&api_key=geo_demo_key_for_testing_only"
```

**Response:**
```json
{
  "query": "8181 Fan",
  "suggestions": [
    {
      "id": "place.12345",
      "text": "Fannin Street",
      "place_name": "8181, Fannin Street, Houston, TX 77054, USA",
      "center": [-95.403, 29.686]
    }
  ]
}
```

### Account & Admin

#### Get Usage Stats
```
GET /usage?api_key={key}
```

Returns your API usage statistics.

**Example:**
```bash
curl "http://localhost:8000/usage?api_key=geo_demo_key_for_testing_only"
```

**Response:**
```json
{
  "api_key": "geo_demo_k...only",
  "name": "Demo Key",
  "rate_limit": "60/minute",
  "usage": {
    "today": {
      "total": 15,
      "endpoints": {
        "geocode": 10,
        "autocomplete": 5
      }
    },
    "all_time_requests": 150
  }
}
```

#### Generate API Key (Admin)
```
POST /keys/generate?name={name}&master_key={master}
```

Generate new API keys (requires master key).

## Authentication

### Methods

Include your API key using either method:

1. **Header (recommended):**
   ```
   X-API-Key: your_api_key_here
   ```

2. **Query parameter:**
   ```
   ?api_key=your_api_key_here
   ```

### API Keys

| Key Type | Rate Limit | Usage |
|----------|------------|-------|
| No key | 30/minute | Public access |
| Demo key | 60/minute | Testing only |
| Generated key | 100/minute | Production use |
| Master key | 1000/minute | Admin access |

**Demo key for testing:** `geo_demo_key_for_testing_only`

## Request Tracking

Every response includes:

| Header | Description |
|--------|-------------|
| `X-Request-ID` | Unique request identifier |
| `X-Response-Time` | Request duration |

Use `X-Request-ID` for debugging and support requests.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `NOMINATIM_BASE_URL` | `https://nominatim.openstreetmap.org` | Nominatim API URL |
| `CONTACT_EMAIL` | `rentpasainc@gmail.com` | Contact for User-Agent |
| `CACHE_TTL` | `3600` | Cache TTL in seconds |
| `RATE_LIMIT` | `30/minute` | Rate limit (unauthenticated) |
| `RATE_LIMIT_AUTHENTICATED` | `100/minute` | Rate limit (authenticated) |
| `REQUIRE_API_KEY` | `false` | Require API key for all requests |
| `MASTER_API_KEY` | `` | Master key for admin endpoints |

## Production Deployment

### Set Master Key
```bash
export MASTER_API_KEY="your_secure_master_key_here"
python main.py
```

### Require API Keys
```bash
export REQUIRE_API_KEY=true
python main.py
```

### Using Gunicorn
```bash
gunicorn main:app -w 4 -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8000
```

## Response Format

All geocoding responses follow Mapbox format:

```json
{
  "type": "FeatureCollection",
  "query": ["new", "york"],
  "features": [
    {
      "id": "place.12345",
      "type": "Feature",
      "place_type": ["place"],
      "text": "New York",
      "place_name": "New York, NY, United States",
      "center": [-74.006, 40.7128],
      "geometry": {
        "type": "Point",
        "coordinates": [-74.006, 40.7128]
      },
      "context": [...]
    }
  ],
  "attribution": "© OpenStreetMap contributors"
}
```

## Security Features

- ✅ API key authentication
- ✅ Rate limiting per key/IP
- ✅ Input validation
- ✅ Request ID tracking
- ✅ Secure key hashing (SHA-256)
- ✅ Usage monitoring

## License

MIT License

## Attribution

Data provided by [OpenStreetMap](https://www.openstreetmap.org/) contributors.
