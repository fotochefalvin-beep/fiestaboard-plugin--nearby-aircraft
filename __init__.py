"""Nearby Aircraft plugin for FiestaBoard.

Displays real-time nearby aircraft information using the OpenSky Network API.
Shows call sign, altitude, ground speed, and squawk code for aircraft within
a user-defined radius.
"""

from typing import Any, Dict, List, Optional, Set, Tuple
from datetime import datetime, timedelta, timezone
import copy
import logging
import requests
from math import radians, cos, sin, asin, sqrt

from src.devices import NOTE_COLS
from src.plugins.base import PluginBase, PluginResult, TriggerResult

try:
    from src.triggers.priority import TriggerPriority
    TRIGGER_PRIORITY = int(TriggerPriority.NOTABLE)
except ImportError:  # pragma: no cover - hosts older than the priority tiers
    TRIGGER_PRIORITY = 50

logger = logging.getLogger(__name__)

# Absolute largest board FiestaBoard supports: an 8x8 note_array (see
# src/devices.py MAX_NOTES_PER_AXIS). max_aircraft's settings_schema maximum
# is sized to this so a large FiestaPanel isn't starved by a cap meant for a
# Flagship -- the actual number shown on any given board is still clamped to
# what that board's rows can hold (see get_formatted_display).
MAX_NOTES_PER_AXIS = 8
NOTE_ROWS = 3
MAX_POSSIBLE_ROWS = MAX_NOTES_PER_AXIS * NOTE_ROWS
MAX_AIRCRAFT_CEILING = MAX_POSSIBLE_ROWS

# Width budget for the single-line 'formatted'/'headers' template variables.
# This is a content contract (manifest max_lengths declares both at 22), not
# a board dimension -- it never grows past 22 even on a wide note_array (the
# page editor already trusts a variable declared at 22 to fit anywhere a user
# places it), and only ever shrinks for a Note-width board.
DECLARED_LINE_WIDTH = 22

# OpenSky API endpoints
OPENSKY_BASE_URL = "https://opensky-network.org/api"
OPENSKY_OAUTH_URL = f"{OPENSKY_BASE_URL}/v1/oauth/token"
OPENSKY_STATES_URL = f"{OPENSKY_BASE_URL}/states/all"
# Free, keyless callsign -> flight route lookup (origin/destination airports).
ROUTE_LOOKUP_URL = "https://api.adsbdb.com/v0/callsign"
# Free, keyless icao24 -> aircraft type lookup.
AIRCRAFT_LOOKUP_URL = "https://api.adsbdb.com/v0/aircraft"
# Optional live flight lookup (needs an AirLabs API key): backup for the IATA
# flight number and aircraft type when adsbdb cannot give them.
AIRLABS_FLIGHTS_URL = "https://airlabs.co/api/v9/flights"
# After an AirLabs error (bad key, quota spent, outage) skip it for this long
# instead of spending a request on every refresh.
AIRLABS_BACKOFF = timedelta(minutes=15)

# Readable names for common ICAO aircraft type designators (at most 11
# characters, the manifest's max_length for aircraft_type). Designators not
# listed here are shown as-is.
AIRCRAFT_TYPE_NAMES = {
    # Airbus
    "A306": "A300-600", "A310": "A310",
    "BCS1": "A220-100", "BCS3": "A220-300",
    "A318": "A318", "A319": "A319", "A320": "A320", "A321": "A321",
    "A19N": "A319neo", "A20N": "A320neo", "A21N": "A321neo",
    "A332": "A330-200", "A333": "A330-300", "A338": "A330-800neo", "A339": "A330-900neo",
    "A343": "A340-300", "A346": "A340-600",
    "A359": "A350-900", "A35K": "A350-1000", "A388": "A380",
    # Boeing
    "B712": "717",
    "B733": "737-300", "B734": "737-400", "B735": "737-500",
    "B736": "737-600", "B737": "737-700", "B738": "737-800", "B739": "737-900",
    "B37M": "737 MAX 7", "B38M": "737 MAX 8", "B39M": "737 MAX 9", "B3XM": "737 MAX 10",
    "B752": "757-200", "B753": "757-300",
    "B762": "767-200", "B763": "767-300", "B764": "767-400",
    "B744": "747-400", "B748": "747-8",
    "B772": "777-200", "B77L": "777-200LR", "B773": "777-300", "B77W": "777-300ER",
    "B778": "777-8", "B779": "777-9",
    "B788": "787-8", "B789": "787-9", "B78X": "787-10",
    "MD11": "MD-11",
    # Regional
    "E170": "E170", "E75L": "E175", "E75S": "E175", "E175": "E175",
    "E190": "E190", "E195": "E195", "E290": "E190-E2", "E295": "E195-E2",
    "CRJ2": "CRJ200", "CRJ7": "CRJ700", "CRJ9": "CRJ900", "CRJX": "CRJ1000",
    "DH8D": "Dash 8-400",
    "AT43": "ATR 42-300", "AT45": "ATR 42-500", "AT46": "ATR 42-600",
    "AT72": "ATR 72", "AT75": "ATR 72-500", "AT76": "ATR 72-600",
    "SF34": "Saab 340", "RJ85": "Avro RJ85", "RJ1H": "Avro RJ100",
    # Business
    "GLEX": "Global Exp", "GL5T": "Global 5000", "GL6T": "Global 6000", "GL7T": "Global 7500",
    "GLF4": "Gulfstrm IV", "GLF5": "Gulfstrm V", "GLF6": "G650",
    "CL35": "CL350", "CL60": "CL600",
    "C510": "Mustang", "C56X": "Citation XL", "C68A": "Latitude",
    "E50P": "Phenom 100", "E55P": "Phenom 300",
    "F2TH": "Falcon 2000", "F900": "Falcon 900", "FA7X": "Falcon 7X", "FA8X": "Falcon 8X",
    "LJ45": "Learjet 45", "PC12": "PC-12",
}

# Trigger defaults (see manifest settings trigger_*).
DEFAULT_TRIGGER_DURATION_SECONDS = 60
MIN_TRIGGER_DURATION_SECONDS = 10
DEFAULT_TRIGGER_COOLDOWN_MINUTES = 30
# Most uncached lookups (adsbdb route + type, AirLabs backup; 5 s timeout
# each) the trigger path may start per fetch, shared by the type rule and
# trigger enrichment.
# Lookups are memoised, so a skipped aircraft is checked on a later fetch.
MAX_TRIGGER_TYPE_LOOKUPS_PER_FETCH = 10
TRIGGER_ID_PREFIX = "aircraft_"
# Manifest max_length for flight_number and aircraft_type.
MAX_FLIGHT_NUMBER_LENGTH = 7
MAX_AIRCRAFT_TYPE_LENGTH = 11


def _utcnow() -> datetime:
    """Clock for trigger cooldowns (a seam for tests)."""
    return datetime.now(timezone.utc)


# State vector field indices (OpenSky API returns arrays)
# See: https://openskynetwork.github.io/opensky-api/rest.html#response
STATE_VECTOR_INDICES = {
    "icao24": 0,
    "callsign": 1,
    "origin_country": 2,
    "time_position": 3,
    "last_contact": 4,
    "longitude": 5,
    "latitude": 6,
    "baro_altitude": 7,
    "on_ground": 8,
    "velocity": 9,
    "true_track": 10,
    "vertical_rate": 11,
    "sensors": 12,
    "geo_altitude": 13,
    "squawk": 14,
    "spi": 15,
    "position_source": 16,
}


class NearbyAircraftPlugin(PluginBase):
    """Nearby aircraft plugin.
    
    Fetches real-time aircraft data from OpenSky Network API and displays
    aircraft within a specified radius showing call sign, altitude, ground
    speed, and squawk code.
    """
    
    def __init__(self, manifest: Dict[str, Any]):
        """Initialize the nearby aircraft plugin."""
        super().__init__(manifest)
        self._cache: Optional[Dict[str, Any]] = None
        self._access_token: Optional[str] = None
        self._token_expires_at: Optional[datetime] = None
        self._reset_trigger_state()

    def _reset_trigger_state(self) -> None:
        """Forget every trigger match, cooldown and pending trigger (memory only).

        Triggers already handed to the host are queued for dismissal (and
        queued dismissals are kept) so the next check_triggers() clears them
        instead of orphaning them.
        """
        dismissals = list(getattr(self, "_trigger_dismissals", []))
        dismissals += [f"{TRIGGER_ID_PREFIX}{icao}" for icao in sorted(getattr(self, "_trigger_active", ()))]
        # icao24 -> when its trigger was last fired (cooldown bookkeeping).
        self._trigger_fired_at: Dict[str, datetime] = {}
        # icao24s whose trigger was handed to the host and may still be active.
        self._trigger_active: Set[str] = set()
        # Triggers found by the latest fresh fetch, not yet returned.
        self._trigger_pending: Dict[str, TriggerResult] = {}
        # trigger_ids to dismiss early (aircraft no longer matches or left).
        self._trigger_dismissals: List[str] = dismissals
        # Uncached lookups (adsbdb route + type, AirLabs backup) left for this fetch.
        self._trigger_lookup_budget = MAX_TRIGGER_TYPE_LOOKUPS_PER_FETCH
    
    @property
    def plugin_id(self) -> str:
        return "nearby_aircraft"
    
    def validate_config(self, config: Dict[str, Any]) -> List[str]:
        """Validate nearby aircraft configuration."""
        errors = []
        
        lat = config.get("latitude")
        lon = config.get("longitude")
        
        if lat is None:
            errors.append("Latitude is required")
        elif not isinstance(lat, (int, float)) or not (-90 <= lat <= 90):
            errors.append("Latitude must be a number between -90 and 90")
        
        if lon is None:
            errors.append("Longitude is required")
        elif not isinstance(lon, (int, float)) or not (-180 <= lon <= 180):
            errors.append("Longitude must be a number between -180 and 180")
        
        radius_km = config.get("radius_km", 50)
        if not isinstance(radius_km, (int, float)) or radius_km < 1:
            errors.append("Radius must be at least 1 km")
        
        max_aircraft = config.get("max_aircraft", 4)
        if not isinstance(max_aircraft, int) or not (1 <= max_aircraft <= MAX_AIRCRAFT_CEILING):
            errors.append(f"Max aircraft must be between 1 and {MAX_AIRCRAFT_CEILING}")
        
        refresh_seconds = config.get("refresh_seconds", 120)
        if not isinstance(refresh_seconds, int) or refresh_seconds < 10:
            errors.append("Refresh interval must be at least 10 seconds")

        for key, label in (("trigger_call_signs", "Trigger call signs"),
                           ("trigger_aircraft_types", "Trigger aircraft types")):
            value = config.get(key)
            if value is not None and not isinstance(value, str):
                errors.append(f"{label} must be a comma-separated list")

        max_alt = config.get("trigger_max_altitude_ft")
        if not self._is_unset(max_alt) and (
            isinstance(max_alt, bool) or not isinstance(max_alt, int) or max_alt < 0
        ):
            errors.append("Trigger max altitude must be a whole number of feet, 0 or more")

        max_dist = config.get("trigger_max_distance_km")
        if not self._is_unset(max_dist) and (
            isinstance(max_dist, bool) or not isinstance(max_dist, (int, float)) or max_dist < 0
        ):
            errors.append("Trigger max distance must be a number of km, 0 or more")

        duration = config.get("trigger_duration_seconds", DEFAULT_TRIGGER_DURATION_SECONDS)
        if isinstance(duration, bool) or not isinstance(duration, int) or duration < MIN_TRIGGER_DURATION_SECONDS:
            errors.append(f"Trigger duration must be at least {MIN_TRIGGER_DURATION_SECONDS} seconds")

        cooldown = config.get("trigger_cooldown_minutes", DEFAULT_TRIGGER_COOLDOWN_MINUTES)
        if isinstance(cooldown, bool) or not isinstance(cooldown, int) or cooldown < 0:
            errors.append("Trigger cooldown must be 0 minutes or more")

        return errors
    
    def on_config_change(self, old_config: Dict[str, Any], new_config: Dict[str, Any]) -> None:
        """Drop cached aircraft and token so a config change takes effect immediately.
        
        The cache is keyed only on age, so without this a change to
        `latitude`/`longitude`/`radius_km` would keep serving aircraft found
        around the old position for up to refresh_seconds. The access token
        is minted from `client_id`/`client_secret`, so it goes too, and so
        does an AirLabs backoff (the key may just have been fixed).
        """
        self._cache = None
        self._access_token = None
        self._token_expires_at = None
        self._airlabs_backoff_until = None
        if (old_config or {}).get("airlabs_api_key") != (new_config or {}).get("airlabs_api_key"):
            self._airlabs_cache = {}
        self._reset_trigger_state()
        logger.debug("Cleared cached aircraft after config change")
    
    @staticmethod
    def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate distance between two points on Earth in km.
        
        Uses the Haversine formula to compute great-circle distance.
        
        Args:
            lat1: Latitude of first point in degrees
            lon1: Longitude of first point in degrees
            lat2: Latitude of second point in degrees
            lon2: Longitude of second point in degrees
            
        Returns:
            Distance in kilometers
        """
        # Convert to radians
        lon1, lat1, lon2, lat2 = map(radians, [lon1, lat1, lon2, lat2])
        
        # Haversine formula
        dlon = lon2 - lon1
        dlat = lat2 - lat1
        a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
        c = 2 * asin(sqrt(a))
        
        # Earth radius in kilometers
        return 6371 * c
    
    @staticmethod
    def calculate_bounding_box(lat: float, lon: float, radius_km: float) -> Dict[str, float]:
        """Calculate bounding box from center point and radius.
        
        Args:
            lat: Center latitude in degrees
            lon: Center longitude in degrees
            radius_km: Radius in kilometers
            
        Returns:
            Dictionary with lamin, lamax, lomin, lomax
        """
        # Approximate conversion: 1 degree latitude ≈ 111 km
        # For longitude, adjust by latitude (cos(lat))
        lat_delta = radius_km / 111.0
        lon_delta = radius_km / (111.0 * abs(cos(radians(lat))))
        
        return {
            "lamin": lat - lat_delta,
            "lamax": lat + lat_delta,
            "lomin": lon - lon_delta,
            "lomax": lon + lon_delta,
        }
    
    def _get_access_token(self) -> Optional[str]:
        """Get OAuth2 access token for authenticated requests.
        
        Returns:
            Access token string, or None if authentication fails
        """
        client_id = self.config.get("client_id", "").strip()
        client_secret = self.config.get("client_secret", "").strip()
        
        # If no credentials, return None (use unauthenticated)
        if not client_id or not client_secret:
            return None
        
        # Check if we have a valid cached token
        if self._access_token and self._token_expires_at:
            if datetime.now() < self._token_expires_at:
                return self._access_token
        
        # Request new token
        try:
            response = requests.post(
                OPENSKY_OAUTH_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                timeout=10
            )
            
            if response.status_code != 200:
                logger.warning(f"Failed to get OAuth token: {response.status_code}")
                return None
            
            data = response.json()
            self._access_token = data.get("access_token")
            expires_in = data.get("expires_in", 3600)  # Default 1 hour
            
            # Set expiration time (subtract 60 seconds for safety margin)
            self._token_expires_at = datetime.now() + timedelta(seconds=expires_in - 60)
            
            logger.debug("Successfully obtained OAuth access token")
            return self._access_token
            
        except Exception as e:
            logger.error(f"Error getting OAuth token: {e}")
            return None
    
    def _get_api_headers(self) -> Dict[str, str]:
        """Get headers for API requests, including auth if available."""
        headers = {}
        token = self._get_access_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers
    
    def _parse_state_vector(self, state_vector: List[Any]) -> Optional[Dict[str, Any]]:
        """Parse a single state vector from OpenSky API.
        
        Args:
            state_vector: Array of state vector values from API
            
        Returns:
            Parsed aircraft data dictionary, or None if invalid
        """
        try:
            if not state_vector or len(state_vector) < 17:
                return None
            
            # Extract fields using indices
            icao24 = state_vector[STATE_VECTOR_INDICES["icao24"]]
            callsign_raw = state_vector[STATE_VECTOR_INDICES["callsign"]]
            latitude = state_vector[STATE_VECTOR_INDICES["latitude"]]
            longitude = state_vector[STATE_VECTOR_INDICES["longitude"]]
            baro_altitude = state_vector[STATE_VECTOR_INDICES["baro_altitude"]]
            geo_altitude = state_vector[STATE_VECTOR_INDICES["geo_altitude"]]
            velocity = state_vector[STATE_VECTOR_INDICES["velocity"]]
            squawk_raw = state_vector[STATE_VECTOR_INDICES["squawk"]]
            on_ground = state_vector[STATE_VECTOR_INDICES["on_ground"]]
            
            # Skip aircraft on ground
            if on_ground:
                return None
            
            # Skip if no position data
            if latitude is None or longitude is None:
                return None
            
            # Get callsign - use ICAO address if missing
            if callsign_raw:
                callsign = str(callsign_raw).strip()
            else:
                # Use ICAO 24-bit address as fallback
                callsign = str(icao24) if icao24 else "UNKNOWN"
            
            # Get altitude - prefer geo_altitude, fallback to baro_altitude
            altitude_m = None
            if geo_altitude is not None:
                altitude_m = float(geo_altitude)
            elif baro_altitude is not None:
                altitude_m = float(baro_altitude)
            
            if altitude_m is None:
                return None  # Skip aircraft without altitude
            
            # Convert altitude from meters to feet
            altitude_ft = int(altitude_m * 3.28084)
            
            # Get velocity (ground speed) in m/s, convert to knots
            if velocity is None:
                return None  # Skip aircraft without velocity
            
            velocity_ms = float(velocity)
            ground_speed_knots = int(velocity_ms * 1.94384)
            
            # Get squawk code
            # Squawk can be None, empty string, 0, or a valid 4-digit code
            if squawk_raw is not None and squawk_raw != "":
                try:
                    squawk_int = int(float(squawk_raw))  # Handle string numbers
                    if squawk_int == 0:
                        # 0 means no squawk code assigned
                        squawk = "----"
                    else:
                        squawk = str(squawk_int).zfill(4)
                except (ValueError, TypeError):
                    squawk = "----"
            else:
                squawk = "----"
            
            # Note: no "formatted" field here. The board a plugin renders on
            # is not known until get_data(board) binds self.board, and
            # get_data()'s cache (and this plugin's own self._cache, see
            # on_config_change) is geometry-agnostic, so a per-board string
            # baked in here would leak one board's column widths into
            # another's render. _align_formatting derives "formatted" fresh
            # from these raw fields every time, bound to self.board.
            return {
                "icao24": str(icao24) if icao24 else "",
                "call_sign": callsign,
                "altitude": altitude_ft,
                "ground_speed": ground_speed_knots,
                "squawk": squawk,
                "latitude": float(latitude),
                "longitude": float(longitude),
            }
            
        except (ValueError, TypeError, IndexError) as e:
            logger.debug(f"Error parsing state vector: {e}")
            return None
    
    def _lookup_route(self, call_sign: str) -> Dict[str, str]:
        """Best-effort origin/destination airport (IATA, falling back to ICAO) for a call sign.

        Results (including misses) are memoised per call sign so repeated
        refreshes don't hit the route API again. Never raises.
        """
        empty = {"origin": "", "destination": "", "flight_number": ""}
        key = (call_sign or "").strip().upper()
        if not key:
            return empty
        cache = self.__dict__.setdefault("_route_cache", {})
        if key in cache:
            return cache[key]
        try:
            resp = requests.get(f"{ROUTE_LOOKUP_URL}/{key}", timeout=5)
            if resp.status_code == 404:
                cache[key] = empty
                return empty
            if resp.status_code != 200:
                return empty
            route = (resp.json().get("response") or {}).get("flightroute") or {}
            result = {}
            for field, name in (("origin", "origin"), ("destination", "destination")):
                airport = route.get(field) or {}
                result[name] = str(airport.get("iata_code") or airport.get("icao_code") or "")
            result["flight_number"] = self._flight_number(key, route.get("airline") or {})
            cache[key] = result
            return result
        except Exception as e:
            logger.debug(f"Route lookup failed for {key}: {e}")
            return empty

    @staticmethod
    def _flight_number(call_sign: str, airline: Dict[str, Any]) -> str:
        """IATA flight number (e.g. BA123) derived from an ICAO call sign (BAW123).

        Only numeric call signs map to a flight number. Alphanumeric ATC call
        signs (e.g. BEL5EN) are unrelated to the published flight number, and
        swapping in the IATA prefix would give a fake one (SN5EN), so those
        return "".
        """
        icao = str(airline.get("icao") or "").upper()
        iata = str(airline.get("iata") or "").upper()
        if not icao or not iata or not call_sign.startswith(icao):
            return ""
        suffix = call_sign[len(icao):]
        if not suffix.isdigit():
            return ""
        return f"{iata}{int(suffix)}"

    def _airlabs_key(self) -> str:
        value = self.config.get("airlabs_api_key")
        return value.strip() if isinstance(value, str) else ""

    def _airlabs_cache_key(self, icao24: str, call_sign: str) -> Optional[Tuple[str, str]]:
        """Memo key for an AirLabs lookup, or None when AirLabs is not used."""
        icao = (icao24 or "").strip().upper()
        if not self._airlabs_key() or not icao:
            return None
        return icao, (call_sign or "").strip().upper()

    def _lookup_airlabs(self, icao24: str, call_sign: str) -> Dict[str, str]:
        """Best-effort flight number and aircraft type from AirLabs live flights.

        Returns ``{"flight_number", "aircraft_type"}``: the published IATA
        flight number (e.g. SN2903) and the readable type of its ICAO
        designator (A20N -> A320neo, via AIRCRAFT_TYPE_NAMES). Looks the
        aircraft up by its icao24 address, so alphanumeric ATC call signs
        (BEL5EN) still resolve. One request serves both fields; the result,
        a miss included, is memoised per (icao24, call sign), so a new leg
        with a new call sign is looked up again. Empty fields without an API
        key, on a miss or on an error. Errors are not memoised; an error
        response (error body or non-200) also pauses AirLabs for
        AIRLABS_BACKOFF. Never raises.
        """
        empty = {"flight_number": "", "aircraft_type": ""}
        key = self._airlabs_cache_key(icao24, call_sign)
        if key is None:
            return empty
        cache = self.__dict__.setdefault("_airlabs_cache", {})
        if key in cache:
            return cache[key]
        backoff_until = self.__dict__.get("_airlabs_backoff_until")
        if backoff_until and _utcnow() < backoff_until:
            return empty
        try:
            resp = requests.get(
                AIRLABS_FLIGHTS_URL,
                params={
                    "api_key": self._airlabs_key(),
                    "hex": key[0],
                    "_fields": "hex,flight_iata,aircraft_icao",
                },
                timeout=5,
            )
            body = resp.json() if resp.status_code == 200 else None
            if not isinstance(body, (dict, list)) or (isinstance(body, dict) and body.get("error")):
                error = body.get("error") if isinstance(body, dict) else resp.status_code
                logger.warning(f"AirLabs lookup failed ({error}); retrying in {AIRLABS_BACKOFF}")
                self._airlabs_backoff_until = _utcnow() + AIRLABS_BACKOFF
                return empty
            flights = body.get("response") if isinstance(body, dict) else body
            flight = next(
                (f for f in flights or [] if isinstance(f, dict)
                 and str(f.get("hex") or "").upper() == key[0]),
                {},
            )
            flight_number = str(flight.get("flight_iata") or "").strip().upper()
            if len(flight_number) > MAX_FLIGHT_NUMBER_LENGTH:
                flight_number = ""
            icao_type = str(flight.get("aircraft_icao") or "").strip().upper()
            aircraft_type = AIRCRAFT_TYPE_NAMES.get(icao_type, icao_type)
            if len(aircraft_type) > MAX_AIRCRAFT_TYPE_LENGTH:
                aircraft_type = ""
            cache[key] = {"flight_number": flight_number, "aircraft_type": aircraft_type}
            return cache[key]
        except Exception as e:
            logger.debug(f"AirLabs lookup failed for {key[0]}: {e}")
            return empty

    @staticmethod
    def _flight_gaps(info: Dict[str, str]) -> List[str]:
        """Fields adsbdb left empty that AirLabs may fill."""
        return [field for field in ("flight_number", "aircraft_type") if not info.get(field)]

    def _fill_from_airlabs(self, info: Dict[str, str], icao24: str, call_sign: str) -> Dict[str, str]:
        """*info* with only its empty flight number/type filled from AirLabs.

        adsbdb's values are never replaced, and AirLabs is not asked when
        adsbdb gave both.
        """
        gaps = self._flight_gaps(info)
        if not gaps:
            return info
        airlabs = self._lookup_airlabs(icao24, call_sign)
        return {**info, **{field: airlabs[field] for field in gaps if airlabs.get(field)}}

    def _lookup_flight(self, icao24: str, call_sign: str) -> Dict[str, str]:
        """Route, flight number and aircraft type: adsbdb first, AirLabs for the gaps."""
        info = {**self._lookup_route(call_sign), "aircraft_type": self._lookup_aircraft_type(icao24)}
        return self._fill_from_airlabs(info, icao24, call_sign)

    def _lookup_aircraft_type(self, icao24: str) -> str:
        """Best-effort readable aircraft type (e.g. 737-800) for an icao24 address.

        Common ICAO type designators are mapped via AIRCRAFT_TYPE_NAMES;
        others are returned as the raw designator. Memoised per address (including misses). Never raises.
        """
        key = (icao24 or "").strip().upper()
        if not key:
            return ""
        cache = self.__dict__.setdefault("_type_cache", {})
        if key in cache:
            return cache[key]
        try:
            resp = requests.get(f"{AIRCRAFT_LOOKUP_URL}/{key}", timeout=5)
            if resp.status_code == 404:
                cache[key] = ""
                return ""
            if resp.status_code != 200:
                return ""
            info = (resp.json().get("response") or {}).get("aircraft") or {}
            icao_type = str(info.get("icao_type") or "").upper()
            cache[key] = AIRCRAFT_TYPE_NAMES.get(icao_type, icao_type)
            return cache[key]
        except Exception as e:
            logger.debug(f"Aircraft type lookup failed for {key}: {e}")
            return ""

    def _render_width(self) -> int:
        """Column budget for the single-line 'formatted'/'headers' variables.

        Derived from ``self.board`` (defaulting to a Flagship's 22 when
        unbound, matching the platform contract for a legacy/unscoped call),
        then capped at :data:`DECLARED_LINE_WIDTH`. A note_array's ``cols``
        is always a multiple of :data:`NOTE_COLS` (15) and a Flagship's is
        22, so this cap can only ever land on exactly 15 or 22 -- there is no
        board whose width falls strictly between them -- which is why the
        two formatting branches below only need to handle those two cases.
        """
        board = self.board
        cols = board.cols if board is not None else DECLARED_LINE_WIDTH
        return min(cols, DECLARED_LINE_WIDTH)

    def _align_formatting(self, aircraft_list: List[Dict]) -> Tuple[List[Dict], str]:
        """Derive 'formatted'/'headers' for *aircraft_list*, sized to the board.

        Recomputed from each aircraft's raw fields every call rather than
        read from a previously-baked string: the raw aircraft dicts (and
        this plugin's own network-avoidance cache in ``self._cache``) carry
        no board-specific formatting, so the same cached data renders
        correctly whichever board is currently bound to ``self.board``. A
        Note-width board (15 cols) drops the squawk column and abbreviates
        headers rather than truncating every field to fit a layout designed
        for 22; every wider board (including every note_array at least two
        notes wide) uses the classic layout.

        Args:
            aircraft_list: List of aircraft dictionaries (mutated in place;
                each gets a "formatted" key).

        Returns:
            Tuple of (aircraft list, headers string).
        """
        width = self._render_width()
        if width <= NOTE_COLS:
            return self._align_formatting_narrow(aircraft_list, width)
        return self._align_formatting_wide(aircraft_list, width)

    def _align_formatting_wide(self, aircraft_list: List[Dict], width: int) -> Tuple[List[Dict], str]:
        """Classic 4-column layout (call sign, altitude, speed, squawk)."""
        if not aircraft_list:
            return aircraft_list, "CALLSGN ALT GS SQWK"

        # Calculate max widths for each field
        max_call_sign_width = 0
        max_altitude_width = 0
        max_speed_width = 0

        for aircraft in aircraft_list:
            call_sign = str(aircraft.get("call_sign", ""))
            altitude = str(aircraft.get("altitude", 0))
            ground_speed = str(aircraft.get("ground_speed", 0))

            max_call_sign_width = max(max_call_sign_width, len(call_sign))
            max_altitude_width = max(max_altitude_width, len(altitude))
            max_speed_width = max(max_speed_width, len(ground_speed))

        # Cap call sign/altitude/speed so the combined line can never grow
        # past `width` regardless of how long any one field's content is.
        max_call_sign_width = min(max_call_sign_width, 8)
        max_altitude_width = min(max(max_altitude_width, 5), 6)  # 5-6 digits
        max_speed_width = min(max(max_speed_width, 3), 4)  # 3-4 digits
        max_squawk_width = 4  # Squawk is always 4

        # Generate aligned headers. ljust/rjust never truncate, so a label
        # longer than its column (e.g. "CALLSGN" in a narrower column) still
        # renders whole; the final slice below is the actual width guard.
        callsign_header = "CALLSGN".ljust(max_call_sign_width)
        altitude_header = "ALT".rjust(max_altitude_width)
        speed_header = "GS".rjust(max_speed_width)
        squawk_header = "SQWK".rjust(max_squawk_width)
        headers = f"{callsign_header} {altitude_header} {speed_header} {squawk_header}"
        headers = headers[:width]

        # Apply aligned formatting to all aircraft
        for aircraft in aircraft_list:
            call_sign = str(aircraft.get("call_sign", ""))[:max_call_sign_width]
            altitude = aircraft.get("altitude", 0)
            ground_speed = aircraft.get("ground_speed", 0)
            squawk = str(aircraft.get("squawk", "----"))[:4]

            call_sign_display = call_sign.ljust(max_call_sign_width)
            altitude_str = str(altitude).rjust(max_altitude_width)
            speed_str = str(ground_speed).rjust(max_speed_width)
            squawk_str = squawk.rjust(max_squawk_width)

            # Combine: "CALLSGN  ALT   GS  SQWK"
            formatted = f"{call_sign_display} {altitude_str} {speed_str} {squawk_str}"
            aircraft["formatted"] = formatted[:width]

        return aircraft_list, headers

    @staticmethod
    def _align_formatting_narrow(aircraft_list: List[Dict], width: int) -> Tuple[List[Dict], str]:
        """Abbreviated 3-column layout for a Note-width board (15 cols).

        The classic layout needs 25 characters at minimum; a Note only has
        15. Squawk is dropped first (least useful for a glance at what's
        overhead) and labels are abbreviated so headers still fit.
        """
        call_w, alt_w, speed_w = 5, 5, 3  # 5 + 1 + 5 + 1 + 3 == 15 == NOTE_COLS

        headers = f"{'CALL'.ljust(call_w)} {'ALT'.rjust(alt_w)} {'GS'.rjust(speed_w)}"[:width]
        if not aircraft_list:
            return aircraft_list, headers

        for aircraft in aircraft_list:
            call_sign = str(aircraft.get("call_sign", ""))[:call_w].ljust(call_w)
            altitude = str(aircraft.get("altitude", 0))[:alt_w].rjust(alt_w)
            ground_speed = str(aircraft.get("ground_speed", 0))[:speed_w].rjust(speed_w)
            formatted = f"{call_sign} {altitude} {ground_speed}"
            aircraft["formatted"] = formatted[:width]

        return aircraft_list, headers

    def _format_aircraft_line(self, call_sign: str, altitude: int, ground_speed: int, squawk: str) -> str:
        """Format one aircraft's data for display, sized to the current board.

        Standalone formatter (no alignment across a list) kept for direct
        use and for the tests that exercise it. Bound to ``self.board`` the
        same way :meth:`_align_formatting` is, via :meth:`_render_width`, so
        it never emits a row wider than the board actually has.

        Args:
            call_sign: Aircraft call sign
            altitude: Altitude in feet
            ground_speed: Ground speed in knots
            squawk: Squawk code (4 chars)

        Returns:
            Formatted string, at most ``self._render_width()`` chars.
        """
        width = self._render_width()
        if width <= NOTE_COLS:
            call_sign_display = call_sign[:5].ljust(5)
            altitude_str = str(altitude)[:5].rjust(5)
            speed_str = str(ground_speed)[:3].rjust(3)
            formatted = f"{call_sign_display} {altitude_str} {speed_str}"
            return formatted[:width]

        # Truncate and pad call sign to 8 chars (left-aligned)
        call_sign_display = call_sign[:8].ljust(8)

        # Format altitude (right-aligned, 5-6 digits)
        altitude_str = str(altitude).rjust(6)

        # Format ground speed (right-aligned, 3-4 digits)
        speed_str = str(ground_speed).rjust(4)

        # Format squawk (right-aligned, 4 chars)
        squawk_str = squawk[:4].rjust(4)

        # Combine: "CALLSGN  ALT   GS  SQWK"
        formatted = f"{call_sign_display} {altitude_str} {speed_str} {squawk_str}"
        return formatted[:width]
    
    def _empty_result_data(self) -> Dict[str, Any]:
        """Payload for 'no aircraft nearby', sized to the current board.

        Built the same board-aware way as a populated result (via
        :meth:`_align_formatting`) so a Note-width board never gets handed
        the 19-character literal "NO AIRCRAFT NEARBY", which alone is wider
        than a Note (15 cols).
        """
        _, headers = self._align_formatting([])
        width = self._render_width()
        message = "NO AIRCRAFT NEARBY" if width >= len("NO AIRCRAFT NEARBY") else "NO AIRCRAFT"
        return {
            "aircraft_count": 0,
            "aircraft": [],
            "call_sign": "",
            "altitude": 0,
            "ground_speed": 0,
            "squawk": "----",
            "origin": "",
            "destination": "",
            "flight_number": "",
            "aircraft_type": "",
            "formatted": message[:width],
            "headers": headers,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }

    def _build_result_data(self, raw_aircraft: List[Dict], last_updated: str) -> Dict[str, Any]:
        """Build the data dict for *raw_aircraft*, formatting fresh for the current board.

        ``raw_aircraft`` holds only geometry-independent fields (no
        "formatted"/"headers" baked in -- see :meth:`_parse_state_vector`),
        whether it came straight from the network or from ``self._cache``.
        Copying before calling :meth:`_align_formatting` (which mutates its
        argument) keeps the cached raw dicts themselves geometry-agnostic
        across repeated calls, which is what lets the very same cached fetch
        render correctly on whichever board is bound to ``self.board`` this
        time (see the F6 note in on_config_change and the module docstring
        on _parse_state_vector).
        """
        if not raw_aircraft:
            return self._empty_result_data()

        aircraft = [dict(a) for a in raw_aircraft]
        aircraft, headers = self._align_formatting(aircraft)
        primary = aircraft[0]

        return {
            "call_sign": primary["call_sign"],
            "altitude": primary["altitude"],
            "ground_speed": primary["ground_speed"],
            "squawk": primary["squawk"],
            "origin": primary.get("origin", ""),
            "destination": primary.get("destination", ""),
            "flight_number": primary.get("flight_number", ""),
            "aircraft_type": primary.get("aircraft_type", ""),
            "formatted": primary["formatted"],
            "headers": headers,
            "aircraft_count": len(aircraft),
            "last_updated": last_updated,
            "aircraft": aircraft,
        }

    def _max_aircraft_config(self) -> int:
        """The user's configured aircraft cap, clamped to sane bounds.

        A ceiling independent of any one board (see MAX_AIRCRAFT_CEILING) --
        how many of those are actually *shown* is decided per render in
        :meth:`_build_display_lines`, from ``self.board.rows``.
        """
        max_aircraft = self.config.get("max_aircraft", 4)
        if not isinstance(max_aircraft, int) or max_aircraft < 1:
            max_aircraft = 4
        return min(max_aircraft, MAX_AIRCRAFT_CEILING)

    def _build_display_lines(self, data: Dict[str, Any]) -> List[str]:
        """This plugin's whole-board display for *data*, sized to ``self.board``.

        Shared by :meth:`fetch_data` (``PluginResult.formatted_lines`` -- the
        path a page actually renders when it displays this plugin directly,
        see ``src/displays/service.py``) and :meth:`get_formatted_display`
        (the documented hook core does not call today), so both react to the
        bound board identically and can't drift apart.

        Title and headers are decoration around the actual data, so each is
        only included while at least one row remains for an aircraft
        afterward -- a Note (3 rows) keeps both and shows one aircraft;
        nothing in FiestaBoard's board catalog is short enough to need to
        drop them, but this degrades instead of overflowing if that changes.
        The number of aircraft rows is the smallest of: the user's
        max_aircraft config, the rows actually left on this board, and how
        many aircraft there are -- never a literal count baked in here.
        """
        board = self.board
        rows = board.rows if board is not None else 6
        cols = board.cols if board is not None else 22

        aircraft = data.get("aircraft") or []
        max_aircraft = self._max_aircraft_config()

        lines: List[str] = []
        remaining_rows = rows
        if remaining_rows > 2:
            lines.append("NEARBY AIRCRAFT".center(cols)[:cols])
            remaining_rows -= 1
        headers = str(data.get("headers") or "")
        if headers and remaining_rows > 1:
            lines.append(headers[:cols])
            remaining_rows -= 1

        shown = max(0, min(max_aircraft, remaining_rows, len(aircraft)))
        for ac in aircraft[:shown]:
            lines.append(str(ac.get("formatted", ""))[:cols])

        while len(lines) < rows:
            lines.append("")
        return lines[:rows]

    def _success_result(self, raw_aircraft: List[Dict], last_updated: str) -> PluginResult:
        """Build the full PluginResult (data + formatted_lines) for *raw_aircraft*."""
        data = self._build_result_data(raw_aircraft, last_updated)
        return PluginResult(available=True, data=data, formatted_lines=self._build_display_lines(data))

    def _empty_success_result(self) -> PluginResult:
        """PluginResult for 'no aircraft nearby', sized to the current board."""
        data = self._empty_result_data()
        return PluginResult(available=True, data=data, formatted_lines=self._build_display_lines(data))

    def fetch_data(self) -> PluginResult:
        """Fetch nearby aircraft data from OpenSky API."""
        lat = self.config.get("latitude")
        lon = self.config.get("longitude")
        radius_km = self.config.get("radius_km", 50)
        max_aircraft = self._max_aircraft_config()
        refresh_seconds = self.config.get("refresh_seconds", 120)

        if lat is None or lon is None:
            return PluginResult(
                available=False,
                error="Latitude and longitude are required"
            )

        # Check cache first. self._cache holds only raw, geometry-independent
        # aircraft fields (see _build_result_data), so reusing it across a
        # config-unchanged, refresh-window-fresh call is safe even when
        # self.board differs from the board that was bound when it was
        # populated -- formatting is derived fresh below either way.
        if self._cache and self._cache.get("aircraft"):
            last_updated = self._cache.get("last_updated", "")
            if last_updated:
                try:
                    cache_time = datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
                    age_seconds = (datetime.now(cache_time.tzinfo) - cache_time).total_seconds()
                    if age_seconds < refresh_seconds:
                        logger.debug(f"Using cached data (age: {age_seconds:.0f}s < {refresh_seconds}s)")
                        return self._success_result(self._cache["aircraft"], self._cache["last_updated"])
                except Exception:
                    pass  # If cache time parsing fails, continue to fetch

        try:
            # Calculate bounding box
            bbox = self.calculate_bounding_box(lat, lon, radius_km)
            
            # Fetch state vectors from OpenSky API
            headers = self._get_api_headers()
            params = {
                "lamin": bbox["lamin"],
                "lamax": bbox["lamax"],
                "lomin": bbox["lomin"],
                "lomax": bbox["lomax"],
            }
            
            response = requests.get(OPENSKY_STATES_URL, params=params, headers=headers, timeout=10)
            
            # Handle rate limiting
            if response.status_code == 429:
                logger.warning("OpenSky API rate limit exceeded, using cached data if available")
                if self._cache and self._cache.get("aircraft"):
                    return self._success_result(self._cache["aircraft"], self._cache["last_updated"])
                return PluginResult(
                    available=False,
                    error="API rate limit exceeded. Please wait or use authentication."
                )

            if response.status_code != 200:
                logger.error(f"OpenSky API error: {response.status_code}")
                if self._cache and self._cache.get("aircraft"):
                    return self._success_result(self._cache["aircraft"], self._cache["last_updated"])
                return PluginResult(
                    available=False,
                    error=f"API error: {response.status_code}"
                )

            data = response.json()
            states = data.get("states")

            if not states:
                # No aircraft found, return empty result
                result = self._empty_success_result()
                self._update_trigger_state([], [], result.data)
                return result

            # Parse state vectors and filter by distance
            nearby_aircraft = []
            for state_vector in states:
                aircraft = self._parse_state_vector(state_vector)
                if aircraft:
                    # Calculate actual distance
                    distance_km = self.haversine_distance(
                        lat, lon,
                        aircraft["latitude"], aircraft["longitude"]
                    )

                    if distance_km <= radius_km:
                        aircraft["distance_km"] = round(distance_km, 1)
                        nearby_aircraft.append(aircraft)

            # Sort by distance and limit to the user's configured max_aircraft.
            # This bounds the *fetched/cached* pool, independent of any one
            # board's row count -- how many of these are actually shown is
            # decided per render in get_formatted_display, from board.rows.
            nearby_aircraft.sort(key=lambda a: a.get("distance_km", float('inf')))
            # Trigger rules see every aircraft in the radius, so match before
            # truncating to max_aircraft.
            in_radius = list(nearby_aircraft)
            trigger_matches = self._match_trigger_rules(in_radius)
            nearby_aircraft = nearby_aircraft[:max_aircraft]

            if not nearby_aircraft:
                result = self._empty_success_result()
                self._update_trigger_state(in_radius, trigger_matches, result.data)
                return result

            for aircraft in nearby_aircraft:
                aircraft.update(self._lookup_flight(aircraft["icao24"], aircraft["call_sign"]))

            last_updated = datetime.now(timezone.utc).isoformat()
            # Cache raw aircraft only -- no "formatted"/"headers" baked in,
            # so this same cached fetch renders correctly for whichever
            # board is bound on a later call (see _build_result_data).
            self._cache = {"aircraft": nearby_aircraft, "last_updated": last_updated}
            result = self._success_result(nearby_aircraft, last_updated)
            self._update_trigger_state(in_radius, trigger_matches, result.data)
            return result

        except requests.exceptions.RequestException as e:
            logger.exception("Error fetching aircraft data")
            if self._cache and self._cache.get("aircraft"):
                return self._success_result(self._cache["aircraft"], self._cache["last_updated"])
            return PluginResult(available=False, error=f"Network error: {str(e)}")
        except Exception as e:
            logger.exception("Unexpected error fetching aircraft data")
            if self._cache and self._cache.get("aircraft"):
                return self._success_result(self._cache["aircraft"], self._cache["last_updated"])
            return PluginResult(available=False, error=str(e))

    # ── Triggers ──────────────────────────────────────────────────────────

    @staticmethod
    def _is_unset(value: Any) -> bool:
        return value is None or (isinstance(value, str) and not value.strip())

    @staticmethod
    def _split_list(value: Any) -> List[str]:
        """Comma-separated setting -> upper-case, non-empty entries."""
        if not isinstance(value, str):
            return []
        return [part.strip().upper() for part in value.split(",") if part.strip()]

    def _number_setting(self, key: str, integer: bool) -> Optional[float]:
        """A numeric rule setting, or None when it is unset or invalid."""
        value = self.config.get(key)
        if self._is_unset(value) or isinstance(value, bool):
            return None
        try:
            number = int(value) if integer else float(value)
        except (TypeError, ValueError):
            return None
        return number if number >= 0 else None

    def _trigger_rules(self) -> Dict[str, Any]:
        """The watch rules the user filled in (an empty dict when none)."""
        rules: Dict[str, Any] = {}
        call_signs = self._split_list(self.config.get("trigger_call_signs"))
        if call_signs:
            rules["call_signs"] = call_signs
        types = self._split_list(self.config.get("trigger_aircraft_types"))
        if types:
            rules["aircraft_types"] = types
        max_alt = self._number_setting("trigger_max_altitude_ft", integer=True)
        if max_alt is not None:
            rules["max_altitude_ft"] = max_alt
        max_dist = self._number_setting("trigger_max_distance_km", integer=False)
        if max_dist is not None:
            rules["max_distance_km"] = max_dist
        return rules

    def _trigger_duration_seconds(self) -> int:
        value = self.config.get("trigger_duration_seconds", DEFAULT_TRIGGER_DURATION_SECONDS)
        if isinstance(value, bool) or not isinstance(value, int) or value < MIN_TRIGGER_DURATION_SECONDS:
            return DEFAULT_TRIGGER_DURATION_SECONDS
        return value

    def _trigger_cooldown(self) -> timedelta:
        value = self.config.get("trigger_cooldown_minutes", DEFAULT_TRIGGER_COOLDOWN_MINUTES)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            value = DEFAULT_TRIGGER_COOLDOWN_MINUTES
        return timedelta(minutes=value)

    @staticmethod
    def _type_matches(aircraft_type: str, wanted: List[str]) -> bool:
        """Prefix match on the readable type, or exact ICAO designator match."""
        readable = (aircraft_type or "").strip().upper()
        if not readable:
            return False
        for value in wanted:
            if readable.startswith(value):
                return True
            name = AIRCRAFT_TYPE_NAMES.get(value)
            if name is not None and name.upper() == readable:
                return True
        return False

    def _match_trigger_rules(self, aircraft_list: List[Dict]) -> List[Dict]:
        """Aircraft (from *aircraft_list*) that satisfy every filled-in rule.

        Rules combine with AND; several values within one rule match any of
        them. The aircraft type is only looked up for aircraft passing every
        other rule, and at most MAX_TRIGGER_TYPE_LOOKUPS_PER_FETCH lookups
        that are not already memoised are started per call.
        """
        rules = self._trigger_rules()
        self._trigger_lookup_budget = MAX_TRIGGER_TYPE_LOOKUPS_PER_FETCH
        if not rules:
            return []

        candidates = []
        for aircraft in aircraft_list:
            if not aircraft.get("icao24"):
                continue
            call_signs = rules.get("call_signs")
            if call_signs:
                call_sign = str(aircraft.get("call_sign") or "").strip().upper()
                if not any(call_sign.startswith(prefix) for prefix in call_signs):
                    continue
            max_alt = rules.get("max_altitude_ft")
            if max_alt is not None and aircraft.get("altitude", 0) > max_alt:
                continue
            max_dist = rules.get("max_distance_km")
            if max_dist is not None and aircraft.get("distance_km", float("inf")) > max_dist:
                continue
            candidates.append(aircraft)

        wanted_types = rules.get("aircraft_types")
        if not wanted_types:
            return candidates

        matches = []
        for aircraft in candidates:
            aircraft_type = self._budgeted_type(aircraft["icao24"])
            if aircraft_type is None:
                continue  # budget spent; checked on a later fetch
            if self._type_matches(aircraft_type, wanted_types):
                matches.append(aircraft)
        return matches

    def _budgeted_type(self, icao24: str) -> Optional[str]:
        """Memoised type, or a new lookup while the per-fetch budget lasts (else None)."""
        key = (icao24 or "").strip().upper()
        if key and key not in self.__dict__.setdefault("_type_cache", {}):
            if self._trigger_lookup_budget <= 0:
                return None
            self._trigger_lookup_budget -= 1
        return self._lookup_aircraft_type(icao24)

    def _budgeted_route(self, call_sign: str) -> Dict[str, str]:
        """Memoised route, or a new lookup while the per-fetch budget lasts (else empty)."""
        key = (call_sign or "").strip().upper()
        if key and key not in self.__dict__.setdefault("_route_cache", {}):
            if self._trigger_lookup_budget <= 0:
                return {"origin": "", "destination": "", "flight_number": ""}
            self._trigger_lookup_budget -= 1
        return self._lookup_route(call_sign)

    def _budgeted_flight(self, icao24: str, call_sign: str) -> Dict[str, str]:
        """_lookup_flight within the per-fetch budget: route, type, then AirLabs for the gaps.

        Each uncached lookup spends one unit; once the budget is spent the
        remaining fields stay empty (memoised values are still used).
        """
        info = {**self._budgeted_route(call_sign), "aircraft_type": self._budgeted_type(icao24) or ""}
        if not self._flight_gaps(info):
            return info
        key = self._airlabs_cache_key(icao24, call_sign)
        if key is not None and key not in self.__dict__.setdefault("_airlabs_cache", {}):
            if self._trigger_lookup_budget <= 0:
                return info
            self._trigger_lookup_budget -= 1
        return self._fill_from_airlabs(info, icao24, call_sign)

    @staticmethod
    def _trigger_message(aircraft: Dict[str, Any]) -> str:
        """Plain message shown when no trigger page is chosen."""
        lines = [f"{aircraft.get('call_sign', '')} {aircraft.get('aircraft_type', '')}".strip()]
        origin, destination = aircraft.get("origin", ""), aircraft.get("destination", "")
        if origin and destination:
            lines.append(f"{origin}>{destination}")
        lines.append(f"{aircraft.get('altitude', 0)}FT {aircraft.get('distance_km', 0)}KM")
        return "\n".join(lines)

    def _build_trigger(self, aircraft: Dict[str, Any], display_data: Dict[str, Any]) -> TriggerResult:
        """TriggerResult for *aircraft*: the normal data, closest fields overridden."""
        enriched = dict(aircraft)
        enriched.update(self._budgeted_flight(enriched["icao24"], enriched.get("call_sign", "")))

        data = copy.deepcopy(display_data) if display_data else {}
        for key in ("call_sign", "flight_number", "aircraft_type", "origin",
                    "destination", "altitude", "ground_speed", "squawk", "distance_km"):
            data[key] = enriched.get(key, "")

        return TriggerResult(
            triggered=True,
            trigger_id=f"{TRIGGER_ID_PREFIX}{enriched['icao24']}",
            message=self._trigger_message(enriched),
            priority=TRIGGER_PRIORITY,
            duration_seconds=self._trigger_duration_seconds(),
            data=data,
        )

    def _update_trigger_state(self, in_radius: List[Dict], matches: List[Dict],
                              display_data: Dict[str, Any]) -> None:
        """Fold one fresh fetch into the trigger state.

        Only called after a fresh OpenSky response; the cache, 429 and error
        paths leave the trigger state untouched.
        """
        if not self._trigger_rules():
            return
        now = _utcnow()
        cooldown = self._trigger_cooldown()
        self._trigger_fired_at = {
            icao: fired for icao, fired in self._trigger_fired_at.items() if now - fired < cooldown
        }

        # Active/pending aircraft that no longer match (moved out of range,
        # climbed, left the radius or landed) lose their trigger now.
        matching = {str(a["icao24"]) for a in matches}
        for icao in sorted(self._trigger_active - matching):
            self._trigger_active.discard(icao)
            self._trigger_dismissals.append(f"{TRIGGER_ID_PREFIX}{icao}")
        for icao in list(self._trigger_pending):
            if icao not in matching:
                del self._trigger_pending[icao]
                self._trigger_fired_at.pop(icao, None)  # never shown: no cooldown

        for aircraft in matches:
            icao = str(aircraft["icao24"])
            if icao in self._trigger_fired_at or icao in self._trigger_pending:
                continue
            self._trigger_pending[icao] = self._build_trigger(aircraft, display_data)
            self._trigger_fired_at[icao] = now

    def check_triggers(self) -> List[TriggerResult]:
        """Return one trigger per newly matching aircraft, once per cooldown.

        Reads only the state left by the latest fresh fetch_data(); never
        calls OpenSky itself. Aircraft that no longer match since their
        trigger fired have that trigger dismissed early.
        """
        if self._trigger_dismissals:
            dismissals, self._trigger_dismissals = self._trigger_dismissals, []
            try:
                from src.triggers.service import get_trigger_service
                service = get_trigger_service()
                for trigger_id in dismissals:
                    service.dismiss_trigger(trigger_id)  # False = already expired; fine
            except Exception:
                logger.exception("Could not dismiss nearby aircraft triggers")

        if not self._trigger_rules():
            self._trigger_pending.clear()
            return []

        results = list(self._trigger_pending.values())
        self._trigger_active.update(self._trigger_pending)
        self._trigger_pending.clear()
        return results

    def get_formatted_display(self) -> Optional[List[str]]:
        """Return the plugin's own whole-board display, sized to the board.

        Documented plugin contract (no caller in core today, but conformance
        holds it to the same bounds as the live template-variable path --
        the exact same lines fetch_data now sets as PluginResult.formatted_lines,
        via the shared :meth:`_build_display_lines`). Always calls
        :meth:`fetch_data` -- which serves from its own age-based raw cache
        when fresh, so this rarely touches the network -- rather than
        reading ``self._cache`` directly, because only fetch_data derives
        "formatted"/"headers" for the board that is *currently* bound to
        ``self.board``. Reading a previously baked display straight out of
        the cache would replay whichever board's column widths and row
        count happened to be active on the call that populated it.
        """
        result = self.fetch_data()
        if not result.available or not result.data:
            return None
        return result.formatted_lines or self._build_display_lines(result.data)


# Export the plugin class
Plugin = NearbyAircraftPlugin
