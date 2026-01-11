#!/usr/bin/env python3
"""API test script for SAIVerse test environment.

Tests that the test environment is correctly set up and API endpoints work.

Usage:
    python test_fixtures/test_api.py              # Run all tests
    python test_fixtures/test_api.py --quick      # Skip chat test (no LLM call)
    python test_fixtures/test_api.py --chat       # Test chat endpoint only

Requires the test server to be running:
    ./test_fixtures/start_test_server.sh
"""

import argparse
import json
import sys
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

# Test server configuration (matches test_data.json)
BASE_URL = "http://127.0.0.1:18000"

# Expected test data (from test_fixtures/definitions/test_data.json)
EXPECTED_CITY = {
    "CITYID": 1,
    "CITYNAME": "test_city",
}

EXPECTED_BUILDINGS = [
    {"BUILDINGID": "test_lobby", "BUILDINGNAME": "Test Lobby"},
    {"BUILDINGID": "test_room_a", "BUILDINGNAME": "Test Room A"},
]

EXPECTED_PERSONAS = [
    {"AIID": "test_persona_a", "AINAME": "Test Persona A"},
    {"AIID": "test_persona_b", "AINAME": "Test Persona B"},
]

EXPECTED_PLAYBOOKS = ["basic_chat", "meta_user", "meta_auto", "sub_router_user"]


def request(method: str, path: str, data: dict = None, streaming: bool = False) -> dict:
    """Make HTTP request and return JSON response.

    Args:
        method: HTTP method
        path: API path
        data: Request body data
        streaming: If True, parse NDJSON streaming response
    """
    url = f"{BASE_URL}{path}"
    headers = {"Content-Type": "application/json"}

    body = json.dumps(data).encode() if data else None
    req = Request(url, data=body, headers=headers, method=method)

    try:
        with urlopen(req, timeout=60) as resp:
            raw = resp.read().decode()
            if streaming:
                # Parse NDJSON (newline-delimited JSON) - return last non-status response
                lines = [l.strip() for l in raw.strip().split('\n') if l.strip()]
                result = {}
                for line in lines:
                    try:
                        obj = json.loads(line)
                        # Keep track of all non-status responses
                        if obj.get("type") != "status":
                            result = obj
                    except json.JSONDecodeError:
                        pass
                return result
            return json.loads(raw)
    except HTTPError as e:
        print(f"HTTP Error {e.code}: {e.reason}")
        try:
            error_body = json.loads(e.read().decode())
            print(f"Response: {json.dumps(error_body, indent=2, ensure_ascii=False)}")
        except:
            pass
        raise
    except URLError as e:
        print(f"Connection Error: {e.reason}")
        print(f"Is the test server running? Try: ./test_fixtures/start_test_server.sh")
        raise


def test_models():
    """Test models config endpoint."""
    print("Testing models endpoint...")
    try:
        resp = request("GET", "/api/config/models")
        models = resp if isinstance(resp, list) else resp.get("models", [])
        print(f"  Models available: {len(models)}")
        if len(models) == 0:
            print("  ! Warning: No models configured")
        print("  OK: Models endpoint works")
        return True
    except Exception as e:
        print(f"  FAIL: Models check failed: {e}")
        return False


def test_city():
    """Test that test city exists in database."""
    print("Testing city data...")
    try:
        resp = request("GET", "/api/db/tables/city")
        rows = resp if isinstance(resp, list) else resp.get("rows", [])

        # Find test city
        test_city = None
        for row in rows:
            if row.get("CITYNAME") == EXPECTED_CITY["CITYNAME"]:
                test_city = row
                break

        if not test_city:
            print(f"  FAIL: Test city '{EXPECTED_CITY['CITYNAME']}' not found")
            print(f"  Cities in DB: {[r.get('CITYNAME') for r in rows]}")
            return False

        print(f"  City: {test_city.get('CITYNAME')} (ID: {test_city.get('CITYID')})")
        print(f"  UI Port: {test_city.get('UI_PORT')}, API Port: {test_city.get('API_PORT')}")
        print("  OK: Test city exists")
        return True
    except Exception as e:
        print(f"  FAIL: City test failed: {e}")
        return False


def test_buildings():
    """Test that test buildings exist in database."""
    print("Testing buildings data...")
    try:
        resp = request("GET", "/api/db/tables/building")
        rows = resp if isinstance(resp, list) else resp.get("rows", [])

        found_buildings = []
        missing_buildings = []

        for expected in EXPECTED_BUILDINGS:
            found = False
            for row in rows:
                if row.get("BUILDINGID") == expected["BUILDINGID"]:
                    found = True
                    found_buildings.append(row)
                    break
            if not found:
                missing_buildings.append(expected["BUILDINGID"])

        for b in found_buildings:
            print(f"  Found: {b.get('BUILDINGNAME')} (ID: {b.get('BUILDINGID')})")

        if missing_buildings:
            print(f"  FAIL: Missing buildings: {missing_buildings}")
            return False

        print(f"  OK: All {len(EXPECTED_BUILDINGS)} expected buildings found")
        return True
    except Exception as e:
        print(f"  FAIL: Buildings test failed: {e}")
        return False


def test_personas():
    """Test that test personas exist in database."""
    print("Testing personas data...")
    try:
        resp = request("GET", "/api/db/tables/ai")
        rows = resp if isinstance(resp, list) else resp.get("rows", [])

        found_personas = []
        missing_personas = []

        for expected in EXPECTED_PERSONAS:
            found = False
            for row in rows:
                if row.get("AIID") == expected["AIID"]:
                    found = True
                    found_personas.append(row)
                    break
            if not found:
                missing_personas.append(expected["AIID"])

        for p in found_personas:
            model = p.get("DEFAULT_MODEL", "default")
            mode = p.get("INTERACTION_MODE", "unknown")
            print(f"  Found: {p.get('AINAME')} (ID: {p.get('AIID')}, model: {model}, mode: {mode})")

        if missing_personas:
            print(f"  FAIL: Missing personas: {missing_personas}")
            return False

        print(f"  OK: All {len(EXPECTED_PERSONAS)} expected personas found")
        return True
    except Exception as e:
        print(f"  FAIL: Personas test failed: {e}")
        return False


def test_playbooks():
    """Test that expected playbooks are imported."""
    print("Testing playbooks...")
    try:
        resp = request("GET", "/api/world/playbooks")
        playbooks = resp if isinstance(resp, list) else resp.get("playbooks", resp.get("items", []))

        playbook_names = [p.get("name") for p in playbooks]

        missing = []
        for expected in EXPECTED_PLAYBOOKS:
            if expected not in playbook_names:
                missing.append(expected)

        print(f"  Playbooks in DB: {len(playbooks)}")
        for name in EXPECTED_PLAYBOOKS:
            status = "OK" if name in playbook_names else "MISSING"
            print(f"    - {name}: {status}")

        if missing:
            print(f"  FAIL: Missing playbooks: {missing}")
            return False

        print(f"  OK: All {len(EXPECTED_PLAYBOOKS)} expected playbooks found")
        return True
    except Exception as e:
        print(f"  FAIL: Playbooks test failed: {e}")
        return False


def test_user_status():
    """Test user status endpoint."""
    print("Testing user status endpoint...")
    try:
        resp = request("GET", "/api/user/status")
        print(f"  User ID: {resp.get('user_id', 'N/A')}")
        print(f"  Current Building: {resp.get('current_building_id', 'None')}")
        print(f"  City: {resp.get('city_name', 'N/A')}")
        print("  OK: User status endpoint works")
        return True
    except Exception as e:
        print(f"  FAIL: User status failed: {e}")
        return False


def test_chat():
    """Test chat send endpoint (requires LLM call)."""
    print("Testing chat endpoint (LLM call)...")
    try:
        # First, move user to test_lobby (required for chat)
        print("  Moving user to test_lobby...")
        move_data = {"target_building_id": "test_lobby"}
        request("POST", "/api/user/move", move_data)

        # Use test_persona_a which should exist
        data = {
            "persona_id": "test_persona_a",
            "message": "Hello, this is a test. Please respond with exactly: TEST_OK",
            "building_id": "test_lobby"
        }
        resp = request("POST", "/api/chat/send", data, streaming=True)

        # Response may have "text", "response", "message", or "content" field
        response_text = resp.get("text", resp.get("response", resp.get("message", resp.get("content", ""))))
        print(f"  Response length: {len(str(response_text))} chars")

        preview = str(response_text)[:150]
        if len(str(response_text)) > 150:
            print(f"  Preview: {preview}...")
        else:
            print(f"  Response: {response_text}")

        if response_text:
            print("  OK: Chat endpoint works (received LLM response)")
            return True
        else:
            print("  FAIL: Empty response from chat endpoint")
            return False
    except Exception as e:
        print(f"  FAIL: Chat test failed: {e}")
        return False


def test_user_buildings():
    """Test user buildings endpoint."""
    print("Testing user buildings endpoint...")
    try:
        resp = request("GET", "/api/user/buildings")
        buildings = resp if isinstance(resp, list) else resp.get("buildings", [])
        print(f"  Buildings accessible to user: {len(buildings)}")
        for b in buildings[:5]:
            name = b.get("BUILDINGNAME") or b.get("name", "unknown")
            bid = b.get("BUILDINGID") or b.get("building_id", "unknown")
            print(f"    - {name} ({bid})")
        print("  OK: User buildings endpoint works")
        return True
    except Exception as e:
        print(f"  FAIL: User buildings failed: {e}")
        return False


def run_all_tests(skip_chat=False):
    """Run all API tests."""
    print("=" * 60)
    print("SAIVerse Test Environment - API Test Suite")
    print(f"Target: {BASE_URL}")
    print("=" * 60)
    print()

    results = []

    # Core data tests - verify test environment setup
    print("-" * 60)
    print("Test Data Verification (from test_data.json)")
    print("-" * 60)

    results.append(("City", test_city()))
    print()

    results.append(("Buildings", test_buildings()))
    print()

    results.append(("Personas", test_personas()))
    print()

    results.append(("Playbooks", test_playbooks()))
    print()

    # API endpoint tests
    print("-" * 60)
    print("API Endpoint Tests")
    print("-" * 60)

    results.append(("Models Config", test_models()))
    print()

    results.append(("User Status", test_user_status()))
    print()

    results.append(("User Buildings", test_user_buildings()))
    print()

    # Chat test (optional, requires LLM)
    if not skip_chat:
        results.append(("Chat (LLM)", test_chat()))
        print()
    else:
        print("Skipping chat test (--quick mode)")
        print()

    # Summary
    print("=" * 60)
    print("Test Summary")
    print("=" * 60)

    passed = sum(1 for _, r in results if r)
    total = len(results)

    for name, result in results:
        status = "PASS" if result else "FAIL"
        marker = "OK" if result else "!!"
        print(f"  [{marker}] {name}: {status}")

    print()
    print(f"Results: {passed}/{total} tests passed")

    if passed == total:
        print("All tests passed!")
    else:
        print("Some tests failed - check output above for details")

    return passed == total


def main():
    global BASE_URL

    parser = argparse.ArgumentParser(description="SAIVerse API Test Script")
    parser.add_argument("--quick", action="store_true", help="Skip chat test (no LLM call)")
    parser.add_argument("--chat", action="store_true", help="Run chat test only")
    parser.add_argument("--base-url", default=BASE_URL, help=f"API base URL (default: {BASE_URL})")

    args = parser.parse_args()
    BASE_URL = args.base_url

    if args.chat:
        success = test_chat()
    else:
        success = run_all_tests(skip_chat=args.quick)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
