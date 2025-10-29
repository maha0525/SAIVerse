# SAIVerse Discord Gateway Onboarding Manual

This manual walks through the end-to-end setup of the Discord Gateway/Bot integration, including local preparation, Discord-side configuration, and the manual acceptance tests expected before shipping to users.

---

## 1. Prerequisites

### 1.1 Local environment
- Python 3.11 or newer
- `pip` or Poetry available
- SAIVerse repository checked out
- Dependencies from `requirements.txt` and `discord_gateway/requirements-dev.txt`

### 1.2 Discord application (Bot)
1. Create a new application at the [Discord Developer Portal](https://discord.com/developers/applications).
2. Add a Bot and enable these intents:
   - Gateway Intents: **Guild Members**, **Message Content**
   - Privileged Intent **Presence** is optional.
3. Copy the Bot token (will be placed in `.env` later).
4. Under OAuth2 → General, note the Client ID / Client Secret and configure a redirect URL (`https://<gateway-host>/oauth/callback`).
5. Keep the application credentials handy for `.env` setup.

### 1.3 Discord server (City host)
1. Invite the Bot to the server with the permissions *View Channels*, *Send Messages*, *Manage Messages*.
2. Create or identify channels/threads representing SAIVerse Cities/Buildings.
3. Record channel IDs, guild ID, and the host user’s Discord ID (needed for channel mapping).

---

## 2. Environment variables

### 2.1 Bot service (`.env`)
```
SAIVERSE_GATEWAY_ENABLED=1
DISCORD_BOT_TOKEN=xxxxxxxxxxxxxxxx
DISCORD_APPLICATION_ID=123456789012345678
DISCORD_OAUTH_CLIENT_ID=xxxxxxxxxxxxxxxx
DISCORD_OAUTH_CLIENT_SECRET=xxxxxxxxxxxxxxxx
SAIVERSE_OAUTH_REDIRECT_URI=https://example.com/oauth/callback
SAIVERSE_WS_HOST=0.0.0.0
SAIVERSE_WS_PORT=8788
SAIVERSE_WS_PATH=/ws
SAIVERSE_PENDING_REPLAY_LIMIT=250
SAIVERSE_REPLAY_BATCH_SIZE=50
SAIVERSE_MAX_MESSAGE_LENGTH=1800
SAIVERSE_WS_TLS_ENABLED=1
SAIVERSE_WS_TLS_CERTFILE=/etc/ssl/certs/saiverse_gateway.crt
SAIVERSE_WS_TLS_KEYFILE=/etc/ssl/private/saiverse_gateway.key
# Optional client auth
# SAIVERSE_WS_TLS_CLIENT_AUTH=required
# SAIVERSE_WS_TLS_CA_FILE=/etc/ssl/certs/saiverse_client_ca.pem
```

### 2.2 Local gateway (`discord_gateway/config.py` or environment)
```
SAIVERSE_GATEWAY_WS_URL=wss://example.com/ws
SAIVERSE_GATEWAY_TOKEN=<token-issued-by-bot>
SAIVERSE_GATEWAY_RECONNECT_INITIAL=1.0
SAIVERSE_GATEWAY_RECONNECT_MAX=30.0
SAIVERSE_GATEWAY_RECONNECT_JITTER=0.3
```

### 2.3 Channel mapping
Supply via environment variable `SAIVERSE_GATEWAY_CHANNEL_MAP` or a JSON file:
```json
{
  "123456789012345678": {
    "city_id": "city_a",
    "building_id": "lobby",
    "host_user_id": "999999999999999999",
    "allowed_roles": ["VIP", "MODERATOR"],
    "invite_required": true
  }
}
```

---

## 3. Setup

### 3.1 Bot service
```bash
pip install -r discord_gateway/requirements-dev.txt
python -m discord_gateway.bot.app
```
Ensure the bot host has a valid TLS certificate and key at the paths referenced by the environment variables above; the WebSocket server refuses to start without them when `SAIVERSE_WS_TLS_ENABLED=1`.
A successful launch prints `Gateway WebSocket server listening on ...`.

### 3.2 Local application
SAIVerse `main.py` can start the gateway runtime automatically. To run the automated test suite locally:
```bash
python scripts/run_discord_gateway_tests.py
```

---

## 4. Manual acceptance checklist

### 4.1 Basic connectivity
1. Start the Bot service.
2. Start the local application and confirm the Bot logs `Registered local app connection`.
3. Post in the Discord City channel and verify the SAIVerse UI receives the message.

### 4.2 Invite & role control
1. Run `!saiverse invite grant @user` and confirm the user can chat.
2. Send from a non-invited user and verify a `permission_denied` system message.
3. Test invite revoke, invite clear, and role-based auto-allow scenarios.

### 4.3 Memory sync (large transfer & drop)
1. Trigger a large memory sync from SAIVerse (`trigger_world_event` or manual command).
2. Observe Bot logs for chunk replay vs `resync_required` transitions.
3. Run automated tests:
   - `pytest discord_gateway/tests/test_orchestrator.py::test_memory_sync_large_transfer`
   - `pytest discord_gateway/tests/test_orchestrator.py::test_memory_sync_duplicate_chunk_ack_without_duplicate_processing`

### 4.4 Connection loss & replay
1. Kill the local application; confirm the Bot awaits reconnection.
2. Restart the local application and ensure pending events are replayed.
3. Force `resync_required` (e.g., by delaying ACKs) and confirm automatic `state_sync_request` handling.

### 4.5 OAuth login
1. Initiate login from the local application; complete Discord OAuth consent.
2. Ensure the issued session token is stored securely (OS keychain/Credential Manager).
3. Re-run `scripts/run_discord_gateway_tests.py` to ensure no regressions.

---

## 5. Reference
- Automated suite: `scripts/run_discord_gateway_tests.py`
- Integration tests: `pytest discord_gateway/tests/test_ws_integration.py`
- Memory sync stress: see the orchestrator tests mentioned above
- Configuration reference: `discord_gateway/bot/config.py`, `discord_gateway/config.py`
- Architecture detail: `discord_gateway/docs/implementation_discord.md`

---

## 6. Troubleshooting
| Symptom | Suggested fix |
| --- | --- |
| `Invalid token` on connect | Verify Bot token and generated session token in `.env` |
| `permission_denied` for expected user | Check `host_user_id`, `allowed_roles`, or invite status in channel mapping |
| Frequent `resync_required` events | Increase `SAIVERSE_PENDING_REPLAY_LIMIT` or inspect network latency |
| OAuth finishes but app fails to connect | Ensure redirect URL matches exactly and the Bot host is reachable |

---

## 7. Acceptance report template
```
- [ ] Bot service boot logs attached
- [ ] Local gateway WebSocket connection verified
- [ ] Invite / role scenarios confirmed
- [ ] Memory sync (large/drop) scenarios confirmed
- [ ] Reconnect / resync flow confirmed
- [ ] OAuth login manually verified
- [ ] scripts/run_discord_gateway_tests.py result attached
- [ ] Additional feedback or issues
```

Share this report with the team once all checkboxes are ticked. Update the manual with lessons learned from each onboarding run.
