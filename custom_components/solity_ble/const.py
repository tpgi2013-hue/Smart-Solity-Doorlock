"""Constants for the Solity BLE (direct) integration."""

DOMAIN = "solity_ble"

CONF_NAME = "name"
CONF_MAC = "mac"
CONF_APP_KEY = "app_key"
CONF_STATUS_MINUTES = "status_minutes"

# How often to read lock status over BLE (battery, deadbolt, counts).
# 0 disables periodic polling (status then updates only on open/close).
DEFAULT_STATUS_MINUTES = 30
MIN_STATUS_MINUTES = 0
MAX_STATUS_MINUTES = 1440

# How long to show 'unlocked' after a BLE open before assuming the deadbolt
# auto-relocked, when no status frame confirms the real state. Matches the
# open command's auto-relock window (second = 7) plus a small margin.
OPTIMISTIC_UNLOCK_SECONDS = 9

MANUFACTURER = "SOLITY"
MODEL = "WELKOM (BLE)"

# How long a prewarmed (connected + authenticated) BLE session is held open
# waiting for a command before it is dropped again.
PREWARM_HOLD_SECONDS = 8

# While a status read shows the deadbolt retracted (door just used / left
# open), re-read this often until it reports locked, at most this many times.
UNLOCKED_RECHECK_SECONDS = 15
UNLOCKED_RECHECK_MAX = 8
