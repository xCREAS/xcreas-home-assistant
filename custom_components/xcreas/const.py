"""Constants for the xCREAS Home Assistant integration.

Contract authority: xcreas-api `docs/ha-integration.md` (§1 endpoints, §3
account device inventory, §4 snapshot, §5 command, §6 entity mapping, §7
push channel). The API samples in `tests/fixtures/contract_samples.json`
mirror that document.
"""

from __future__ import annotations

DOMAIN = "xcreas"

# Default cloud endpoints (the API base URL convention; overridable via the
# config flow's advanced field for test deployments).
DEFAULT_BASE_URL = "https://api.xcreas.com"

CONF_BASE_URL = "base_url"
CONF_ACCOUNT_ID = "account_id"

# The HA public client id — NOT a secret (public client + PKCE S256; see
# docs/ha-integration.md §2). The matching client registration lives in the
# xcreas-api deployment's `OAuth.HA` config.
OAUTH2_CLIENT_ID = "xcreas-ha"

AUTHORIZE_URL = f"{DEFAULT_BASE_URL}/oauth/ha/authorize"
TOKEN_URL = f"{DEFAULT_BASE_URL}/oauth/ha/token"

# The ACCOUNT-level authorization scope (2026-11-21 account-sync contract
# §2): one consent covers every device the account can currently access plus
# any device it becomes entitled to later. The cloud rejects the legacy
# per-device scope "homeassistant".
SCOPES = ["homeassistant:account"]

SNAPSHOT_PATH = "/api/integrations/ha/snapshot"
COMMAND_PATH = "/api/integrations/ha/command"
WS_PATH = "/ws/ha"

# --- push channel behavior (docs/ha-integration.md §7) ---
# The server throttles "changed" frames (PushThrottleMs); the client adds a
# small debounce on top so a burst of frames still costs one snapshot pull.
WS_DEBOUNCE_SECONDS = 1.0
# Low-frequency calibration to recover silently lost notifications (spec §3:
# 定期低频校准). The coordinator's fallback poll period.
FALLBACK_POLL_SECONDS = 300
# Reconnect backoff: exponential 1s → 300s with ±20% jitter (spec §7).
WS_BACKOFF_MIN = 1.0
WS_BACKOFF_MAX = 300.0

# --- entity mapping (docs/ha-integration.md §6) ---
MANUFACTURER = "xCREAS"
MODEL = "V350s"

PRESET_MODES = ["auto", "sleep", "eco"]
PRESET_TO_MODE = {"auto": 1, "sleep": 2, "eco": 3}

SPEED_COUNT = 100  # 1% percentage steps over the 1..100 scale

# Availability semantics (§6): a device is entity-available only when the
# snapshot marks it online and the last full refresh succeeded.
AVAILABILITY_ONLINE = "online"
AVAILABILITY_OFFLINE = "offline"
AVAILABILITY_UNKNOWN = "unknown"
