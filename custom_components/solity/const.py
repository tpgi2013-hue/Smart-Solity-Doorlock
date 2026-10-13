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

# The device logs ONLY open events (never a lock/close). The real lock state
# comes from get_status (which wakes the lock): deadBolt (1 = bolt thrown) and
# subLatch, the push-pull mortise's door-detect latch (1 = pressed by the
# frame = door closed, 0 = door open). After every open we poll and let the
# door state pick the next check:
#   locked            -> done
#   closed, unlocked  -> auto-lock is about to fire: recheck VERIFY_CLOSED_RECHECK
#                        (after VERIFY_CLOSED_FAST_TRIES such rechecks the lock
#                        is probably in manual mode -> back off)
#   open              -> people still passing: VERIFY_OPEN_FIRST, doubling up
#                        to VERIFY_BACKOFF_MAX
# for up to VERIFY_WINDOW seconds, then back to the regular status interval.
VERIFY_CLOSED_RECHECK = 1.5
VERIFY_CLOSED_FAST_TRIES = 3
VERIFY_OPEN_FIRST = 5
VERIFY_UNKNOWN_RECHECK = 3
VERIFY_BACKOFF_MAX = 120
VERIFY_WINDOW = 900
VERIFY_MAX_POLLS = 40
VERIFY_TRACE_LEN = 15

# Right after an open the first get_status often hangs until the HTTP timeout
# (the WiFi+ module seems busy uploading the event); the next call answers in
# ~1s. Post-open polls therefore give up after VERIFY_POLL_TIMEOUT and retry
# immediately (up to VERIFY_POLL_RETRIES extra tries per check) instead of
# blocking 15s. Successful polls observed so far took <= 2.3s.
VERIFY_POLL_TIMEOUT = 2.5
VERIFY_POLL_RETRIES = 3

# When the lock stops answering (WiFi module stuck/offline, seen in the field),
# don't wait for the next regular status poll (up to hours): re-poll after
# these delays, staying at the last one, until it answers again.
RECOVER_DELAYS = (60, 120, 300)

# Leftovers from v1.6.x auto-calibration, cleaned up on setup.
LEGACY_OPTION_KEYS = (
    "auto_calibrate",
    "first_verify_seconds",
    "recalibrate",
    "measure_mode",
    "auto_close_seconds",
)
LEGACY_CALIBRATION_STORE = "calibration"
# Entities removed in later versions: (platform, unique_id suffix).
LEGACY_ENTITIES = (
    ("sensor", "calibration"),  # v1.7.0
    ("event", "door_event"),  # v1.7.1: covered by the lock + door sensor
)

# Repair issue raised when the phone app (same account) kicks HA's session.
ISSUE_SESSION_TAKEN = "session_taken"

MANUFACTURER = "SOLITY"
MODEL = "WELKOM"

# Door-lock timezone for the log API (Korea).
LOG_TIMEZONE = "+9:00"

# Event-log codes (logCode) that mean the door was opened.
LOG_CODE_OPEN = "1"
LOG_CODE_OPEN_LONG = "7"
OPEN_LOG_CODES = (LOG_CODE_OPEN, LOG_CODE_OPEN_LONG)
