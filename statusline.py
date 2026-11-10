#!/usr/bin/env python3
"""One-line Claude Code status line: plan usage, reset timers, manual reset, model cost.

  5h ███░░░░░░░ 32% ~€15 left ↻4h34m /limit-reset │ 7d ░░░░░░░░░░ 4% ~€139 left ↻3d19h │ Fable 5.2 xhigh €7.13/€35.67

For the 5-hour and the 7-day limit: a bar and the percentage used, an estimate of
the euro value still left, and the time until the limit resets. `/limit-reset` is
shown while a manual reset is available. Then the model, the effort level (with
Claude Code's relative cost of that effort, where it publishes one) and the list
price per million input/output tokens. Parts are dropped when the terminal is too
narrow; the bars go last.

Where the numbers come from
  * Percentages and reset times: `rate_limits` in the status line input
    (https://code.claude.com/docs/en/statusline). Only claude.ai subscriptions have
    them, and only after the first response of a session; until then the last seen
    values are shown.
  * Manual reset: `unusedLimitResetStatusCache` in ~/.claude.json, the cache Claude
    Code keeps for its own /limit-reset notice. It is refreshed when Claude Code
    starts and is internal, so it may change between versions.
  * Euro: US dollar amounts converted with the ECB reference rate, fetched in the
    background at most twice a day. Set CLAUDE_STATUSLINE_USD_EUR to a fixed rate
    (e.g. 0.89) to never touch the network.
  * "~€ left" is an ESTIMATE. A subscription has no euro quota, so the script learns
    how many dollars of list-price usage (`cost.total_cost_usd`) move each limit by
    one percentage point, from the Claude Code sessions on this machine, and scales
    the remaining percentage by that. It appears once enough usage has been seen.

State lives in ~/.claude/statusline/: state.json (learned ratios), fx.json (rate),
error.log (last failure). `touch ~/.claude/statusline/debug` dumps each input to
last-input.json; delete the file to stop.
"""

import json
import os
import re
import sys
import time

try:
    import fcntl
except ImportError:  # non-POSIX: run without locking
    fcntl = None

HOME = os.path.expanduser("~")
CONFIG_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
DATA_DIR = os.environ.get("CLAUDE_STATUSLINE_DIR") or os.path.join(CONFIG_DIR, "statusline")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
FX_FILE = os.path.join(DATA_DIR, "fx.json")
LOCK_FILE = os.path.join(DATA_DIR, "lock")
DEBUG_FLAG = os.path.join(DATA_DIR, "debug")

FX_ENV = "CLAUDE_STATUSLINE_USD_EUR"
FX_FALLBACK = 0.89            # used only until the first successful fetch
FX_MAX_AGE = 12 * 3600
FX_RETRY = 30 * 60
ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"

RESET_COMMAND = "/limit-reset"
MIN_CAL_PCT = 2.0             # percentage points observed before showing euro estimates
SESSION_TTL = 8 * 86400
WINDOWS = [("five_hour", "5h"), ("seven_day", "7d")]

# USD per million tokens (input, output), fast-mode price multiplier, Claude Code's
# default effort, and its relative cost per effort level (high = 1). Prices are
# Anthropic list prices; the effort index is from the Claude Code 2.1.322 model
# catalog, which has none for the 5.2 models.
_E = lambda lo, me, xh, mx: {"low": lo, "medium": me, "high": 1.0, "xhigh": xh, "max": mx}
MODELS = {
    "claude-fable-5-2":  (8.0, 40.0, 2, "medium", None),
    "claude-mythos-5-2": (8.0, 40.0, 2, "medium", None),
    "claude-fable-5-1":  (10.0, 50.0, None, "high", _E(0.75, 0.86, 1.38, 1.74)),
    "claude-mythos-5-1": (10.0, 50.0, None, "high", _E(0.75, 0.86, 1.38, 1.74)),
    "claude-fable-5":    (10.0, 50.0, None, "high", _E(0.60, 0.77, 1.74, 1.91)),
    "claude-opus-5-5":   (4.0, 20.0, 2, "medium", _E(0.48, 0.66, 1.66, 3.44)),
    "claude-opus-5":     (5.0, 25.0, 2, "high", _E(0.67, 0.76, 1.60, 1.70)),
    "claude-opus-4-8":   (5.0, 25.0, 2, "high", _E(0.72, 0.90, 1.65, 1.88)),
    "claude-opus-4-7":   (5.0, 25.0, None, None, None),
    "claude-opus-4-6":   (5.0, 25.0, None, None, None),
    "claude-sonnet-5-5": (2.0, 10.0, None, "medium", _E(0.49, 0.54, 1.87, 5.50)),
    "claude-sonnet-5":   (2.0, 10.0, None, "high", _E(0.47, 0.74, 2.41, 5.59)),
    "claude-sonnet-4-6": (3.0, 15.0, None, None, None),
    "claude-haiku-5-5":  (0.10, 0.50, None, "medium", _E(0.38, 0.52, 2.03, 4.34)),
    "claude-haiku-4-5":  (1.0, 5.0, None, None, None),
}

NO_COLOR = bool(os.environ.get("NO_COLOR"))
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


# ---------------------------------------------------------------- small helpers

def paint(code, text):
    return text if NO_COLOR else "\033[%sm%s\033[0m" % (code, text)


def dim(text):
    return paint("2", text)


def num(value, default=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path, data):
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def vislen(text):
    return len(ANSI_RE.sub("", text))


def eur(usd, fx, approx=False):
    value = usd * fx
    if value >= 10:
        text = "%.0f" % value
    elif approx and value >= 1:
        text = "%.1f" % value
    else:
        text = "%.2f" % value
    return ("~" if approx else "") + "€" + text


def pct_text(value):
    return "<1%" if 0 < value < 0.5 else "%d%%" % round(value)


def duration(seconds):
    minutes = max(0, int(seconds)) // 60
    if minutes < 1:
        return "<1m"
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    if days:
        return "%dd%dh" % (days, hours)
    if hours:
        return "%dh%02dm" % (hours, mins)
    return "%dm" % mins


# ------------------------------------------------------------------- currency

def update_fx():
    """Fetch the ECB USD reference rate. Runs detached via --update-fx."""
    import urllib.request
    data = read_json(FX_FILE)
    try:
        req = urllib.request.Request(ECB_URL, headers={"User-Agent": "claude-statusline"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            xml = resp.read().decode("utf-8", "replace")
        rate = re.search(r"currency=['\"]USD['\"]\s+rate=['\"]([0-9.]+)['\"]", xml)
        day = re.search(r"time=['\"]([0-9-]+)['\"]", xml)
        usd_per_eur = float(rate.group(1))
        if not 0.5 < usd_per_eur < 2.5:
            raise ValueError("implausible rate %r" % usd_per_eur)
        data.update(usd_to_eur=round(1 / usd_per_eur, 6), date=day.group(1) if day else None,
                    fetched_at=int(time.time()), source="ECB")
        data.pop("error", None)
    except Exception as exc:  # offline, blocked, format change: keep the old rate
        data["error"] = str(exc)[:200]
    data["attempted_at"] = int(time.time())
    write_json(FX_FILE, data)


def load_fx(now):
    fixed = os.environ.get(FX_ENV)
    if fixed:
        try:
            value = float(fixed.replace(",", "."))
            if 0.2 < value < 5:
                return value
        except ValueError:
            pass
    data = read_json(FX_FILE)
    rate = num(data.get("usd_to_eur"))
    stale = rate is None or now - num(data.get("fetched_at"), 0) > FX_MAX_AGE
    if stale and now - num(data.get("attempted_at"), 0) > FX_RETRY:
        data["attempted_at"] = int(now)       # claim the attempt before spawning
        write_json(FX_FILE, data)
        try:
            import subprocess
            subprocess.Popen([sys.executable or "python3", os.path.abspath(__file__), "--update-fx"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        except Exception:
            pass
    return rate if rate and 0.2 < rate < 5 else FX_FALLBACK


# ------------------------------------------------------------ state / learning

def track(state, data, now):
    """Record this session's cost delta; learn USD per limit percentage point."""
    sessions = state.setdefault("sessions", {})
    sid = str(data.get("session_id") or "unknown")
    cost_now = num((data.get("cost") or {}).get("total_cost_usd"), 0.0)
    known = sessions.get(sid)
    if isinstance(known, dict):
        delta = max(0.0, cost_now - num(known.get("cost"), 0.0))
        stamp = int(now) if delta or now - num(known.get("ts"), 0) > 300 else known.get("ts")
    else:
        delta, stamp = 0.0, int(now)          # first sight: history unknown, start here
    sessions[sid] = {"cost": cost_now, "ts": stamp}
    for old in [k for k, v in sessions.items()
                if not isinstance(v, dict) or now - num(v.get("ts"), 0) > SESSION_TTL]:
        del sessions[old]

    limits = data.get("rate_limits") or {}
    wins = state.setdefault("win", {})
    cal = state.setdefault("cal", {})
    for name, _label in WINDOWS:
        cur = limits.get(name)
        pct = num(cur.get("used_percentage")) if isinstance(cur, dict) else None
        if pct is None:
            continue
        resets_at = num(cur.get("resets_at"))
        prev = wins.get(name)
        same_window = (isinstance(prev, dict) and resets_at is not None
                       and num(prev.get("resets_at")) is not None
                       and abs(resets_at - prev["resets_at"]) <= 120
                       and pct >= num(prev.get("pct"), 0.0) - 0.01)
        if not same_window:
            # New window (or a manual reset). Intervals only count once aligned to a
            # percentage tick, unless we are watching from the very start.
            prev = {"pct": pct, "resets_at": resets_at, "pending": 0.0, "synced": pct < 0.5, "seen": int(now)}
        else:
            prev["pending"] = num(prev.get("pending"), 0.0) + delta
            if pct > num(prev.get("pct"), 0.0) + 1e-9:
                if prev.get("synced") and prev["pending"] > 0:
                    entry = cal.setdefault(name, {"usd": 0.0, "pct": 0.0})
                    entry["usd"] = num(entry.get("usd"), 0.0) + prev["pending"]
                    entry["pct"] = num(entry.get("pct"), 0.0) + (pct - prev["pct"])
                    if entry["pct"] > 200:                 # forget old behaviour slowly
                        entry["usd"] /= 2
                        entry["pct"] /= 2
                # pending == 0 means the limit moved without local spend (claude.ai,
                # another device): skip it rather than dilute the ratio.
                prev.update(pending=0.0, synced=True, pct=pct, seen=int(now))
            elif now - num(prev.get("seen"), 0) > 60:
                prev["seen"] = int(now)
        wins[name] = prev


def update_state(data, now):
    lock = None
    try:
        if fcntl is not None:
            lock = open(LOCK_FILE, "w")
            for _ in range(25):
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    time.sleep(0.01)
            else:
                return read_json(STATE_FILE)               # busy: show, do not record
        state = read_json(STATE_FILE)
        before = json.dumps(state, sort_keys=True)
        track(state, data, now)
        if json.dumps(state, sort_keys=True) != before:
            write_json(STATE_FILE, state)
        return state
    finally:
        if lock is not None:
            lock.close()


def usd_per_point(state, name):
    entry = (state.get("cal") or {}).get(name)
    if not isinstance(entry, dict):
        return None
    usd, pct = num(entry.get("usd"), 0.0), num(entry.get("pct"), 0.0)
    return usd / pct if pct >= MIN_CAL_PCT and usd > 0 else None


# ---------------------------------------------------------------- manual reset

def manual_reset_available(now):
    """True while Claude Code's own cache says an unused limit reset is held."""
    paths = [os.path.join(HOME, ".claude.json")]
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        paths.insert(0, os.path.join(os.environ["CLAUDE_CONFIG_DIR"], ".claude.json"))
    cfg = {}
    for path in paths:
        cfg = read_json(path)
        if cfg:
            break
    cache = cfg.get("unusedLimitResetStatusCache")
    if not isinstance(cache, dict) or cache.get("holdsUnusedReset") is not True:
        return False
    account = (cfg.get("oauthAccount") or {}).get("accountUuid")
    now_ms = now * 1000
    checked, valid_until = num(cache.get("checkedAt"), 0), num(cache.get("validUntil"), 0)
    return (checked <= now_ms and now_ms - checked < 3 * 86400 * 1000 and now_ms < valid_until
            and (not account or cache.get("accountUuid") == account))


# -------------------------------------------------------------------- rendering

def model_id(data):
    mid = str((data.get("model") or {}).get("id") or "").lower()
    mid = re.sub(r"\[[^\]]*\]$", "", mid)                    # claude-fable-5-2[1m]
    mid = re.sub(r"^(?:[a-z]{2,6}\.)?anthropic\.", "", mid)  # cloud provider prefixes
    mid = re.sub(r"-v\d+(?::\d+)?$", "", mid)
    mid = re.sub(r"[-@]20\d{6}$", "", mid)                   # dated snapshots
    return mid


def severity(used):
    return "32" if used < 50 else "33" if used < 80 else "31"


def bar(used, width):
    filled = min(width, max(0, int(used / 100.0 * width + 0.5)))
    return "█" * filled + "░" * (width - filled)


def window_view(name, data, state, now):
    cur = (data.get("rate_limits") or {}).get(name)
    if isinstance(cur, dict) and num(cur.get("used_percentage")) is not None:
        return {"pct": num(cur["used_percentage"]), "resets_at": num(cur.get("resets_at"))}
    prev = (state.get("win") or {}).get(name)
    if isinstance(prev, dict) and num(prev.get("pct")) is not None:
        if num(prev.get("resets_at"), 0) > now:               # last seen, still current
            return {"pct": prev["pct"], "resets_at": prev["resets_at"]}
        return {"pct": 0.0, "resets_at": None}                # that window has ended
    return None


def spend_view(data):
    """Claude apps gateway spend limit: real dollar amounts, no estimate needed."""
    cur = (data.get("rate_limits") or {}).get("spend_limit")
    if not isinstance(cur, dict) or num(cur.get("used_percentage")) is None:
        return None
    view = {"pct": num(cur["used_percentage"]), "resets_at": num(cur.get("resets_at"))}
    used, limit = num(cur.get("used_usd")), num(cur.get("limit_usd"))
    if used is not None and limit is not None:
        view["left_usd"] = max(0.0, limit - used)
    return view


def window_segment(label, view, ratio, fx, now, level):
    width = 10 if level < 2 else 6
    name = paint("1", label)
    if view is None:                                          # nothing known yet
        return name + " " + dim(bar(0, width) + " –")
    used = min(100.0, max(0.0, view["pct"]))
    text = name + " " + paint(severity(used), bar(used, width) + " " + pct_text(used))
    if level < 4:
        if view.get("left_usd") is not None:
            text += " " + eur(view["left_usd"], fx) + dim(" left")
        elif ratio:
            text += " " + eur((100.0 - used) * ratio, fx, True) + dim(" left")
    if view.get("resets_at"):
        text += " " + dim("↻") + duration(view["resets_at"] - now)
    return text


def model_segment(data, fx, level):
    model = data.get("model") or {}
    mid = model_id(data)
    name = model.get("display_name") or mid
    if not name or level >= 3:
        return None
    info = MODELS.get(mid)
    fast = bool(data.get("fast_mode"))
    parts = [paint("1;35", str(name))]
    if fast:
        parts.append(paint("33", "fast"))
    effort = (data.get("effort") or {}).get("level")
    if effort:
        text = str(effort)
        index, base = (info[4], info[3]) if info else (None, None)
        if index and base and effort != base and index.get(effort) and index.get(base):
            factor = ("%.1f" % (index[effort] / index[base])).rstrip("0").rstrip(".")
            text += dim(" ~%s×" % factor)                     # cost relative to default effort
        parts.append(text)
    if info and level < 1:
        mult = (info[2] or 1) if fast else 1
        parts.append("€%.2f/€%.2f" % (info[0] * mult * fx, info[1] * mult * fx))
    return " ".join(parts)


def render(data, state, fx, now, budget):
    views = [(label, window_view(name, data, state, now), usd_per_point(state, name))
             for name, label in WINDOWS]
    spend = spend_view(data)
    if spend:
        if all(view is None for _label, view, _ratio in views):
            views = []                                        # gateway user: no plan limits
        views.append(("spend", spend, None))
    reset = manual_reset_available(now)
    line = ""
    # Each level is narrower: 1 no prices, 2 short bars, 3 no model, 4 no euro, 5 no reset hint.
    for level in range(6):
        segs = [window_segment(label, view, ratio, fx, now, level) for label, view, ratio in views]
        if reset and level < 5:
            segs[0] += " " + paint("1;36", RESET_COMMAND)
        extra = model_segment(data, fx, level)
        if extra:
            segs.append(extra)
        line = dim(" │ ").join(segs)
        if vislen(line) <= budget:
            break
    return line


# ------------------------------------------------------------------------ main

def main():
    if "--update-fx" in sys.argv[1:]:
        os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
        update_fx()
        return
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    now = time.time()
    os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
    if os.path.exists(DEBUG_FLAG):
        try:
            with open(os.path.join(DATA_DIR, "last-input.json"), "w", encoding="utf-8") as fh:
                fh.write(raw)
        except OSError:
            pass
    try:  # temp files left behind when a run is cancelled mid-write
        for entry in os.listdir(DATA_DIR):
            path = os.path.join(DATA_DIR, entry)
            if entry.endswith(".tmp") and now - os.path.getmtime(path) > 3600:
                os.unlink(path)
    except OSError:
        pass
    fx = load_fx(now)
    state = update_state(data, now)
    try:
        columns = int(os.environ.get("COLUMNS") or 0)
    except ValueError:
        columns = 0
    budget = (columns - 4) if columns >= 30 else 200
    print(render(data, state, fx, now, budget))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        main()
    except Exception:
        # Never leave the status line blank: log the failure and say where.
        try:
            import traceback
            os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
            with open(os.path.join(DATA_DIR, "error.log"), "w", encoding="utf-8") as fh:
                fh.write(time.strftime("%Y-%m-%d %H:%M:%S\n") + traceback.format_exc())
        except Exception:
            pass
        print("statusline error, see " + os.path.join(DATA_DIR, "error.log"))
