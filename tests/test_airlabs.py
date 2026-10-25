"""Tests for the optional AirLabs flight number lookup."""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest

from plugins.nearby_aircraft import (
    AIRLABS_BACKOFF,
    AIRLABS_FLIGHTS_URL,
    MAX_TRIGGER_TYPE_LOOKUPS_PER_FETCH,
    NearbyAircraftPlugin,
    OPENSKY_STATES_URL,
)

KEY = "YOUR_AIRLABS_KEY"
START = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def plugin(sample_manifest, sample_config):
    plugin = NearbyAircraftPlugin(sample_manifest)
    plugin.config = {**sample_config, "airlabs_api_key": KEY}
    return plugin


def _response(status=200, body=None):
    resp = Mock(status_code=status)
    resp.json.return_value = body
    return resp


def _airlabs(flight_iata="SN2903", hex_="ABC123"):
    return _response(body={"response": [{"hex": hex_, "flight_icao": "BEL5EN", "flight_iata": flight_iata}]})


def _adsbdb_route(iata_prefix="SN", icao_prefix="BEL"):
    return _response(body={"response": {"flightroute": {
        "airline": {"icao": icao_prefix, "iata": iata_prefix},
        "origin": {"iata_code": "BRU"},
        "destination": {"iata_code": "LIS"},
    }}})


def _airlabs_calls(mock_get):
    return [c for c in mock_get.call_args_list if c.args and c.args[0] == AIRLABS_FLIGHTS_URL]


class TestLookup:
    def test_no_key_never_calls_airlabs(self, plugin):
        plugin.config["airlabs_api_key"] = "  "
        with patch("plugins.nearby_aircraft.requests.get") as mock_get:
            assert plugin._lookup_airlabs_flight_number("abc123", "BEL5EN") == ""
        mock_get.assert_not_called()

    def test_queries_by_hex_with_key(self, plugin):
        with patch("plugins.nearby_aircraft.requests.get", return_value=_airlabs()) as mock_get:
            assert plugin._lookup_airlabs_flight_number("abc123", "BEL5EN") == "SN2903"
        params = mock_get.call_args.kwargs["params"]
        assert params["api_key"] == KEY
        assert params["hex"] == "ABC123"

    def test_bare_list_response(self, plugin):
        body = [{"hex": "ABC123", "flight_iata": "sn2903"}]
        with patch("plugins.nearby_aircraft.requests.get", return_value=_response(body=body)):
            assert plugin._lookup_airlabs_flight_number("abc123", "BEL5EN") == "SN2903"

    def test_other_hex_in_response_is_ignored(self, plugin):
        with patch("plugins.nearby_aircraft.requests.get", return_value=_airlabs(hex_="FFFFFF")):
            assert plugin._lookup_airlabs_flight_number("abc123", "BEL5EN") == ""

    def test_too_long_flight_number_is_ignored(self, plugin):
        with patch("plugins.nearby_aircraft.requests.get", return_value=_airlabs("SN290312")):
            assert plugin._lookup_airlabs_flight_number("abc123", "BEL5EN") == ""

    def test_memoised_per_icao24_and_call_sign(self, plugin):
        with patch("plugins.nearby_aircraft.requests.get", return_value=_airlabs()) as mock_get:
            plugin._lookup_airlabs_flight_number("abc123", "BEL5EN")
            plugin._lookup_airlabs_flight_number("ABC123", "bel5en")
            assert mock_get.call_count == 1
            plugin._lookup_airlabs_flight_number("abc123", "BEL7XY")  # next leg
            assert mock_get.call_count == 2

    def test_miss_is_memoised(self, plugin):
        with patch("plugins.nearby_aircraft.requests.get",
                   return_value=_response(body={"response": []})) as mock_get:
            assert plugin._lookup_airlabs_flight_number("abc123", "BEL5EN") == ""
            assert plugin._lookup_airlabs_flight_number("abc123", "BEL5EN") == ""
        assert mock_get.call_count == 1

    @pytest.mark.parametrize("resp", [
        _response(body={"error": {"message": "Unknown api_key", "code": "unknown_api_key"}}),
        _response(status=500),
    ])
    def test_error_backs_off_then_retries(self, plugin, resp):
        with patch("plugins.nearby_aircraft._utcnow", return_value=START), \
                patch("plugins.nearby_aircraft.requests.get", return_value=resp) as mock_get:
            assert plugin._lookup_airlabs_flight_number("abc123", "BEL5EN") == ""
            assert plugin._lookup_airlabs_flight_number("def456", "KLM12") == ""
            assert mock_get.call_count == 1
        later = START + AIRLABS_BACKOFF
        with patch("plugins.nearby_aircraft._utcnow", return_value=later), \
                patch("plugins.nearby_aircraft.requests.get", return_value=_airlabs()):
            assert plugin._lookup_airlabs_flight_number("abc123", "BEL5EN") == "SN2903"

    def test_network_error_returns_empty(self, plugin):
        with patch("plugins.nearby_aircraft.requests.get", side_effect=Exception("boom")):
            assert plugin._lookup_airlabs_flight_number("abc123", "BEL5EN") == ""

    def test_config_change_clears_backoff_and_cache_on_new_key(self, plugin):
        plugin._airlabs_backoff_until = START + timedelta(hours=1)
        plugin._airlabs_cache = {("ABC123", "BEL5EN"): ""}
        old = dict(plugin.config)
        plugin.on_config_change(old, {**old, "airlabs_api_key": "YOUR_OTHER_KEY"})
        assert plugin._airlabs_backoff_until is None
        assert plugin._airlabs_cache == {}


class TestLookupFlight:
    def _get(self, airlabs):
        def get(url, *args, **kwargs):
            if url == AIRLABS_FLIGHTS_URL:
                return airlabs
            return _adsbdb_route()
        return get

    def test_airlabs_overrides_derived_number(self, plugin):
        with patch("plugins.nearby_aircraft.requests.get", side_effect=self._get(_airlabs("SN2903"))):
            info = plugin._lookup_flight("abc123", "BEL123")
        assert info["flight_number"] == "SN2903"
        assert (info["origin"], info["destination"]) == ("BRU", "LIS")

    def test_falls_back_to_derived_number(self, plugin):
        miss = _response(body={"response": []})
        with patch("plugins.nearby_aircraft.requests.get", side_effect=self._get(miss)):
            assert plugin._lookup_flight("abc123", "BEL123")["flight_number"] == "SN123"

    def test_route_cache_is_not_modified(self, plugin):
        with patch("plugins.nearby_aircraft.requests.get", side_effect=self._get(_airlabs("SN2903"))):
            plugin._lookup_flight("abc123", "BEL123")
        assert plugin._route_cache["BEL123"]["flight_number"] == "SN123"


class TestFetchData:
    def test_displayed_aircraft_get_airlabs_number(self, plugin, mock_opensky_states_response):
        def get(url, *args, **kwargs):
            if url == OPENSKY_STATES_URL:
                return _response(body=mock_opensky_states_response)
            if url == AIRLABS_FLIGHTS_URL:
                hex_ = kwargs["params"]["hex"]
                return _response(body={"response": [{"hex": hex_, "flight_iata": "XX" + hex_[:3]}]})
            return _response(status=404)

        with patch("plugins.nearby_aircraft.requests.get", side_effect=get):
            result = plugin.fetch_data()
        assert result.available
        closest = result.data["aircraft"][0]
        assert result.data["flight_number"] == "XX" + closest["icao24"].upper()[:3]


class TestTriggerBudget:
    def test_trigger_enrichment_respects_budget(self, plugin):
        plugin._trigger_lookup_budget = 0
        with patch("plugins.nearby_aircraft.requests.get") as mock_get:
            info = plugin._budgeted_flight("abc123", "BEL5EN")
        mock_get.assert_not_called()
        assert info["flight_number"] == ""

    def test_trigger_enrichment_spends_budget(self, plugin):
        plugin._trigger_lookup_budget = MAX_TRIGGER_TYPE_LOOKUPS_PER_FETCH
        with patch("plugins.nearby_aircraft.requests.get",
                   side_effect=lambda url, *a, **k: _airlabs() if url == AIRLABS_FLIGHTS_URL
                   else _response(status=404)):
            info = plugin._budgeted_flight("abc123", "BEL5EN")
        assert info["flight_number"] == "SN2903"
        assert plugin._trigger_lookup_budget == MAX_TRIGGER_TYPE_LOOKUPS_PER_FETCH - 2
