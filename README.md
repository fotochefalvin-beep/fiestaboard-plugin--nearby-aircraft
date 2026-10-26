# Nearby Aircraft Plugin

Display real-time nearby aircraft information from the OpenSky Network API.

![Nearby Aircraft Display](./docs/board-display.png)

**→ [Setup Guide](./docs/SETUP.md)** - Configuration and optional OAuth setup

## Overview

The Nearby Aircraft plugin fetches live aircraft state vectors from the OpenSky Network API and displays aircraft within a user-defined radius. It shows call sign, altitude (feet), ground speed (knots), and squawk code for each aircraft. The plugin includes column alignment formatting to ensure consistent display across all aircraft rows.

## Architecture

### Plugin Class

`NearbyAircraftPlugin` extends `PluginBase` and implements:

- **OAuth2 Authentication**: Optional client credentials flow for higher rate limits
- **State Vector Parsing**: Parses OpenSky API state vector arrays
- **Distance Filtering**: Bounding box calculation + Haversine distance filtering
- **Unit Conversions**: Meters to feet, m/s to knots
- **Caching**: Implements cache with TTL based on refresh interval
- **Error Handling**: Graceful handling of rate limits, missing data, API failures

### Data Flow

```
1. User Configuration
   ├─ Latitude/Longitude (center point)
   ├─ Radius (km)
   └─ Optional: OAuth2 credentials

2. API Request
   ├─ Calculate bounding box from center + radius
   ├─ Request OAuth token (if credentials provided)
   └─ Fetch state vectors from OpenSky API

3. Data Processing
   ├─ Parse state vectors (array format)
   ├─ Filter by actual distance (Haversine)
   ├─ Convert units (m→ft, m/s→knots)
   └─ Format display strings

4. Output
   ├─ Primary aircraft (closest)
   ├─ Array of all aircraft
   └─ Formatted display lines
```

## API Integration

### OpenSky Network API

**Base URL**: `https://opensky-network.org/api`

**Endpoints:**
- OAuth Token: `POST /v1/oauth/token`
- States: `GET /states/all?lamin=X&lamax=Y&lomin=Z&lomax=W`

**Authentication:**
- Optional OAuth2 client credentials flow
- Unauthenticated requests supported (lower rate limits)
- Token caching with expiration handling

**Rate Limits:**
- Unauthenticated: 400 API credits/day (1 req/10 sec)
- Authenticated: 4,000 API credits/day (~10 req/sec)
- Active Contributors: 8,000 API credits/day

### State Vector Structure

OpenSky API returns state vectors as arrays with fixed indices:

```python
STATE_VECTOR_INDICES = {
    "icao24": 0,           # ICAO 24-bit address (hex)
    "callsign": 1,         # Flight call sign (may be null)
    "origin_country": 2,   # Country code
    "time_position": 3,    # Unix timestamp
    "last_contact": 4,     # Unix timestamp
    "longitude": 5,        # Degrees
    "latitude": 6,         # Degrees
    "baro_altitude": 7,    # Meters (barometric)
    "on_ground": 8,        # Boolean
    "velocity": 9,         # m/s (ground speed)
    "true_track": 10,      # Degrees
    "vertical_rate": 11,   # m/s
    "sensors": 12,         # Sensor IDs
    "geo_altitude": 13,    # Meters (GPS/geometric)
    "squawk": 14,          # Transponder code (may be null)
    "spi": 15,             # Special purpose indicator
    "position_source": 16, # Position source
}
```

## Implementation Details

### Bounding Box Calculation

To efficiently query the API, we calculate a bounding box from the center point and radius:

```python
def calculate_bounding_box(lat: float, lon: float, radius_km: float):
    # Approximate: 1 degree latitude ≈ 111 km
    lat_delta = radius_km / 111.0
    # Longitude adjusted by latitude (cos(lat))
    lon_delta = radius_km / (111.0 * abs(cos(radians(lat))))
    
    return {
        "lamin": lat - lat_delta,
        "lamax": lat + lat_delta,
        "lomin": lon - lon_delta,
        "lomax": lon + lon_delta,
    }
```

The API returns all aircraft in the bounding box, then we filter by actual distance using Haversine formula.

### Haversine Distance

Calculates great-circle distance between two points on Earth:

```python
def haversine_distance(lat1, lon1, lat2, lon2):
    # Convert to radians
    lon1, lat1, lon2, lat2 = map(radians, [lon1, lat1, lon2, lat2])
    
    # Haversine formula
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    c = 2 * asin(sqrt(a))
    
    # Earth radius in kilometers
    return 6371 * c
```

### Unit Conversions

**Altitude (meters → feet):**
```python
altitude_ft = int(altitude_m * 3.28084)
```

**Ground Speed (m/s → knots):**
```python
ground_speed_knots = int(velocity_ms * 1.94384)
```

### Altitude Selection

The plugin prefers `geo_altitude` (GPS-based) over `baro_altitude` (barometric):

```python
if geo_altitude is not None:
    altitude_m = float(geo_altitude)
elif baro_altitude is not None:
    altitude_m = float(baro_altitude)
```

### Call Sign Handling

If call sign is null, use ICAO 24-bit address as fallback:

```python
if callsign_raw:
    callsign = str(callsign_raw).strip()
else:
    callsign = str(icao24) if icao24 else "UNKNOWN"
```

### Squawk Code Handling

Squawk codes are handled with proper validation:

```python
# Squawk can be None, empty string, 0, or a valid 4-digit code
if squawk_raw is not None and squawk_raw != "":
    squawk_int = int(float(squawk_raw))
    if squawk_int == 0:
        squawk = "----"  # 0 means no squawk assigned
    else:
        squawk = str(squawk_int).zfill(4)  # Format as 4-digit code
else:
    squawk = "----"  # No squawk available
```

- Valid squawk codes: Displayed as 4-digit codes (e.g., "1200", "2513")
- No squawk (None, empty, or 0): Displayed as "----"
- Short codes: Padded with leading zeros (e.g., "12" → "0012")

### Display Formatting

The plugin uses column alignment to ensure consistent formatting across all aircraft. The `headers` variable and all `formatted` fields use the same column widths for perfect alignment.

**Example Display:**
```
NEARBY AIRCRAFT
CALLSGN  ALT   GS  SQWK
ITY630   40000 393 2513
AAY453   4575  279 1603
DAL2267  4750  276 ----
```

**Alignment Formatting:**
The `_align_formatting()` method calculates maximum widths for each field across all aircraft, then applies consistent padding to ensure columns align properly:

```python
def _align_formatting(aircraft_list):
    # Calculates max widths for each field across all aircraft
    # Applies consistent padding to ensure columns align
    # Returns (aligned_aircraft_list, headers_string)
```

**Column Alignment:**
- **CALLSGN**: Left-aligned, max 8 characters (truncated if longer)
- **ALT**: Right-aligned, width matches maximum altitude width across all aircraft
- **GS**: Right-aligned, width matches maximum ground speed width across all aircraft
- **SQWK**: Right-aligned, 4 characters (shows "----" if no squawk)

The `headers` variable uses the same column widths as the aircraft data, ensuring perfect alignment when used in templates. This matches the pattern used by the Stocks plugin for consistent column formatting.

## Error Handling

### Rate Limiting

- Detects 429 status code
- Returns cached data if available
- Logs warning for rate limit hits
- Suggests authentication for higher limits

### Missing Data

- Skips aircraft without required fields (altitude, velocity, position)
- Uses fallbacks for optional fields (callsign → ICAO address)
- Handles null values gracefully

### API Failures

- Catches network exceptions
- Returns cached data when available
- Logs errors for debugging
- Returns user-friendly error messages

### Caching

- Caches successful API responses
- TTL based on `refresh_seconds` setting
- Returns cached data on errors
- Invalidates cache on config changes

## Configuration

### Required Settings

- `latitude`: Center latitude (-90 to 90)
- `longitude`: Center longitude (-180 to 180)

### Optional Settings

- `radius_km`: Search radius (default: 50)
- `client_id`: OAuth2 client ID
- `client_secret`: OAuth2 client secret
- `airlabs_api_key`: AirLabs API key, backup source for flight number and aircraft type (see [Flight number and aircraft type](#flight-number-and-aircraft-type))
- `max_aircraft`: Maximum to display (default: 4, max: 24; the board actually rendered on may show fewer)
- `refresh_seconds`: Update interval (default: 120, min: 10)
- `trigger_call_signs`, `trigger_aircraft_types`, `trigger_max_altitude_ft`, `trigger_max_distance_km`: watch rules (all empty by default, see [Trigger a page](#trigger-a-page))
- `trigger_duration_seconds`: How long a trigger stays on the board (default: 60, min: 10)
- `trigger_cooldown_minutes`: Minimum time before the same aircraft fires again (default: 30, min: 0)

### Flight number and aircraft type

adsbdb is the first source for both:

- `flight_number` is derived from the call sign and the airline adsbdb returns
  (`BAW123` -> `BA123`). It is empty for alphanumeric ATC call signs
  (`BEL5EN`, `VLG8UE`), and may differ from the published number when the call
  sign digits do.
- `aircraft_type` comes from adsbdb's aircraft record (`B738` -> `737-800`).

With `airlabs_api_key` set, AirLabs is the backup. Only when adsbdb gives no
flight number or no aircraft type (unknown, error or timeout) does the plugin
ask AirLabs live flights (`/api/v9/flights?hex=<icao24>`) once for that
aircraft. Its `flight_iata` and `aircraft_icao` fill only the missing
field(s); a value adsbdb gave is never replaced. Notes:

- Looked up only for the aircraft shown on the board (and for triggers, inside
  the same 10-lookups-per-fetch budget). Each result, including a miss, is
  remembered per icao24 + call sign, so one flight costs at most one request
  for both fields.
- AirLabs' type designator is shown with the same readable names as adsbdb's
  (`A20N` -> `A320neo`); unknown designators are shown as-is.
- On a miss the missing field(s) stay empty.
- After an AirLabs error (bad key, quota used up, outage) the plugin skips
  AirLabs for 15 minutes. Changing the settings ends that pause.
- Without a key, no AirLabs request is made.
- Check your AirLabs plan returns `flight_iata` and `aircraft_icao`: the docs
  mark only some fields as available on the free plan.
- The trigger aircraft-type rule uses adsbdb only.

### Validation

- Latitude: -90 to 90
- Longitude: -180 to 180
- Radius: >= 1 km
- Max aircraft: 1 to 24
- Refresh: >= 10 seconds
- Trigger max altitude / max distance: empty, or 0 or more
- Trigger duration: >= 10 seconds; trigger cooldown: >= 0 minutes

## Template Variables

### Simple Variables

- `call_sign`: Primary aircraft call sign
- `altitude`: Primary aircraft altitude (feet)
- `ground_speed`: Primary aircraft speed (knots)
- `squawk`: Primary aircraft squawk code (4-digit code or "----" if unavailable)
- `formatted`: Pre-formatted display line for primary aircraft
- `headers`: Aligned column headers matching aircraft data format (e.g., "CALLSGN  ALT   GS  SQWK")
- `aircraft_count`: Number of aircraft found
- `last_updated`: ISO timestamp of last update

### Array Variables

- `aircraft`: Array of all aircraft (sorted by distance, closest first)
  - `icao24`: ICAO 24-bit address
  - `call_sign`: Call sign (or ICAO address if call sign unavailable)
  - `altitude`: Altitude in feet
  - `ground_speed`: Ground speed in knots
  - `squawk`: Squawk code (4-digit code or "----" if unavailable)
  - `latitude`: Latitude
  - `longitude`: Longitude
  - `distance_km`: Distance from center point in kilometers
  - `formatted`: Pre-formatted display line with aligned columns

## Usage Examples

### Basic Template

Use the `headers` variable for aligned column headers and `formatted` for each aircraft:

```
{{nearby_aircraft.headers}}
{{nearby_aircraft.aircraft.0.formatted}}
{{nearby_aircraft.aircraft.1.formatted}}
{{nearby_aircraft.aircraft.2.formatted}}
{{nearby_aircraft.aircraft.3.formatted}}
```

### Individual Fields

Access specific fields from aircraft:

```
{{nearby_aircraft.aircraft.0.call_sign}}
{{nearby_aircraft.aircraft.0.altitude}}ft
{{nearby_aircraft.aircraft.0.ground_speed}}kt
{{nearby_aircraft.aircraft.0.distance_km}}km away
```

### Primary Aircraft

Access the closest aircraft directly:

```
Closest: {{nearby_aircraft.call_sign}}
Altitude: {{nearby_aircraft.altitude}}ft
Speed: {{nearby_aircraft.ground_speed}}kt
```

## Trigger a page

The plugin can switch the board to a page of your choice when an interesting
aircraft passes. This is opt-in: with every rule empty, it never fires a trigger.

### Rules

| Setting | Matches when |
|---|---|
| `trigger_call_signs` | the call sign starts with one of the comma-separated prefixes, e.g. `KLM, BAW` |
| `trigger_aircraft_types` | the readable type starts with one of the values (`747` matches `747-8`), or equals an ICAO designator (`A388`); case-insensitive |
| `trigger_max_altitude_ft` | the aircraft is at or below this altitude (feet) |
| `trigger_max_distance_km` | the aircraft is within this distance (km) of your location |

- Rules combine with **AND**: every rule you fill in must hold. Several values
  within one rule match **any** of them. A single rule on its own is fine.
  Example: call signs `KLM` + max altitude `5000` fires for KLM flights below 5000 ft.
- Rules look at every airborne aircraft within `radius_km`, not only the
  `max_aircraft` closest ones shown on the board.
- The aircraft type is looked up (adsbdb) only for aircraft that pass every
  other rule; an aircraft whose type is unknown never matches a type rule.
  A type filled in by AirLabs (shown on the board) is not matched.
- At most 10 new lookups (adsbdb route and type, plus the AirLabs backup, all
  together) are started per fetch for triggers. Past that, a trigger still fires but shows only values
  already known (e.g. an empty `origin`/`aircraft_type`); aircraft whose type
  could not be checked yet are checked on a later fetch.

### What happens when an aircraft matches

- One trigger per aircraft (`trigger_id` `aircraft_<icao24>`, priority NOTABLE)
  is shown for `trigger_duration_seconds`.
- The same aircraft does not fire again until `trigger_cooldown_minutes` have
  passed, even if it stays in range.
- As soon as a fresh fetch finds the aircraft no longer matching (for example
  it moved beyond `trigger_max_distance_km`, climbed above
  `trigger_max_altitude_ft`, left the radius or landed), its trigger is
  dismissed early, so the page shows only while the aircraft matches.
  Changing the plugin settings also clears its active triggers.
- Cooldowns live in memory only; a restart may announce an aircraft once more.

### Choosing the trigger page

Pick any page with the **Trigger Page** setting (added automatically by
FiestaBoard). The page receives the plugin's normal variables as
`{{nearby_aircraft.<key>}}`: `formatted`, `headers`, `aircraft_count`,
`last_updated` and the `aircraft` array stay as on the normal display, while
these hold the **triggering** aircraft instead of the closest one:

`call_sign`, `flight_number`, `aircraft_type`, `origin`, `destination`,
`altitude`, `ground_speed`, `squawk`, plus `distance_km`.

So a page you already built for this plugin works unchanged as the trigger page.
For example:

```
{{nearby_aircraft.call_sign}} {{nearby_aircraft.aircraft_type}}
{{nearby_aircraft.origin}}>{{nearby_aircraft.destination}}
{{nearby_aircraft.altitude}}FT {{nearby_aircraft.distance_km}}KM
```

Without a trigger page, the board shows a plain message: call sign and type,
then `origin>destination` (when known), then altitude and distance.

### Refresh interval caveat

Triggers reuse the regular OpenSky fetch; no extra API calls are made. A new
aircraft is therefore only noticed on the next fresh fetch, up to
`refresh_seconds` later, and a quick pass between two fetches can be missed.
Lower `refresh_seconds` for faster reaction (mind the OpenSky rate limits).
Clearing the page early also waits for the next fresh fetch. When OpenSky
fails or rate-limits and cached data is served, nothing new fires and nothing
is dismissed.

## Testing

### Unit Tests

Located in `tests/test_plugin.py`:

- Plugin initialization
- Configuration validation
- Bounding box calculation
- Haversine distance calculation
- State vector parsing (including squawk code handling: null, 0, empty string, valid codes)
- Unit conversions
- Display formatting (including column alignment)
- Headers variable generation
- OAuth token handling
- Error handling
- Caching behavior
- Distance filtering and sorting

### Test Fixtures

- Mock OpenSky API responses
- Sample state vectors
- Test coordinates (San Francisco)
- Mock OAuth responses

### Coverage

Current: 95% code coverage (exceeds 80% target)

## Dependencies

- `requests`: HTTP client for API calls
- `math`: Standard library for distance calculations
- `datetime`: Standard library for timestamps and caching

## Performance Considerations

### API Rate Limits

- Default refresh: 120 seconds (safe for unauthenticated)
- With authentication: Can reduce to 60 seconds
- Caching reduces actual API calls

### Distance Filtering

- Bounding box reduces API response size
- Haversine filtering is O(n) where n = aircraft in bounding box
- Sorting by distance: O(n log n)

### Memory

- Caches single response (small footprint)
- Limits aircraft array to max_aircraft (default: 4)

## Future Enhancements

Potential improvements:

- Filter by altitude range
- Filter by aircraft type
- Historical tracking
- Flight path visualization
- Integration with flight databases (FlightAware, etc.)
- WebSocket support for real-time updates

## References

- [OpenSky Network API Documentation](https://openskynetwork.github.io/opensky-api/)
- [OpenSky Network Website](https://opensky-network.org/)
- [Haversine Formula](https://en.wikipedia.org/wiki/Haversine_formula)
- [Plugin Development Guide](../../docs/development/PLUGIN_DEVELOPMENT.md)
