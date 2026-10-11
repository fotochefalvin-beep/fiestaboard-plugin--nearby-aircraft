"""Tests for the Nearby Aircraft trigger feature (watch rules -> trigger page)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, Mock, patch

import pytest
import requests

import plugins.nearby_aircraft as na
from plugins.nearby_aircraft import NearbyAircraftPlugin
from src.plugins.base import TriggerResult
from src.triggers.priority import TriggerPriority

CENTER_LAT, CENTER_LON = 52.0, 4.0


def state(icao24, callsign, km_north=1.0, alt_m=1000.0, on_ground=False):
    """OpenSky state vector *km_north* km north of the center."""
    lat = CENTER_LAT + km_north / 111.0
    return [icao24, callsign, "NL", 0, 0, CENTER_LON, lat, alt_m, on_ground,
            100.0, 0.0, 0.0, None, alt_m, "1000", False, 0]


class FakeApi:
    """Dispatches requests.get to fake OpenSky / adsbdb answers."""

    def __init__(self, states=None, routes=None, types=None):
        self.states = states or []
        self.routes = routes or {}
        self.types = types or {}
        self.opensky_status = 200
        self.opensky_error = None
        self.type_lookups = []
        self.route_lookups = []

    def __call__(self, url, params=None, headers=None, timeout=None):
        resp = Mock()
        if url == na.OPENSKY_STATES_URL:
            if self.opensky_error:
                raise self.opensky_error
            resp.status_code = self.opensky_status
            resp.json.return_value = {"time": 0, "states": self.states}
            return resp
        key = url.rsplit("/", 1)[-1]
        if url.startswith(na.AIRCRAFT_LOOKUP_URL):
            self.type_lookups.append(key)
            if key in self.types:
                resp.status_code = 200
                resp.json.return_value = {"response": {"aircraft": {"icao_type": self.types[key]}}}
            else:
                resp.status_code = 404
            return resp
        if url.startswith(na.ROUTE_LOOKUP_URL):
            self.route_lookups.append(key)
            if key in self.routes:
                origin, destination = self.routes[key]
                resp.status_code = 200
                resp.json.return_value = {"response": {"flightroute": {
                    "origin": {"iata_code": origin},
                    "destination": {"iata_code": destination},
                    "airline": {"icao": "KLM", "iata": "KL"},
                }}}
            else:
                resp.status_code = 404
            return resp
        raise AssertionError(f"unexpected URL {url}")


@pytest.fixture
def api():
    fake = FakeApi()
    with patch("plugins.nearby_aircraft.requests.get", side_effect=fake):
        yield fake


@pytest.fixture
def service():
    svc = MagicMock()
    svc.dismiss_trigger.return_value = False
    with patch("src.triggers.service.get_trigger_service", return_value=svc):
        yield svc


def make_plugin(sample_manifest, **settings):
    plugin = NearbyAircraftPlugin(sample_manifest)
    plugin.config = {
        "enabled": True,
        "latitude": CENTER_LAT,
        "longitude": CENTER_LON,
        "radius_km": 50,
        "max_aircraft": 4,
        "refresh_seconds": 120,
        **settings,
    }
    return plugin


def fresh_fetch(plugin):
    """fetch_data() bypassing the age cache, so it always hits (fake) OpenSky."""
    plugin._cache = None
    return plugin.fetch_data()


# ── I/O matrix ────────────────────────────────────────────────────────────


class TestNoRules:
    def test_no_rules_never_fires(self, sample_manifest, api, service):
        api.states = [state("484a01", "KLM1234")]
        plugin = make_plugin(sample_manifest)
        result = fresh_fetch(plugin)
        assert result.available and result.data["call_sign"] == "KLM1234"
        assert plugin.check_triggers() == []
        assert plugin._trigger_fired_at == {}

    def test_blank_rules_count_as_unset(self, sample_manifest, api, service):
        api.states = [state("484a01", "KLM1234")]
        plugin = make_plugin(sample_manifest, trigger_call_signs=" , ", trigger_aircraft_types="",
                             trigger_max_altitude_ft=None, trigger_max_distance_km="")
        fresh_fetch(plugin)
        assert plugin.check_triggers() == []

    def test_no_rules_no_extra_lookups(self, sample_manifest, api, service):
        api.states = [state(f"a{i:05d}", f"KLM{i}", km_north=i + 1) for i in range(8)]
        plugin = make_plugin(sample_manifest, max_aircraft=2)
        fresh_fetch(plugin)
        assert len(api.type_lookups) == 2  # only the displayed aircraft


class TestNewMatch:
    def test_prefix_rule_fires_one_trigger(self, sample_manifest, api, service):
        api.states = [state("aaaaaa", "BAW1", km_north=1.0),
                      state("484a01", "KLM1234", km_north=3.0, alt_m=1524.0)]
        api.routes = {"KLM1234": ("AMS", "JFK")}
        api.types = {"484A01": "B77W"}
        plugin = make_plugin(sample_manifest, trigger_call_signs="klm, DLH")
        fresh_fetch(plugin)

        results = plugin.check_triggers()
        assert len(results) == 1
        trig = results[0]
        assert isinstance(trig, TriggerResult)
        assert trig.triggered is True
        assert trig.trigger_id == "aircraft_484a01"
        assert trig.priority == TriggerPriority.NOTABLE
        assert trig.duration_seconds == 60
        assert trig.message == "KLM1234 777-300ER\nAMS>JFK\n5000FT 3.0KM"

        data = trig.data
        # Closest-aircraft fields carry the triggering aircraft.
        assert data["call_sign"] == "KLM1234"
        assert data["flight_number"] == "KL1234"
        assert data["aircraft_type"] == "777-300ER"
        assert (data["origin"], data["destination"]) == ("AMS", "JFK")
        assert data["altitude"] == 5000
        assert data["ground_speed"] == 194
        assert data["squawk"] == "1000"
        assert data["distance_km"] == 3.0
        # The rest is the normal display data.
        assert data["aircraft_count"] == 2
        assert [a["call_sign"] for a in data["aircraft"]] == ["BAW1", "KLM1234"]
        assert data["formatted"].startswith("BAW1")
        assert data["headers"] and data["last_updated"]

    def test_trigger_data_independent_of_display_data(self, sample_manifest, api, service):
        api.states = [state("484a01", "KLM1234")]
        plugin = make_plugin(sample_manifest, trigger_call_signs="KLM")
        result = fresh_fetch(plugin)
        trig = plugin.check_triggers()[0]
        trig.data["aircraft"][0]["call_sign"] = "CHANGED"
        assert result.data["aircraft"][0]["call_sign"] == "KLM1234"

    def test_custom_duration(self, sample_manifest, api, service):
        api.states = [state("484a01", "KLM1234")]
        plugin = make_plugin(sample_manifest, trigger_call_signs="KLM", trigger_duration_seconds=15)
        fresh_fetch(plugin)
        assert plugin.check_triggers()[0].duration_seconds == 15

    def test_message_without_route(self, sample_manifest, api, service):
        api.states = [state("484a01", "KLM1234", km_north=2.0, alt_m=304.8)]
        plugin = make_plugin(sample_manifest, trigger_call_signs="KLM")
        fresh_fetch(plugin)
        assert plugin.check_triggers()[0].message == "KLM1234\n1000FT 2.0KM"

    def test_rules_combine_with_and(self, sample_manifest, api, service):
        api.states = [
            state("000001", "KLM1", km_north=1.0, alt_m=3000.0),  # KLM but too high
            state("000002", "BAW2", km_north=2.0, alt_m=500.0),   # low but not KLM
            state("000003", "KLM3", km_north=3.0, alt_m=500.0),   # both hold
            state("000004", "KLM4", km_north=30.0, alt_m=500.0),  # too far
        ]
        plugin = make_plugin(sample_manifest, trigger_call_signs="KLM",
                             trigger_max_altitude_ft=5000, trigger_max_distance_km=10)
        fresh_fetch(plugin)
        assert [t.trigger_id for t in plugin.check_triggers()] == ["aircraft_000003"]

    def test_single_numeric_rule(self, sample_manifest, api, service):
        api.states = [state("000001", "X1", km_north=1.0), state("000002", "X2", km_north=20.0)]
        plugin = make_plugin(sample_manifest, trigger_max_distance_km=5)
        fresh_fetch(plugin)
        assert [t.trigger_id for t in plugin.check_triggers()] == ["aircraft_000001"]

    def test_triggers_only_on_check_after_fetch(self, sample_manifest, api, service):
        plugin = make_plugin(sample_manifest, trigger_call_signs="KLM")
        assert plugin.check_triggers() == []


class TestRepeatAndCooldown:
    def test_repeat_pass_not_returned_again(self, sample_manifest, api, service):
        api.states = [state("484a01", "KLM1234")]
        plugin = make_plugin(sample_manifest, trigger_call_signs="KLM")
        fresh_fetch(plugin)
        assert len(plugin.check_triggers()) == 1
        assert plugin.check_triggers() == []  # engine pass without a fetch
        fresh_fetch(plugin)
        assert plugin.check_triggers() == []  # still matching, inside cooldown
        service.dismiss_trigger.assert_not_called()

    def test_fires_again_after_cooldown(self, sample_manifest, api, service):
        api.states = [state("484a01", "KLM1234")]
        plugin = make_plugin(sample_manifest, trigger_call_signs="KLM", trigger_cooldown_minutes=30)
        start = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
        with patch("plugins.nearby_aircraft._utcnow", return_value=start):
            fresh_fetch(plugin)
            assert len(plugin.check_triggers()) == 1
        with patch("plugins.nearby_aircraft._utcnow", return_value=start + timedelta(minutes=29)):
            fresh_fetch(plugin)
            assert plugin.check_triggers() == []
        with patch("plugins.nearby_aircraft._utcnow", return_value=start + timedelta(minutes=30)):
            fresh_fetch(plugin)
            assert [t.trigger_id for t in plugin.check_triggers()] == ["aircraft_484a01"]


class TestLeaves:
    def test_dismissed_when_gone(self, sample_manifest, api, service):
        api.states = [state("484a01", "KLM1234"), state("aaaaaa", "BAW1", km_north=2.0)]
        plugin = make_plugin(sample_manifest, trigger_call_signs="KLM")
        fresh_fetch(plugin)
        plugin.check_triggers()

        api.states = [state("aaaaaa", "BAW1", km_north=2.0)]
        fresh_fetch(plugin)
        assert plugin.check_triggers() == []  # False return from dismiss is ignored
        service.dismiss_trigger.assert_called_once_with("aircraft_484a01")

        fresh_fetch(plugin)
        plugin.check_triggers()
        assert service.dismiss_trigger.call_count == 1  # dismissed only once

    def test_dismissed_when_landed_and_empty_states(self, sample_manifest, api, service):
        api.states = [state("484a01", "KLM1234")]
        plugin = make_plugin(sample_manifest, trigger_call_signs="KLM")
        fresh_fetch(plugin)
        plugin.check_triggers()
        api.states = []
        fresh_fetch(plugin)
        plugin.check_triggers()
        service.dismiss_trigger.assert_called_once_with("aircraft_484a01")

    def test_dismissed_when_on_ground(self, sample_manifest, api, service):
        api.states = [state("484a01", "KLM1234")]
        plugin = make_plugin(sample_manifest, trigger_call_signs="KLM")
        fresh_fetch(plugin)
        plugin.check_triggers()
        api.states = [state("484a01", "KLM1234", on_ground=True)]
        fresh_fetch(plugin)
        plugin.check_triggers()
        service.dismiss_trigger.assert_called_once_with("aircraft_484a01")

    def test_dismiss_errors_are_swallowed(self, sample_manifest, api, service):
        service.dismiss_trigger.side_effect = RuntimeError("boom")
        api.states = [state("484a01", "KLM1234")]
        plugin = make_plugin(sample_manifest, trigger_call_signs="KLM")
        fresh_fetch(plugin)
        plugin.check_triggers()
        api.states = []
        fresh_fetch(plugin)
        assert plugin.check_triggers() == []

    def test_pending_dropped_when_gone_before_check(self, sample_manifest, api, service):
        api.states = [state("484a01", "KLM1234")]
        plugin = make_plugin(sample_manifest, trigger_call_signs="KLM")
        fresh_fetch(plugin)
        api.states = []
        fresh_fetch(plugin)
        assert plugin.check_triggers() == []
        service.dismiss_trigger.assert_not_called()


class TestBeyondMaxAircraft:
    def test_ninth_closest_still_fires(self, sample_manifest, api, service):
        api.states = [state(f"00000{i}", f"BAW{i}", km_north=i + 1) for i in range(8)]
        api.states.append(state("484a01", "KLM1234", km_north=20.0))
        plugin = make_plugin(sample_manifest, trigger_call_signs="KLM", max_aircraft=4)
        result = fresh_fetch(plugin)
        assert result.data["aircraft_count"] == 4
        assert "KLM1234" not in [a["call_sign"] for a in result.data["aircraft"]]

        trig = plugin.check_triggers()
        assert [t.trigger_id for t in trig] == ["aircraft_484a01"]
        assert trig[0].data["call_sign"] == "KLM1234"
        assert trig[0].data["distance_km"] == 20.0
        assert trig[0].data["aircraft_count"] == 4


class TestFetchFailure:
    @pytest.mark.parametrize("failure", ["429", "500", "network"])
    def test_cache_paths_leave_state_unchanged(self, sample_manifest, api, service, failure):
        api.states = [state("484a01", "KLM1234")]
        plugin = make_plugin(sample_manifest, trigger_call_signs="KLM")
        fresh_fetch(plugin)
        assert len(plugin.check_triggers()) == 1

        # The next OpenSky answer would drop KLM1234 and add KLM9 -- but it fails.
        api.states = [state("484a02", "KLM9")]
        if failure == "network":
            api.opensky_error = requests.exceptions.ConnectionError("down")
        else:
            api.opensky_status = int(failure)
        plugin._cache["last_updated"] = "2000-01-01T00:00:00+00:00"  # stale -> refetch
        result = plugin.fetch_data()
        assert result.available and result.data["call_sign"] == "KLM1234"  # cached
        assert plugin.check_triggers() == []
        service.dismiss_trigger.assert_not_called()

    def test_fresh_cache_hit_leaves_state_unchanged(self, sample_manifest, api, service):
        api.states = [state("484a01", "KLM1234")]
        plugin = make_plugin(sample_manifest, trigger_call_signs="KLM")
        plugin.fetch_data()
        plugin.check_triggers()
        api.states = []
        plugin.fetch_data()  # served from the age cache
        plugin.check_triggers()
        service.dismiss_trigger.assert_not_called()


class TestTypeRule:
    def test_type_rule_matches_readable_prefix_and_designator(self, sample_manifest, api, service):
        api.states = [state("000001", "AAA1", km_north=1.0), state("000002", "BBB2", km_north=2.0),
                      state("000003", "CCC3", km_north=3.0), state("000004", "DDD4", km_north=4.0)]
        api.types = {"000001": "A388", "000002": "B748", "000003": "B738"}
        plugin = make_plugin(sample_manifest, trigger_aircraft_types="a380, 747")
        fresh_fetch(plugin)
        ids = sorted(t.trigger_id for t in plugin.check_triggers())
        assert ids == ["aircraft_000001", "aircraft_000002"]  # 000004: lookup miss

    def test_icao_designator_exact(self, sample_manifest, api, service):
        api.states = [state("000001", "AAA1")]
        api.types = {"000001": "B77W"}
        plugin = make_plugin(sample_manifest, trigger_aircraft_types="B77W")
        fresh_fetch(plugin)
        assert len(plugin.check_triggers()) == 1

    def test_type_looked_up_only_after_other_rules(self, sample_manifest, api, service):
        api.states = [state("000001", "BAW1", km_north=1.0)]  # displayed
        api.states += [state(f"10000{i}", f"BAW{i}", km_north=5 + i) for i in range(5)]
        api.states.append(state("484a01", "KLM1234", km_north=20.0))
        api.types = {"484A01": "A388"}
        plugin = make_plugin(sample_manifest, trigger_call_signs="KLM",
                             trigger_aircraft_types="A380", max_aircraft=1)
        fresh_fetch(plugin)
        assert sorted(api.type_lookups) == ["000001", "484A01"]
        assert [t.trigger_id for t in plugin.check_triggers()] == ["aircraft_484a01"]

    def test_type_lookups_capped_per_fetch(self, sample_manifest, api, service):
        api.states = [state(f"2000{i:02d}", f"X{i}", km_north=1 + i) for i in range(14)]
        api.types = {f"2000{i:02d}": "A388" for i in range(14)}
        plugin = make_plugin(sample_manifest, trigger_aircraft_types="A380", max_aircraft=1)

        fresh_fetch(plugin)
        assert len(api.type_lookups) == na.MAX_TRIGGER_TYPE_LOOKUPS_PER_FETCH
        assert len(plugin.check_triggers()) == 10

        # Memoised lookups are free; the remaining 4 are checked next fetch.
        fresh_fetch(plugin)
        assert len(api.type_lookups) == 14
        assert len(plugin.check_triggers()) == 4


# ── State, validation, manifest ───────────────────────────────────────────


class TestConfigChange:
    def test_config_change_resets_trigger_state(self, sample_manifest, api, service):
        api.states = [state("484a01", "KLM1234")]
        plugin = make_plugin(sample_manifest, trigger_call_signs="KLM")
        fresh_fetch(plugin)
        plugin.check_triggers()
        assert plugin._trigger_fired_at

        plugin.config = {**plugin.config, "trigger_call_signs": "KLM, BAW"}
        assert plugin._trigger_fired_at == {}
        assert plugin._trigger_active == set()
        fresh_fetch(plugin)
        assert len(plugin.check_triggers()) == 1

    def test_rules_removed_drops_pending(self, sample_manifest, api, service):
        api.states = [state("484a01", "KLM1234")]
        plugin = make_plugin(sample_manifest, trigger_call_signs="KLM")
        fresh_fetch(plugin)
        plugin._config = {**plugin.config, "trigger_call_signs": ""}  # bypass reset
        assert plugin.check_triggers() == []


class TestTriggerValidation:
    def test_valid_trigger_settings(self, plugin_config_base):
        plugin, config = plugin_config_base
        config.update(trigger_call_signs="KLM", trigger_aircraft_types="A380",
                      trigger_max_altitude_ft=0, trigger_max_distance_km=2.5,
                      trigger_duration_seconds=10, trigger_cooldown_minutes=0)
        assert plugin.validate_config(config) == []

    def test_empty_numbers_are_valid(self, plugin_config_base):
        plugin, config = plugin_config_base
        config.update(trigger_max_altitude_ft=None, trigger_max_distance_km="")
        assert plugin.validate_config(config) == []

    @pytest.mark.parametrize("key,value,needle", [
        ("trigger_max_altitude_ft", -1, "altitude"),
        ("trigger_max_altitude_ft", 1.5, "altitude"),
        ("trigger_max_altitude_ft", True, "altitude"),
        ("trigger_max_distance_km", -0.1, "distance"),
        ("trigger_max_distance_km", "far", "distance"),
        ("trigger_duration_seconds", 9, "duration"),
        ("trigger_duration_seconds", "60", "duration"),
        ("trigger_cooldown_minutes", -1, "cooldown"),
        ("trigger_call_signs", ["KLM"], "call signs"),
        ("trigger_aircraft_types", 380, "aircraft types"),
    ])
    def test_invalid_trigger_settings(self, plugin_config_base, key, value, needle):
        plugin, config = plugin_config_base
        config[key] = value
        errors = plugin.validate_config(config)
        assert len(errors) == 1 and needle in errors[0].lower()

    def test_invalid_runtime_values_fall_back(self, sample_manifest, api, service):
        api.states = [state("484a01", "KLM1234")]
        plugin = make_plugin(sample_manifest, trigger_call_signs="KLM",
                             trigger_max_altitude_ft="high", trigger_duration_seconds=1,
                             trigger_cooldown_minutes=-5)
        fresh_fetch(plugin)
        trig = plugin.check_triggers()
        assert trig[0].duration_seconds == na.DEFAULT_TRIGGER_DURATION_SECONDS
        assert plugin._trigger_cooldown() == timedelta(minutes=na.DEFAULT_TRIGGER_COOLDOWN_MINUTES)


@pytest.fixture
def plugin_config_base(sample_manifest, sample_config):
    return NearbyAircraftPlugin(sample_manifest), dict(sample_config)


class TestManifest:
    def test_supports_triggers(self, sample_manifest):
        assert sample_manifest["supports_triggers"] is True
        assert NearbyAircraftPlugin(sample_manifest).supports_triggers is True

    def test_trigger_page_id_not_declared(self, sample_manifest):
        props = sample_manifest["settings_schema"]["properties"]
        assert "trigger_page_id" not in props

    def test_trigger_settings_declared(self, sample_manifest):
        props = sample_manifest["settings_schema"]["properties"]
        assert props["trigger_call_signs"]["type"] == "string"
        assert props["trigger_aircraft_types"]["type"] == "string"
        assert props["trigger_max_altitude_ft"] == {**props["trigger_max_altitude_ft"],
                                                    "type": "integer", "minimum": 0}
        assert props["trigger_max_distance_km"]["type"] == "number"
        assert props["trigger_max_distance_km"]["minimum"] == 0
        assert props["trigger_duration_seconds"]["default"] == 60
        assert props["trigger_duration_seconds"]["minimum"] == 10
        assert props["trigger_cooldown_minutes"]["default"] == 30
        assert props["trigger_cooldown_minutes"]["minimum"] == 0
        assert sample_manifest["version"] == "1.3.0"

    def test_host_injects_trigger_page_id(self, sample_manifest):
        from src.plugins.manifest import PluginManifest, validate_manifest

        valid, errors = validate_manifest(sample_manifest)
        assert valid, errors
        manifest = PluginManifest.from_dict(sample_manifest)
        assert "trigger_page_id" in manifest.settings_schema["properties"]


class TestHostTriggerServiceIntegration:
    def test_fire_then_leave_with_real_service(self, sample_manifest, api, tmp_path, monkeypatch):
        import src.triggers.service as service_module
        from src.triggers.service import TriggerService

        real = TriggerService(dismissals_file=tmp_path / "trigger_dismissals.json")
        monkeypatch.setattr(service_module, "_trigger_service", real)

        api.states = [state("484a01", "KLM1234")]
        plugin = make_plugin(sample_manifest, trigger_call_signs="KLM")
        plugin.enabled = True
        fresh_fetch(plugin)

        real.check_plugin_triggers(plugin)
        active = real.get_active_trigger()
        assert active is not None
        assert active.trigger_id == "aircraft_484a01"
        assert active.plugin_id == "nearby_aircraft"
        assert active.data["call_sign"] == "KLM1234"
        activated_at = active.activated_at

        # Later engine passes do not restart the timer.
        real.check_plugin_triggers(plugin)
        assert real.get_active_trigger().activated_at == activated_at

        api.states = []
        fresh_fetch(plugin)
        real.check_plugin_triggers(plugin)
        assert real.get_active_trigger() is None
        assert real.list_active_triggers() == []


# ── Review follow-ups ─────────────────────────────────────────────────────


class TestLookupBudget:
    def test_enrichment_shares_lookup_budget(self, sample_manifest, api, service):
        # Distance-only rule: 8 new matches would need 16 uncached lookups.
        api.states = [state(f"3000{i:02d}", f"KLM{i}", km_north=1 + i) for i in range(8)]
        api.routes = {f"KLM{i}": ("AMS", "JFK") for i in range(8)}
        api.types = {f"3000{i:02d}": "B738" for i in range(8)}
        plugin = make_plugin(sample_manifest, trigger_max_distance_km=40, max_aircraft=1)
        fresh_fetch(plugin)
        display_lookups = 2  # route + type of the one displayed aircraft
        trig = plugin.check_triggers()
        assert len(trig) == 8  # all still fire
        trigger_lookups = len(api.type_lookups) + len(api.route_lookups) - display_lookups
        assert trigger_lookups == na.MAX_TRIGGER_TYPE_LOOKUPS_PER_FETCH
        by_id = {t.trigger_id: t for t in trig}
        assert by_id["aircraft_300000"].data["origin"] == "AMS"  # memoised by display
        assert by_id["aircraft_300007"].data["origin"] == ""  # budget spent
        assert by_id["aircraft_300007"].data["aircraft_type"] == ""


class TestPendingAndReset:
    def test_dropped_pending_does_not_block_cooldown(self, sample_manifest, api, service):
        api.states = [state("484a01", "KLM1234")]
        plugin = make_plugin(sample_manifest, trigger_call_signs="KLM")
        fresh_fetch(plugin)
        api.states = []
        fresh_fetch(plugin)  # gone before check_triggers(): never shown
        assert plugin._trigger_fired_at == {}
        api.states = [state("484a01", "KLM1234")]
        fresh_fetch(plugin)
        assert [t.trigger_id for t in plugin.check_triggers()] == ["aircraft_484a01"]

    def test_config_change_dismisses_active_triggers(self, sample_manifest, api, service):
        api.states = [state("484a01", "KLM1234")]
        plugin = make_plugin(sample_manifest, trigger_call_signs="KLM")
        fresh_fetch(plugin)
        plugin.check_triggers()
        plugin.config = {**plugin.config, "trigger_call_signs": ""}
        assert plugin.check_triggers() == []
        service.dismiss_trigger.assert_called_once_with("aircraft_484a01")

    def test_queued_dismissals_survive_reset(self, sample_manifest, api, service):
        api.states = [state("484a01", "KLM1234")]
        plugin = make_plugin(sample_manifest, trigger_call_signs="KLM")
        fresh_fetch(plugin)
        plugin.check_triggers()
        api.states = []
        fresh_fetch(plugin)  # queues the dismissal
        plugin.config = {**plugin.config, "radius_km": 40}
        plugin.check_triggers()
        service.dismiss_trigger.assert_called_once_with("aircraft_484a01")


class TestNoLongerMatching:
    def test_dismissed_when_beyond_max_distance_but_in_radius(self, sample_manifest, api, service):
        api.states = [state("484a01", "KLM1234", km_north=3.0)]
        plugin = make_plugin(sample_manifest, trigger_max_distance_km=5)
        fresh_fetch(plugin)
        assert len(plugin.check_triggers()) == 1
        api.states = [state("484a01", "KLM1234", km_north=8.0)]  # still in 50 km radius
        result = fresh_fetch(plugin)
        assert result.data["call_sign"] == "KLM1234"
        assert plugin.check_triggers() == []
        service.dismiss_trigger.assert_called_once_with("aircraft_484a01")

    def test_dismissed_when_climbing_above_max_altitude(self, sample_manifest, api, service):
        api.states = [state("484a01", "KLM1234", alt_m=500.0)]
        plugin = make_plugin(sample_manifest, trigger_max_altitude_ft=5000)
        fresh_fetch(plugin)
        plugin.check_triggers()
        api.states = [state("484a01", "KLM1234", alt_m=3000.0)]
        fresh_fetch(plugin)
        plugin.check_triggers()
        service.dismiss_trigger.assert_called_once_with("aircraft_484a01")

    def test_active_type_match_kept_when_budget_spent(self, sample_manifest, api, service):
        api.states = [state("484a01", "KLM1234")]
        api.types = {"484A01": "A388"}
        plugin = make_plugin(sample_manifest, trigger_aircraft_types="A380", max_aircraft=1)
        fresh_fetch(plugin)
        assert len(plugin.check_triggers()) == 1
        # Many new uncached candidates exhaust the budget; the active one is memoised.
        api.states += [state(f"4000{i:02d}", f"X{i}", km_north=2 + i) for i in range(12)]
        fresh_fetch(plugin)
        plugin.check_triggers()
        service.dismiss_trigger.assert_not_called()


class TestNoExtraOpenSkyCalls:
    def test_one_states_request_within_refresh_window(self, sample_manifest, service):
        fake = FakeApi(states=[state("484a01", "KLM1234")])
        calls = []

        def spy(url, **kwargs):
            calls.append(url)
            return fake(url, **kwargs)

        plugin = make_plugin(sample_manifest, trigger_call_signs="KLM", refresh_seconds=300)
        with patch("plugins.nearby_aircraft.requests.get", side_effect=spy):
            plugin.fetch_data()
            for _ in range(3):
                plugin.check_triggers()
            plugin.fetch_data()
            for _ in range(3):
                plugin.check_triggers()
        assert calls.count(na.OPENSKY_STATES_URL) == 1


class TestBoundaries:
    def test_altitude_exactly_at_max_fires(self, sample_manifest, api, service):
        api.states = [state("484a01", "KLM1234", alt_m=1524.0)]  # 5000 ft
        plugin = make_plugin(sample_manifest, trigger_max_altitude_ft=5000)
        result = fresh_fetch(plugin)
        assert result.data["altitude"] == 5000
        assert len(plugin.check_triggers()) == 1

    def test_distance_exactly_at_max_fires(self, sample_manifest, api, service):
        api.states = [state("484a01", "KLM1234", km_north=3.0)]
        plugin = make_plugin(sample_manifest, trigger_max_distance_km=3.0)
        result = fresh_fetch(plugin)
        assert result.data["aircraft"][0]["distance_km"] == 3.0
        assert len(plugin.check_triggers()) == 1

    def test_returning_inside_cooldown_does_not_fire(self, sample_manifest, api, service):
        api.states = [state("484a01", "KLM1234")]
        plugin = make_plugin(sample_manifest, trigger_call_signs="KLM")
        fresh_fetch(plugin)
        assert len(plugin.check_triggers()) == 1
        api.states = []
        fresh_fetch(plugin)
        plugin.check_triggers()
        service.dismiss_trigger.assert_called_once_with("aircraft_484a01")
        api.states = [state("484a01", "KLM1234")]
        fresh_fetch(plugin)
        assert plugin.check_triggers() == []
