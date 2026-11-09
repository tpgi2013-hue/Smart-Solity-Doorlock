"""Constants for the Solity BLE (direct) integration."""

DOMAIN = "solity_ble"

CONF_NAME = "name"
CONF_MAC = "mac"
CONF_APP_KEY = "app_key"

# How long to show 'unlocked' after a BLE open before assuming the deadbolt
# auto-relocked (there is no state feedback over BLE). Matches the open
# command's auto-relock window (second = 7) plus a small margin.
OPTIMISTIC_UNLOCK_SECONDS = 9

MANUFACTURER = "SOLITY"
MODEL = "WELKOM (BLE)"
