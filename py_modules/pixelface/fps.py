# The running game's frame rate, sampled now and then from gamescope's feed to mangoapp.
#
# On SteamOS, gamescope sends mangoapp (Steam's performance overlay) one System V
# message per game frame with that frame's time. Reading the queue briefly gives
# the same numbers the overlay shows. A message goes to whichever reader takes it
# first, so this only listens for about a second per sample: the overlay misses a
# few frames' worth of graph, nothing else. Layout from gamescope's mangoapp.cpp
# (struct mangoapp_msg_v1, packed); only fields that have been there since v1 are read.

import ctypes
import ctypes.util
import errno
import os
import struct
import threading
import time

IPC_NOWAIT = 0o4000
MSG_NOERROR = 0o10000
# Legacy single-output message type, then the first per-connector one.
FRAME_TYPES = (1, 100)
# Offsets inside the message body (after the C long mtype).
OFF_VERSION = 0
OFF_APP_FRAMETIME = 8
OFF_VISIBLE_FRAMETIME = 18
OFF_REFRESH = 42
OFF_FLAGS = 44  # bit 0 app wants HDR, bit 1 Steam (not the game) has focus
MIN_BODY = OFF_FLAGS + 1
# Fewer frames than this in a sample isn't a frame rate worth showing.
MIN_FRAMES = 5


class _Message(ctypes.Structure):
    _fields_ = [("mtype", ctypes.c_long), ("body", ctypes.c_ubyte * 1016)]


NO_VALUE = 0xFFFFFFFFFFFFFFFF


def parse(body):
    """(app frame time, visible frame time (ns, None if unset), refresh Hz, Steam focused), or None.

    gamescope sends two kinds of message: one per game frame (app frame time
    set, visible unset) and one per new frame on screen (the other way round).
    """
    if len(body) < MIN_BODY:
        return None
    (version,) = struct.unpack_from("<I", body, OFF_VERSION)
    if version < 1:
        return None
    (app,) = struct.unpack_from("<Q", body, OFF_APP_FRAMETIME)
    (visible,) = struct.unpack_from("<Q", body, OFF_VISIBLE_FRAMETIME)
    (refresh,) = struct.unpack_from("<H", body, OFF_REFRESH)
    steam_focused = bool(body[OFF_FLAGS] & 0x02)
    return (None if app == NO_VALUE else app, None if visible == NO_VALUE else visible,
            refresh, steam_focused)


def frame_rate(frametimes):
    """Frames per second from a list of frame times in ns (median, so hitches don't count), or None."""
    usable = sorted(t for t in frametimes if 1_000_000 <= t <= 1_000_000_000)  # 1000 to 1 fps
    if len(usable) < MIN_FRAMES:
        return None
    return int(round(1e9 / usable[len(usable) // 2]))


def _gamescope_cwd(proc_root="/proc"):
    """Working directory of a running mangoapp or gamescope, for ftok's relative path."""
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return None
    for want in ("mangoapp", "gamescope", "gamescope-wl"):
        for entry in entries:
            if not entry.isdigit():
                continue
            try:
                with open(os.path.join(proc_root, entry, "comm"), "r") as handle:
                    if handle.read().strip() != want:
                        continue
                return os.readlink(os.path.join(proc_root, entry, "cwd"))
            except OSError:
                continue
    return None


class MangoappFeed:
    """Opens the queue lazily; every call is safe when there is no gamescope at all."""

    def __init__(self, libc=None, proc_root="/proc"):
        self._libc = libc
        self._proc_root = proc_root
        self.queue = -1
        self.key = None
        # What the last sample saw, for the diagnostics report.
        self.stats = {}
        # Frame types with a reader thread still waiting in msgrcv.
        self._readers = set()
        self._readers_lock = threading.Lock()

    def _lib(self):
        if self._libc is None:
            lib = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
            lib.ftok.argtypes = [ctypes.c_char_p, ctypes.c_int]
            lib.ftok.restype = ctypes.c_int
            lib.msgget.argtypes = [ctypes.c_int, ctypes.c_int]
            lib.msgget.restype = ctypes.c_int
            lib.msgrcv.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_long, ctypes.c_int]
            lib.msgrcv.restype = ctypes.c_ssize_t
            self._libc = lib
        return self._libc

    def _open(self):
        if self.queue >= 0:
            return True
        lib = self._lib()
        # gamescope and mangoapp both call ftok("mangoapp", 65) relative to their
        # own working directory; with no such file there it's -1, a fixed key.
        cwd = _gamescope_cwd(self._proc_root)
        key = lib.ftok(os.path.join(cwd, "mangoapp").encode(), 65) if cwd else -1
        self.key = key
        self.queue = lib.msgget(key, 0)  # never create one
        if self.queue < 0:
            self.stats = {"error": "no mangoapp queue for key %d (errno %d)" % (key, ctypes.get_errno())}
        return self.queue >= 0

    def _blocking_reader(self, mtype, sink, deadline):
        """Wait in line for frames of one type until the deadline (runs in its own thread).

        mangoapp sleeps in msgrcv, and the kernel hands a new message straight to
        a waiting receiver instead of queueing it, so a reader that only polls
        never sees one. Waiting too puts this reader in the same line: frames
        alternate between the two for the sample. The thread can outlive the
        sample by one frame (or until the next one, if the game stops drawing).
        """
        lib = self._lib()
        try:
            while True:
                message = _Message()
                size = lib.msgrcv(self.queue, ctypes.byref(message), ctypes.sizeof(message.body), mtype, MSG_NOERROR)
                if size < 0:
                    if ctypes.get_errno() == errno.EINTR:
                        continue
                    return
                if time.monotonic() > deadline:
                    return
                sink.append((mtype, bytes(message.body[:size])))
        finally:
            with self._readers_lock:
                self._readers.discard(mtype)

    def sample(self, seconds=1.0):
        """The game's frame rate over about `seconds`, or None (no game frames, no gamescope)."""
        try:
            if not self._open():
                return None
            stats = {"queue": self.queue, "key": self.key, "by_type": {}, "steam_focused": 0, "unparsed": 0}
            received = []
            deadline = time.monotonic() + seconds
            for mtype in FRAME_TYPES:
                with self._readers_lock:
                    if mtype in self._readers:
                        continue  # still waiting from last time (nothing of that type came)
                    self._readers.add(mtype)
                threading.Thread(target=self._blocking_reader, args=(mtype, received, deadline),
                                 name="pixel-faceplate-fps-%d" % mtype, daemon=True).start()
            time.sleep(seconds + 0.05)
            app_times, visible_times = [], []
            for mtype, body in list(received):
                stats["by_type"][mtype] = stats["by_type"].get(mtype, 0) + 1
                parsed = parse(body)
                if not parsed:
                    stats["unparsed"] += 1
                elif parsed[3]:
                    stats["steam_focused"] += 1
                else:
                    if parsed[0] is not None:
                        app_times.append(parsed[0])
                    if parsed[1] is not None:
                        visible_times.append(parsed[1])
            # The game's own frames first; frames reaching the screen if those are missing.
            result = frame_rate(app_times)
            if result is None:
                result = frame_rate(visible_times)
            stats.update(app_frames=len(app_times), visible_frames=len(visible_times), fps=result)
            self.stats = stats
            return result
        except (OSError, AttributeError, ValueError) as error:
            self.stats = {"error": str(error)}
            return None
