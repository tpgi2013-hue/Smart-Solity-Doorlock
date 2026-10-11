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
# open was detected) until the deadbolt reads locked. A door left open keeps
# backing off, then falls back to the regular status interval. The first offset
# is deliberately aggressive (3s); the lock's last_verify_* attributes record
# which poll confirmed it so this can be tuned from real history.
LOCK_VERIFY_OFFSETS = (3, 8, 15, 30, 60, 120, 180, 300, 600, 900)

# Back-to-back measurement used while calibrating: after an open, poll
# get_status as soon as the previous poll returns (floor MEASURE_MIN_INTERVAL)
# for up to MEASURE_WINDOW_SECONDS to time the real relock.
MEASURE_WINDOW_SECONDS = 30
MEASURE_MIN_INTERVAL = 0.5  # floor between poll starts (never hammer the API)
MEASURE_MAX_POLLS = 60

# Auto-calibration (option, default on): measure the first CALIBRATION_SAMPLES
# entries, then derive this home's verify schedule and stop measuring.
#   first poll  = 75th percentile of relock time (most entries: 1 wake-up)
#   second poll = 95th percentile, then doubling up to VERIFY_LAST_SECONDS.
CONF_AUTO_CALIBRATE = "auto_calibrate"
DEFAULT_AUTO_CALIBRATE = True
CONF_RECALIBRATE = "recalibrate"
CALIBRATION_SAMPLES = 10
# Relock times are measured from the lock's own open timestamp when its clock
# agrees with HA's; at least this many samples must have a usable timestamp,
# otherwise the schedule is anchored to HA's detection instead.
CALIBRATION_MIN_OPEN_BASIS = 8
# Lock clock sanity: detection may lag the logged open by 0..MAX_DETECT_LAG
# seconds; a logged open more than CLOCK_AHEAD_TOLERANCE in the future (or
# older than the max lag) means the lock clock is off -> use detection time.
MAX_DETECT_LAG_SECONDS = 120
CLOCK_AHEAD_TOLERANCE_SECONDS = 5
VERIFY_MIN_FIRST_SECONDS = 1.0
VERIFY_LAST_SECONDS = 900

# Manual override for the first verify poll (seconds after the real open).
# 0 = use the calibrated/default schedule.
CONF_FIRST_VERIFY_SECONDS = "first_verify_seconds"
DEFAULT_FIRST_VERIFY_SECONDS = 0
MAX_FIRST_VERIFY_SECONDS = 60

# Attribute values tagging the deadbolt-confirmed close event.
VERIFIED_CLOSE_METHOD = "deadbolt"
VERIFIED_CLOSE_LOG_TYPE = "DEADBOLT_STATUS"
VERIFIED_CLOSE_MESSAGE = "데드볼트 잠김이 확인되었습니다."

MANUFACTURER = "SOLITY"
MODEL = "WELKOM"

# Door-lock timezone for the log API (Korea). logDateTime is in this zone.
LOG_TIMEZONE = "+9:00"
LOG_UTC_OFFSET_HOURS = 9

# Event-log codes (logCode) → event category.
LOG_CODE_CLOSE = "0"
LOG_CODE_OPEN = "1"
LOG_CODE_OPEN_LONG = "7"
OPEN_LOG_CODES = (LOG_CODE_OPEN, LOG_CODE_OPEN_LONG)

EVENT_OPEN = "open"
EVENT_CLOSE = "close"
EVENT_OTHER = "other"
EVENT_TYPES = [EVENT_OPEN, EVENT_CLOSE, EVENT_OTHER]
