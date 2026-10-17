# A plain-text report of what the plugin sees, for when game art or FPS doesn't show up.
# Written from the Quick Access panel while a game runs; read-only apart from a
# one-second FPS sample (the same one Show FPS takes).

import ctypes
import glob
import json
import os
import re
import time

from . import __version__, fps, render

REPORT_PATH = os.path.join(os.path.expanduser("~deck"), "pixel-faceplate-diagnostics.txt")
MSG_COPY = 0o40000  # peek at a queued message by index without taking it


def _read(path, limit=65536):
    try:
        with open(path, "rb") as handle:
            return handle.read(limit)
    except OSError as error:
        return error


def _launches(proc_root="/proc"):
    """Steam's game launches (reaper SteamLaunch): pid, AppId= argument, Steam's IDs from the environment."""
    found = []
    for entry in os.listdir(proc_root):
        if not entry.isdigit():
            continue
        cmdline = _read(os.path.join(proc_root, entry, "cmdline"), 4096)
        if isinstance(cmdline, Exception) or b"SteamLaunch" not in cmdline.split(b"\0"):
            continue
        args = cmdline.split(b"\0")
        appid_arg = next((a.decode(errors="replace") for a in args if a.startswith(b"AppId=")), "(no AppId=)")
        env = _read(os.path.join(proc_root, entry, "environ"))
        ids = {}
        if isinstance(env, Exception):
            ids["environ"] = "unreadable: %s" % env
        else:
            for item in env.split(b"\0"):
                key, _, value = item.partition(b"=")
                if key in (b"SteamGameId", b"SteamAppId", b"SteamOverlayGameId"):
                    ids[key.decode()] = value.decode(errors="replace")
        game = " ".join(a.decode(errors="replace") for a in args[args.index(b"--") + 1:][:2]) if b"--" in args else ""
        found.append((int(entry), appid_arg, ids, game[:160]))
    return sorted(found, reverse=True)


def _processes(names, proc_root="/proc"):
    out = []
    for entry in os.listdir(proc_root):
        if not entry.isdigit():
            continue
        comm = _read(os.path.join(proc_root, entry, "comm"), 64)
        if isinstance(comm, Exception) or comm.strip().decode(errors="replace") not in names:
            continue
        try:
            cwd = os.readlink(os.path.join(proc_root, entry, "cwd"))
        except OSError as error:
            cwd = "unreadable: %s" % error.strerror
        try:
            uid = os.stat(os.path.join(proc_root, entry)).st_uid
        except OSError:
            uid = "?"
        out.append("%s pid %s uid %s cwd %s" % (comm.strip().decode(errors="replace"), entry, uid, cwd))
    return out


def _grid_report(root, appids):
    lines = []
    users = _read(os.path.join(root, "config", "loginusers.vdf"))
    if isinstance(users, Exception):
        lines.append("loginusers.vdf: %s" % users)
    else:
        for match in re.finditer(r'"(\d{16,20})"\s*\{([^{}]*)\}', users.decode(errors="replace")):
            recent = re.search(r'"MostRecent"\s*"(\d)"', match.group(2), re.IGNORECASE)
            lines.append("account %s (userdata %d) MostRecent=%s" % (
                match.group(1), int(match.group(1)) & 0xFFFFFFFF, recent.group(1) if recent else "missing"))
    for grid in sorted(glob.glob(os.path.join(root, "userdata", "*", "config", "grid"))):
        try:
            names = sorted(os.listdir(grid))
        except OSError as error:
            lines.append("%s: %s" % (grid, error))
            continue
        lines.append("%s: %d files" % (grid, len(names)))
        for appid in appids:
            mine = [n for n in names if n.startswith(str(appid))]
            lines.append("  for %d: %s" % (appid, ", ".join(mine) or "nothing"))
        shortcut_like = [n for n in names if re.match(r"\d+", n) and int(re.match(r"\d+", n).group()) >= 0x80000000]
        lines.append("  shortcut-looking names (first 8): %s" % (", ".join(shortcut_like[:8]) or "none"))
    return lines


def _queue_report(feed):
    lines = []
    table = _read("/proc/sysvipc/msg")
    lines.append("/proc/sysvipc/msg:")
    lines += ["  " + line for line in (str(table) if isinstance(table, Exception) else table.decode()).splitlines()]
    lines += ["process: " + p for p in _processes(("gamescope", "gamescope-wl", "mangoapp"))] or ["no gamescope or mangoapp process"]
    try:
        opened = feed._open()
    except OSError as error:
        opened = False
        lines.append("open failed: %s" % error)
    lines.append("our key %s, queue id %s" % (feed.key, feed.queue))
    if opened:
        lib = feed._lib()
        for index in range(10):
            message = fps._Message()
            size = lib.msgrcv(feed.queue, ctypes.byref(message), ctypes.sizeof(message.body), index,
                              fps.IPC_NOWAIT | fps.MSG_NOERROR | MSG_COPY)
            if size < 0:
                lines.append("peek %d: nothing (errno %d)" % (index, ctypes.get_errno()))
                break
            lines.append("peek %d: type %d, %d bytes, %s" % (
                index, message.mtype, size, fps.parse(bytes(message.body[:size]))))
    value = feed.sample(1.0)
    lines.append("1 s sample: %s FPS, %s" % (value, json.dumps(feed.stats, default=str)))
    return lines, value


def write_report(service, frontend):
    """Write the report next to the user's home and return a short summary for the panel."""
    lines = ["Pixel Faceplate %s diagnostics, %s, plugin uid %d" % (
        __version__, time.strftime("%Y-%m-%d %H:%M:%S"), os.getuid())]
    status = service.status()
    settings = status.pop("settings")
    lines.append("status: " + json.dumps(status, default=str))
    lines.append("settings: mode=%s fps_overlay=%s fps_interval=%s" % (
        settings.get("mode"), settings.get("fps_overlay"), settings.get("fps_interval")))

    lines.append("")
    lines.append("== Which game is running")
    lines.append("frontend: " + json.dumps(frontend, default=str))
    lines.append("reported by frontend: %d, from /proc: %d" % (service.reported_app, render.running_appid()))
    launches = _launches()
    for pid, appid_arg, ids, game in launches[:4]:
        lines.append("launch pid %d %s %s %s" % (pid, appid_arg, ids, game))
    if not launches:
        lines.append("no SteamLaunch process")

    candidates = []
    for value in [service.appid, service.reported_app, render.running_appid(),
                  (frontend or {}).get("main_running_appid")] + [
                      render.normalize_appid(ids.get("SteamGameId")) for _, _, ids, _ in launches[:2]]:
        appid = render.normalize_appid(value)
        if appid and appid not in candidates:
            candidates.append(appid)

    lines.append("")
    lines.append("== Artwork")
    for appid in candidates:
        lines.append("app %d (%s): grid dirs %s" % (
            appid, "non-Steam" if appid & render.SHORTCUT_BIT else "Steam", render.grid_dirs(render.STEAM_ROOT, appid)))
        for kind in ("hero", "logo", "capsule", "header"):
            lines.append("  %s: %s" % (kind, render.find_art(appid, kind)))
    lines += _grid_report(render.STEAM_ROOT, candidates)

    lines.append("")
    lines.append("== FPS")
    lines.append("sampler: enabled=%s appid=%s value=%s next in %.0f s, last stats %s" % (
        service.fps.enabled, service.fps.appid, service.fps.value,
        service.fps.next_at - time.monotonic(), json.dumps(getattr(service.fps.feed, "stats", {}), default=str)))
    queue_lines, sampled = _queue_report(fps.MangoappFeed())
    lines += queue_lines

    with open(REPORT_PATH, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    shown = candidates[0] if candidates else 0
    art = render.find_art(shown, "hero") or render.find_art(shown, "capsule") if shown else None
    return {
        "path": REPORT_PATH,
        "summary": "Game: %s. Art: %s. FPS sample: %s." % (
            shown or "none found", "found" if art else "none", sampled if sampled is not None else "nothing"),
    }
