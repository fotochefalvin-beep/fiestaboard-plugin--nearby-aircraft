"""Tests for the optional AirLabs backup (flight number and aircraft type)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest

from plugins.nearby_aircraft import (
    AIRCRAFT_LOOKUP_URL,
    AIRLABS_BACKOFF,
    AIRLABS_FLIGHTS_URL,
    MAX_TRIGGER_TYPE_LOOKUPS_PER_FETCH,
    NearbyAircraftPlugin,
    OPENSKY_STATES_URL,
    ROUTE_LOOKUP_URL,
)

KEY = "YOUR_AIRLABS_KEY"
START = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
MISS = object()  # adsbdb answers 404


@pytest.fixture
def plugin(sample_manifest, sample_config):
    plugin = NearbyAircraftPlugin(sample_manifest)
    plugin.config = {**sample_config, "airlabs_api_key": KEY}
    return plugin


def _response(status=200, body=None):
    resp = Mock(status_code=status)
    resp.json.return_value = body
    return resp


def _airlabs(flight_iata="SN2903", hex_="ABC123", aircraft_icao="A20N"):
    return _response(body={"response": [
        {"hex": hex_, "flight_iata": flight_iata, "aircraft_icao": aircraft_icao},
    ]})


def _adsbdb_route(iata_prefix="SN", icao_prefix="BEL"):
    return _response(body={"response": {"flightroute": {
        "airline": {"icao": icao_prefix, "iata": iata_prefix},
        "origin": {"iata_code": "BRU"},
        "destination": {"iata_code": "LIS"},
    }}})


def _adsbdb_type(icao_type):
    return _response(body={"response": {"aircraft": {"icao_type": icao_type}}})


def _world(route=MISS, aircraft_type=MISS, airlabs=None):
    """requests.get side effect: adsbdb route/type and AirLabs by URL.

    *route* / *aircraft_type* are responses (or MISS for a 404, or an
    exception to raise); *airlabs* is the AirLabs response.
    """
    def pick(value):
        if value is MISS:
            return _response(status=404)
        if isinstance(value, Exception):
            raise value
        return value

    def get(url, *args, **kwargs):
        if url == AIRLABS_FLIGHTS_URL:
            return pick(airlabs if airlabs is not None else _response(body={"response": []}))
        if url.startswith(ROUTE_LOOKUP_URL):
            return pick(route)
        if url.startswith(AIRCRAFT_LOOKUP_URL):
            return pick(aircraft_type)
        return _response(status=404)
    return get


def _airlabs_calls(mock_get):
    return [c for c in mock_get.call_args_list if c.args and c.args[0] == AIRLABS_FLIGHTS_URL]


EMPTY = {"flight_number": "", "aircraft_type": ""}


class TestLookup:
    def test_no_key_never_calls_airlabs(self, plugin):
        plugin.config["airlabs_api_key"] = "  "
        with patch("plugins.nearby_aircraft.requests.get") as mock_get:
            assert plugin._lookup_airlabs("abc123", "BEL5EN") == EMPTY
        mock_get.assert_not_called()

    def test_queries_by_hex_with_key(self, plugin):
        with patch("plugins.nearby_aircraft.requests.get", return_value=_airlabs()) as mock_get:
            info = plugin._lookup_airlabs("abc123", "BEL5EN")
        assert info == {"flight_number": "SN2903", "aircraft_type": "A320neo"}
        params = mock_get.call_args.kwargs["params"]
        assert params["api_key"] == KEY
        assert params["hex"] == "ABC123"
        assert set(params["_fields"].split(",")) >= {"hex", "flight_iata", "aircraft_icao"}

    def test_bare_list_response(self, plugin):
        body = [{"hex": "ABC123", "flight_iata": "sn2903", "aircraft_icao": "a20n"}]
        with patch("plugins.nearby_aircraft.requests.get", return_value=_response(body=body)):
            assert plugin._lookup_airlabs("abc123", "BEL5EN") == {
                "flight_number": "SN2903", "aircraft_type": "A320neo"}

    def test_unmapped_type_shows_designator(self, plugin):
        with patch("plugins.nearby_aircraft.requests.get", return_value=_airlabs(aircraft_icao="XYZ1")):
            assert plugin._lookup_airlabs("abc123", "BEL5EN")["aircraft_type"] == "XYZ1"

    def test_too_long_type_is_ignored(self, plugin):
        with patch("plugins.nearby_aircraft.requests.get",
                   return_value=_airlabs(aircraft_icao="ABCDEFGHIJKL")):
            assert plugin._lookup_airlabs("abc123", "BEL5EN")["aircraft_type"] == ""

    def test_other_hex_in_response_is_ignored(self, plugin):
        with patch("plugins.nearby_aircraft.requests.get", return_value=_airlabs(hex_="FFFFFF")):
            assert plugin._lookup_airlabs("abc123", "BEL5EN") == EMPTY

    def test_too_long_flight_number_is_ignored(self, plugin):
        with patch("plugins.nearby_aircraft.requests.get", return_value=_airlabs("SN290312")):
            info = plugin._lookup_airlabs("abc123", "BEL5EN")
        assert info["flight_number"] == ""
        assert info["aircraft_type"] == "A320neo"

    def test_memoised_per_icao24_and_call_sign(self, plugin):
        with patch("plugins.nearby_aircraft.requests.get", return_value=_airlabs()) as mock_get:
            plugin._lookup_airlabs("abc123", "BEL5EN")
            assert plugin._lookup_airlabs("ABC123", "bel5en") == {
                "flight_number": "SN2903", "aircraft_type": "A320neo"}
            assert mock_get.call_count == 1
            plugin._lookup_airlabs("abc123", "BEL7XY")  # next leg
            assert mock_get.call_count == 2
        assert plugin._airlabs_cache[("ABC123", "BEL5EN")] == {
            "flight_number": "SN2903", "aircraft_type": "A320neo"}

    def test_miss_is_memoised(self, plugin):
        with patch("plugins.nearby_aircraft.requests.get",
                   return_value=_response(body={"response": []})) as mock_get:
            assert plugin._lookup_airlabs("abc123", "BEL5EN") == EMPTY
            assert plugin._lookup_airlabs("abc123", "BEL5EN") == EMPTY
        assert mock_get.call_count == 1

    @pytest.mark.parametrize("resp", [
        _response(body={"error": {"message": "Unknown api_key", "code": "unknown_api_key"}}),
        _response(status=500),
    ])
    def test_error_backs_off_then_retries(self, plugin, resp):
        with patch("plugins.nearby_aircraft._utcnow", return_value=START), \
                patch("plugins.nearby_aircraft.requests.get", return_value=resp) as mock_get:
            assert plugin._lookup_airlabs("abc123", "BEL5EN") == EMPTY
            assert plugin._lookup_airlabs("def456", "KLM12") == EMPTY
            assert mock_get.call_count == 1
        assert plugin._airlabs_cache == {}  # errors are not memoised
        later = START + AIRLABS_BACKOFF
        with patch("plugins.nearby_aircraft._utcnow", return_value=later), \
                patch("plugins.nearby_aircraft.requests.get", return_value=_airlabs()):
            assert plugin._lookup_airlabs("abc123", "BEL5EN")["flight_number"] == "SN2903"

    def test_network_error_returns_empty(self, plugin):
        with patch("plugins.nearby_aircraft.requests.get", side_effect=Exception("boom")):
            assert plugin._lookup_airlabs("abc123", "BEL5EN") == EMPTY
        assert plugin._airlabs_cache == {}

    def test_config_change_clears_backoff_and_cache_on_new_key(self, plugin):
        plugin._airlabs_backoff_until = START + timedelta(hours=1)
        plugin._airlabs_cache = {("ABC123", "BEL5EN"): dict(EMPTY)}
        old = dict(plugin.config)
        plugin.on_config_change(old, {**old, "airlabs_api_key": "YOUR_OTHER_KEY"})
        assert plugin._airlabs_backoff_until is None
        assert plugin._airlabs_cache == {}


class TestLookupFlight:
    """The I/O matrix: adsbdb first, AirLabs only for the gaps."""

    def _lookup(self, plugin, call_sign, world):
        with patch("plugins.nearby_aircraft.requests.get", side_effect=world) as mock_get:
            info = plugin._lookup_flight("abc123", call_sign)
        return info, len(_airlabs_calls(mock_get))

    def test_both_known_no_airlabs_request(self, plugin):
        world = _world(_adsbdb_route("BA", "BAW"), _adsbdb_type("B738"), _airlabs("BA999"))
        info, calls = self._lookup(plugin, "BAW716", world)
        assert (info["flight_number"], info["aircraft_type"]) == ("BA716", "737-800")
        assert calls == 0

    def test_derived_number_wins_no_airlabs_call(self, plugin):
        world = _world(_adsbdb_route(), _adsbdb_type("A320"), _airlabs("SN2903"))
        info, calls = self._lookup(plugin, "BEL123", world)
        assert info["flight_number"] == "SN123"
        assert (info["origin"], info["destination"]) == ("BRU", "LIS")
        assert calls == 0

    def test_number_missing(self, plugin):
        world = _world(_adsbdb_route("VY", "VLG"), _adsbdb_type("A320"),
                       _airlabs("VY1080", aircraft_icao="A20N"))
        info, calls = self._lookup(plugin, "VLG8UE", world)
        # adsbdb's type is kept even though AirLabs has another one.
        assert (info["flight_number"], info["aircraft_type"]) == ("VY1080", "A320")
        assert (info["origin"], info["destination"]) == ("BRU", "LIS")  # route stays adsbdb's
        assert calls == 1

    def test_type_missing(self, plugin):
        world = _world(_adsbdb_route(), MISS, _airlabs("SN999", aircraft_icao="A20N"))
        info, calls = self._lookup(plugin, "BEL123", world)
        assert (info["flight_number"], info["aircraft_type"]) == ("SN123", "A320neo")
        assert calls == 1

    @pytest.mark.parametrize("aircraft_type", [_response(status=500), TimeoutError("slow")])
    def test_type_error_or_timeout_counts_as_missing(self, plugin, aircraft_type):
        world = _world(_adsbdb_route(), aircraft_type, _airlabs("SN999", aircraft_icao="A20N"))
        info, calls = self._lookup(plugin, "BEL123", world)
        assert (info["flight_number"], info["aircraft_type"]) == ("SN123", "A320neo")
        assert calls == 1

    @pytest.mark.parametrize("route", [MISS, _response(status=500), TimeoutError("slow")])
    def test_route_404_error_or_timeout_counts_as_missing(self, plugin, route):
        world = _world(route, _adsbdb_type("A320"), _airlabs("SN2903"))
        info, calls = self._lookup(plugin, "BEL123", world)
        assert (info["flight_number"], info["aircraft_type"]) == ("SN2903", "A320")
        assert calls == 1

    def test_both_missing_one_request(self, plugin):
        world = _world(_adsbdb_route(), MISS, _airlabs("SN2903", aircraft_icao="A20N"))
        with patch("plugins.nearby_aircraft.requests.get", side_effect=world) as mock_get:
            info = plugin._lookup_flight("abc123", "BEL5EN")
            again = plugin._lookup_flight("abc123", "BEL5EN")
        assert (info["flight_number"], info["aircraft_type"]) == ("SN2903", "A320neo")
        assert again == info
        assert len(_airlabs_calls(mock_get)) == 1  # one request; both fields memoised

    def test_airlabs_miss_is_memoised(self, plugin):
        world = _world(_adsbdb_route(), MISS, _response(body={"response": []}))
        with patch("plugins.nearby_aircraft.requests.get", side_effect=world) as mock_get:
            info = plugin._lookup_flight("abc123", "BEL5EN")
            plugin._lookup_flight("abc123", "BEL5EN")
        assert (info["flight_number"], info["aircraft_type"]) == ("", "")
        assert len(_airlabs_calls(mock_get)) == 1

    def test_airlabs_error_leaves_gaps_and_pauses(self, plugin):
        world = _world(_adsbdb_route(), MISS, _response(status=500))
        with patch("plugins.nearby_aircraft._utcnow", return_value=START), \
                patch("plugins.nearby_aircraft.requests.get", side_effect=world) as mock_get:
            info = plugin._lookup_flight("abc123", "BEL5EN")
            plugin._lookup_flight("def456", "BEL7XY")
        assert (info["flight_number"], info["aircraft_type"]) == ("", "")
        assert len(_airlabs_calls(mock_get)) == 1
        assert plugin._airlabs_backoff_until == START + AIRLABS_BACKOFF

    def test_unmapped_type(self, plugin):
        world = _world(_adsbdb_route(), MISS, _airlabs("SN123", aircraft_icao="XYZ1"))
        info, _ = self._lookup(plugin, "BEL123", world)
        assert info["aircraft_type"] == "XYZ1"

    def test_no_key_keeps_adsbdb_values(self, plugin):
        plugin.config.pop("airlabs_api_key")
        world = _world(_adsbdb_route(), MISS, _airlabs())
        info, calls = self._lookup(plugin, "BEL5EN", world)
        assert (info["flight_number"], info["aircraft_type"]) == ("", "")
        assert calls == 0

    def test_adsbdb_caches_are_not_modified(self, plugin):
        world = _world(_adsbdb_route(), MISS, _airlabs("SN2903"))
        with patch("plugins.nearby_aircraft.requests.get", side_effect=world):
            plugin._lookup_flight("abc123", "BEL5EN")
        assert plugin._route_cache["BEL5EN"]["flight_number"] == ""
        assert plugin._type_cache["ABC123"] == ""


class TestFetchData:
    @staticmethod
    def _get(states, adsbdb_covers):
        def get(url, *args, **kwargs):
            if url == OPENSKY_STATES_URL:
                return _response(body=states)
            if url == AIRLABS_FLIGHTS_URL:
                hex_ = kwargs["params"]["hex"]
                return _response(body={"response": [
                    {"hex": hex_, "flight_iata": "XX" + hex_[:3], "aircraft_icao": "A20N"},
                ]})
            if adsbdb_covers and url.startswith(ROUTE_LOOKUP_URL):
                return _adsbdb_route("AB", "ABC")
            if adsbdb_covers and url.startswith(AIRCRAFT_LOOKUP_URL):
                return _adsbdb_type("B738")
            return _response(status=404)
        return get

    def test_displayed_aircraft_get_airlabs_backup(self, plugin, mock_opensky_states_response):
        with patch("plugins.nearby_aircraft.requests.get",
                   side_effect=self._get(mock_opensky_states_response, False)):
            result = plugin.fetch_data()
        assert result.available
        closest = result.data["aircraft"][0]
        assert result.data["flight_number"] == "XX" + closest["icao24"].upper()[:3]
        assert result.data["aircraft_type"] == "A320neo"

    def test_no_airlabs_request_when_adsbdb_covers(self, plugin, mock_opensky_states_response):
        states = dict(mock_opensky_states_response)
        states["states"] = [list(s) for s in mock_opensky_states_response["states"]]
        for i, state in enumerate(states["states"]):
            state[1] = f"ABC{100 + i}"  # numeric call signs adsbdb can derive
        with patch("plugins.nearby_aircraft.requests.get",
                   side_effect=self._get(states, True)) as mock_get:
            result = plugin.fetch_data()
        assert result.available and result.data["aircraft"]
        assert _airlabs_calls(mock_get) == []
        assert all(a["flight_number"].startswith("AB") and a["aircraft_type"] == "737-800"
                   for a in result.data["aircraft"])


class TestTriggerBudget:
    def test_budget_spent_no_lookup_and_gaps_stay_empty(self, plugin):
        plugin._trigger_lookup_budget = 0
        aircraft = {"icao24": "abc123", "call_sign": "BEL5EN", "altitude": 3000, "distance_km": 2.0}
        with patch("plugins.nearby_aircraft.requests.get") as mock_get:
            trigger = plugin._build_trigger(aircraft, {})
        mock_get.assert_not_called()
        assert (trigger.data["flight_number"], trigger.data["aircraft_type"]) == ("", "")

    def test_budget_spent_still_uses_memoised_airlabs(self, plugin):
        plugin._trigger_lookup_budget = 0
        plugin._route_cache = {"BEL5EN": {"origin": "", "destination": "", "flight_number": ""}}
        plugin._airlabs_cache = {("ABC123", "BEL5EN"): {"flight_number": "SN2903", "aircraft_type": "A320neo"}}
        with patch("plugins.nearby_aircraft.requests.get") as mock_get:
            info = plugin._budgeted_flight("abc123", "BEL5EN")
        mock_get.assert_not_called()
        assert (info["flight_number"], info["aircraft_type"]) == ("SN2903", "A320neo")

    def test_spends_route_type_and_airlabs(self, plugin):
        plugin._trigger_lookup_budget = MAX_TRIGGER_TYPE_LOOKUPS_PER_FETCH
        with patch("plugins.nearby_aircraft.requests.get",
                   side_effect=_world(MISS, MISS, _airlabs())) as mock_get:
            info = plugin._budgeted_flight("abc123", "BEL5EN")
        assert (info["flight_number"], info["aircraft_type"]) == ("SN2903", "A320neo")
        assert len(_airlabs_calls(mock_get)) == 1
        assert plugin._trigger_lookup_budget == MAX_TRIGGER_TYPE_LOOKUPS_PER_FETCH - 3

    def test_no_airlabs_spend_when_adsbdb_covers(self, plugin):
        plugin._trigger_lookup_budget = MAX_TRIGGER_TYPE_LOOKUPS_PER_FETCH
        with patch("plugins.nearby_aircraft.requests.get",
                   side_effect=_world(_adsbdb_route(), _adsbdb_type("A320"), _airlabs())) as mock_get:
            info = plugin._budgeted_flight("abc123", "BEL123")
        assert (info["flight_number"], info["aircraft_type"]) == ("SN123", "A320")
        assert _airlabs_calls(mock_get) == []
        assert plugin._trigger_lookup_budget == MAX_TRIGGER_TYPE_LOOKUPS_PER_FETCH - 2

    def test_budget_runs_out_before_airlabs(self, plugin):
        plugin._trigger_lookup_budget = 2
        with patch("plugins.nearby_aircraft.requests.get",
                   side_effect=_world(MISS, MISS, _airlabs())) as mock_get:
            info = plugin._budgeted_flight("abc123", "BEL5EN")
        assert (info["flight_number"], info["aircraft_type"]) == ("", "")
        assert _airlabs_calls(mock_get) == []
        assert plugin._trigger_lookup_budget == 0

    def test_build_trigger_fills_gap_within_budget(self, plugin):
        plugin._trigger_lookup_budget = MAX_TRIGGER_TYPE_LOOKUPS_PER_FETCH
        aircraft = {"icao24": "abc123", "call_sign": "BEL123", "altitude": 3000, "distance_km": 2.0}
        with patch("plugins.nearby_aircraft.requests.get",
                   side_effect=_world(_adsbdb_route(), MISS, _airlabs("SN999", aircraft_icao="A20N"))):
            trigger = plugin._build_trigger(aircraft, {})
        assert trigger.data["flight_number"] == "SN123"
        assert trigger.data["aircraft_type"] == "A320neo"
        assert "A320neo" in trigger.message


class TestTriggerTypeRule:
    def test_type_rule_does_not_use_airlabs(self, plugin):
        plugin.config["trigger_aircraft_types"] = "A20N"
        aircraft = {"icao24": "abc123", "call_sign": "BEL5EN", "altitude": 3000, "distance_km": 2.0}
        with patch("plugins.nearby_aircraft.requests.get",
                   side_effect=_world(_adsbdb_route(), MISS, _airlabs(aircraft_icao="A20N"))) as mock_get:
            matches = plugin._match_trigger_rules([aircraft])
        assert matches == []
        assert _airlabs_calls(mock_get) == []
