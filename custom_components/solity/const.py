"""Constants for the Smart Solity Doorlock integration."""

DOMAIN = "solity"

CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_HASHED_PWD = "hashed_pwd"
CONF_DEVICE_ID = "device_id"
CONF_NICKNAME = "nickname"

# Event-log poll (fast, seconds) — detects external open/close/fingerprint.
CONF_LOG_SECONDS = "log_seconds"
DEFAULT_LOG_SECONDS = 10
MIN_LOG_SECONDS = 5
MAX_LOG_SECONDS = 600

# Status poll (slow, minutes) — battery % and deadbolt resync (wakes the lock).
CONF_STATUS_MINUTES = "status_minutes"
DEFAULT_STATUS_MINUTES = 30
MIN_STATUS_MINUTES = 1
MAX_STATUS_MINUTES = 1440

# How many consecutive status-poll misses to tolerate before the lock/battery
# go 'unavailable'. A battery lock is asleep most of the time and often misses
# a single poll, so one miss must NOT flap the entities to unavailable.
TOLERATED_STATUS_FAILURES = 3

# The device logs ONLY open events (never a lock/close). The real deadbolt
# (motor) state is only available from get_status, which wakes the lock. So
# after every open we re-poll get_status at these offsets (seconds since the
# open was detected) until the deadbolt reads locked. Normal entries confirm on
# the 1st-2nd poll; a door left open keeps backing off, then falls back to the
# regular status interval.
LOCK_VERIFY_OFFSETS = (5, 10, 20, 40, 70, 120, 180, 300, 600, 900)

# Attribute values tagging the deadbolt-confirmed close event.
VERIFIED_CLOSE_METHOD = "deadbolt"
VERIFIED_CLOSE_LOG_TYPE = "DEADBOLT_STATUS"
VERIFIED_CLOSE_MESSAGE = "데드볼트 잠김이 확인되었습니다."

MANUFACTURER = "SOLITY"
MODEL = "WELKOM"

# Door-lock timezone for the log API (Korea).
LOG_TIMEZONE = "+9:00"

# Event-log codes (logCode) → event category.
LOG_CODE_CLOSE = "0"
LOG_CODE_OPEN = "1"
LOG_CODE_OPEN_LONG = "7"
OPEN_LOG_CODES = (LOG_CODE_OPEN, LOG_CODE_OPEN_LONG)

EVENT_OPEN = "open"
EVENT_CLOSE = "close"
EVENT_OTHER = "other"
EVENT_TYPES = [EVENT_OPEN, EVENT_CLOSE, EVENT_OTHER]
