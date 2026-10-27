"""
Sony HT-B600 (BRAVIA Theatre Bar 6) BLE Volume Controller
==========================================================
Controls and reads volume via BLE GATT on Windows.
Supports Daemon mode for instant, zero-latency control.

Usage:
  python bravia_control.py daemon           Start the background daemon
  python bravia_control.py set <volume>     Set volume (0-100)
  python bravia_control.py up [step]        Volume up (default step=1)
  python bravia_control.py down [step]      Volume down (default step=1)
  python bravia_control.py status           Show current volume

If the daemon is running, commands are sent to it via TCP (instant).
If not, commands will connect to the soundbar directly (takes 3-5s).
"""
import asyncio
import sys
import os

# The scheduled task launches a bare interpreter with no venv activation, so the project's
# site-packages has to be added here, before any third-party import. site.addsitedir() is used
# rather than a PYTHONPATH entry because only a real site directory gets its .pth files
# processed - and pywin32 publishes its win32/ and win32/lib/ folders through pywin32.pth.
# Without that, "import win32gui" fails and the OSD silently loses its click-through styling.
_SITE_PACKAGES = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv", "Lib", "site-packages")
if os.path.isdir(_SITE_PACKAGES):
    import site
    site.addsitedir(_SITE_PACKAGES)

import socket
import subprocess
import tkinter as tk
import threading
import queue
import time
from bleak import BleakClient, BleakScanner

DEVICE_MAC = "54:80:fc:3e:f7:67"
WRITE_CHAR = "5b833c66-6bc7-4802-8e9a-723ceca4bd8f"
NOTIFY_CHAR = "5b833c67-6bc7-4802-8e9a-723ceca4bd8f"
MAX_VOL = 100
MIN_VOL = 0
DAEMON_PORT = 9105

# Perfectly crafted 17-byte volume query with valid checksum
# This completely replaces the 1.2KB MASSIVE_QUERY from the iOS App,
# preventing the Soundbar from muting due to Bluetooth processor overload.
MINI_VOL_QUERY = bytes.fromhex("3e070000000010000001000a025661db3c")

# Transaction control packets: TX3 acknowledges, TX4 closes the soundbar's state machine
TX_ACK = bytes([0x3e, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x02, 0x3c])
TX_CLOSE = bytes([0x3e, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01, 0x3c])

# Watchdog tuning
WRITE_TIMEOUT = 3.0             # A wedged Windows BLE stack blocks writes forever without this
CONNECT_TIMEOUT = 15.0
COMMAND_TIMEOUT = 9.0           # Must stay below the CLI's 10s socket read timeout
HEARTBEAT_INTERVAL = 30.0       # Only probes after the link has been silent this long
HEARTBEAT_REPLY_TIMEOUT = 2.0   # Caps how long a probe can delay a user command
HEARTBEAT_MAX_MISSES = 2        # Consecutive silent probes before forcing a reconnect
STALE_SERVICE_MAX_TRIES = 4     # Service-less connects tolerated before respawning the process
RESTART_MIN_INTERVAL = 600      # Never respawn more often than this (guards against a restart loop)

SCRIPT_PATH = os.path.abspath(__file__)
SCRIPT_DIR = os.path.dirname(SCRIPT_PATH)
LOG_PATH = os.path.join(SCRIPT_DIR, "bravia_daemon.log")
LOG_MAX_BYTES = 512 * 1024
RESTART_STAMP = os.path.join(SCRIPT_DIR, ".daemon_restart")


class StaleServicesError(Exception):
    """Windows returned a truncated GATT service list (no Sony service)."""


class DaemonLog:
    """Timestamped stdout replacement.

    Under pythonw.exe sys.stdout is None and print() silently discards everything,
    which is why the daemon's historical failures left no evidence at all. Installing
    this as sys.stdout routes every existing print() into a file instead.
    """

    def __init__(self, path, mirror=None):
        self.path = path
        self.mirror = mirror   # Real console, when there is one (foreground debugging)
        self.buf = ""

    def write(self, text):
        if self.mirror is not None:
            try:
                self.mirror.write(text)
                self.mirror.flush()
            except Exception:
                pass
        self.buf += text
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self._emit(line)

    def _emit(self, line):
        try:
            # Truncate rather than rotate: this is a diagnostic tail, not an audit trail.
            if os.path.exists(self.path) and os.path.getsize(self.path) > LOG_MAX_BYTES:
                os.replace(self.path, self.path + ".old")
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(f"[{stamp}] {line}\n")
        except Exception:
            pass  # Logging must never take the daemon down

    def flush(self):
        if self.buf:
            self._emit(self.buf)
            self.buf = ""

# ==========================================
# Direct Connection Logic (Slow Fallback)
# ==========================================

async def find_device():
    device = await BleakScanner.find_device_by_address(DEVICE_MAC, timeout=5.0)
    if not device:
        devices = await BleakScanner.discover(timeout=5.0)
        for d in devices:
            if d.name and "BRAVIA" in d.name:
                return d
    return device


async def direct_get_volume() -> int:
    device = await find_device()
    if not device: return None
    try:
        async with BleakClient(device.address) as client:
            all_notifs = bytearray()
            def handler(sender, data): all_notifs.extend(data)
            await client.start_notify(NOTIFY_CHAR, handler)
            await asyncio.sleep(0.2)
            await client.write_gatt_char(WRITE_CHAR, MINI_VOL_QUERY, response=False)
            
            last_len = 0
            for _ in range(20):
                await asyncio.sleep(0.1)
                if len(all_notifs) > 0 and len(all_notifs) == last_len: break
                last_len = len(all_notifs)
            
            hex_str = all_notifs.hex()
            target = "0a025661120208"
            idx = hex_str.find(target)
            if idx != -1 and len(hex_str) >= idx + 16:
                return int(hex_str[idx + 14: idx + 16], 16)
            return None
    except: return None


async def direct_set_volume(vol: int) -> bool:
    if vol < MIN_VOL or vol > MAX_VOL: return False
    device = await find_device()
    if not device: return False
    try:
        async with BleakClient(device.address) as client:
            header = bytes([0x3e, 0x07, 0x00, 0x00, 0x00, 0x00, 0x10, 0x00, 0x00, 0x02,
                            0x02, 0x0a, 0x0a, 0x0a, 0x02, 0x56, 0x61, 0x10, 0x01, 0x22])
            vv = vol & 0xFF
            cc = (vv & 0xFE) + 0x30
            cmd = bytes([0x02, 0x08, vv, cc, 0x3c])
            # TX1 & TX2
            await client.write_gatt_char(WRITE_CHAR, header, response=False)
            await asyncio.sleep(0.02)
            await client.write_gatt_char(WRITE_CHAR, cmd, response=False)
            
            # Wait for RX1 & RX2
            await asyncio.sleep(0.2)
            
            # TX3: iOS Echo ACK
            tx3 = bytes([0x3e, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x02, 0x3c])
            await client.write_gatt_char(WRITE_CHAR, tx3, response=False)
            
            # Wait for RX3 & RX4
            await asyncio.sleep(0.2)
            
            # TX4: Close/End connection gracefully
            tx4 = bytes([0x3e, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01, 0x3c])
            await client.write_gatt_char(WRITE_CHAR, tx4, response=False)
            
            await asyncio.sleep(0.2)
            return True
    except: return False

async def direct_change_volume(action: str, step: int):
    device = await find_device()
    if not device: return None
    try:
        async with BleakClient(device.address) as client:
            all_notifs = bytearray()
            def handler(sender, data): all_notifs.extend(data)
            await client.start_notify(NOTIFY_CHAR, handler)
            
            # Windows cache workaround
            await asyncio.sleep(1.0)
            try:
                await client.clear_cache()
            except: pass
            _ = client.services
            
            # Step 1: Fetch Current Volume
            print("Reading current volume...")
            await client.write_gatt_char(WRITE_CHAR, MINI_VOL_QUERY, response=False)
            
            target = "0a025661120208"
            current = None
            for _ in range(20):
                await asyncio.sleep(0.1)
                hex_str = all_notifs.hex()
                idx = hex_str.find(target)
                if idx != -1 and len(hex_str) >= idx + 16:
                    current = int(hex_str[idx + 14: idx + 16], 16)
                    break
            
            if current is None:
                return None
                
            new_vol = min(MAX_VOL, current + step) if action == "up" else max(MIN_VOL, current - step)
            if new_vol == current:
                print(f"Already at limit ({new_vol})")
                return current
                
            # Step 2: Set Volume (Full 5-Step Protocol)
            header = bytes([0x3e, 0x07, 0x00, 0x00, 0x00, 0x00, 0x10, 0x00, 0x00, 0x02,
                            0x02, 0x0a, 0x0a, 0x0a, 0x02, 0x56, 0x61, 0x10, 0x01, 0x22])
            vv = new_vol & 0xFF
            cc = (vv & 0xFE) + 0x30
            cmd = bytes([0x02, 0x08, vv, cc, 0x3c])
            
            await client.write_gatt_char(WRITE_CHAR, header, response=False)
            await asyncio.sleep(0.02)
            await client.write_gatt_char(WRITE_CHAR, cmd, response=False)
            
            await asyncio.sleep(0.2)
            tx3 = bytes([0x3e, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x02, 0x3c])
            await client.write_gatt_char(WRITE_CHAR, tx3, response=False)
            
            await asyncio.sleep(0.2)
            tx4 = bytes([0x3e, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01, 0x3c])
            await client.write_gatt_char(WRITE_CHAR, tx4, response=False)
            
            await asyncio.sleep(0.2)
            print(f"Volume: {current} -> {new_vol}")
            return new_vol
    except Exception as e:
        print(f"Direct connection error: {e}")
        return None


# ==========================================
# Daemon Logic (Fast)
# ==========================================

class VolumeOSD:
    def __init__(self):
        self.root = None
        self.label = None
        self.hide_timer = None
        self.q = queue.Queue()

    def start(self):
        thread = threading.Thread(target=self._run, daemon=True)
        thread.start()

    def _run(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True) # 無邊框
        self.root.attributes("-topmost", True) # 最上層
        
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.w = int(sw * 0.10)
        self.h = int(sw * 0.10)
        w = self.w
        h = self.h
        x = int(sw - (sw * 0.10) - w) # Margin right 10%
        y = int(sh / 2 - h / 2)       # Vertically centered
        
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.configure(bg='black')
        
        self.label = tk.Label(self.root, text="", font=("Arial", int(h/2.5), "bold"), bg='black', fg='white')
        self.label.pack(expand=True)
        
        self._apply_window_styles()
        
        self.root.withdraw()
        self._check_queue()
        self.root.mainloop()
        
    def _apply_window_styles(self):
        try:
            import win32gui, win32con
            self.root.update_idletasks()
            # int(self.root.frame(), 16) is the bulletproof way to get the true top-level HWND in Tkinter
            hwnd = int(self.root.frame(), 16)
            ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            
            # WS_EX_LAYERED for alpha, WS_EX_TRANSPARENT for click-through, WS_EX_TOOLWINDOW to hide from Alt-Tab
            win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex_style | win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT | win32con.WS_EX_TOOLWINDOW)
        except Exception as e:
            print(f"OSD styling error: {e}")
        
    def _check_queue(self):
        try:
            while True:
                vol = self.q.get_nowait()
                self._update_ui(vol)
        except queue.Empty:
            pass
        self.root.after(50, self._check_queue)
        
    def show_volume(self, vol):
        self.q.put(vol)
        
    def _update_ui(self, vol):
        self.label.config(text=str(vol))
        
        # Cancel any running fade or hide timers
        if hasattr(self, 'fade_after_id') and self.fade_after_id:
            self.root.after_cancel(self.fade_after_id)
            self.fade_after_id = None
            
        if self.hide_timer:
            self.root.after_cancel(self.hide_timer)
            
        # Tkinter's deiconify often resets Windows extended styles, so we re-apply them instantly
        self.root.deiconify()
        self._apply_window_styles()
        
        # Reset opacity to 80% (0.8) and show
        self.root.attributes("-alpha", 0.8)
        
        # Start the fade out process after 1 second (1000ms)
        self.hide_timer = self.root.after(1000, self._start_fade_out, 0.8)
        
    def _start_fade_out(self, alpha=0.8):
        alpha *= 0.85 # Exponential decay (faster at first, then smooth trail)
        if alpha > 0.01:
            self.root.attributes("-alpha", alpha)
            # 8ms = ~120fps ultra smooth fade
            self.fade_after_id = self.root.after(8, self._start_fade_out, alpha)
        else:
            self.root.withdraw()
            self.fade_after_id = None


class BraviaDaemon:
    def __init__(self):
        self.client = None
        self.connected = False
        self.all_notifs = bytearray()
        self.cached_vol = None
        self.ignore_ble_until = 0
        self.ble_lock = asyncio.Lock()
        # Lock order is always conn_lock -> ble_lock. Never take conn_lock while
        # holding ble_lock, or the connect and reconnect paths will deadlock.
        self.conn_lock = asyncio.Lock()
        self.last_rx = 0.0          # Last time ANY byte arrived from the soundbar
        self.heartbeat_misses = 0
        self.connect_failures = 0
        self.stale_services = 0
        self.osd = VolumeOSD()
        self.osd.start()
        
    async def connect_loop(self):
        while True:
            try:
                if not self.connected:
                    async with self.conn_lock:
                        await self._connect_once()
            except Exception as e:
                # An escaped exception used to kill this task outright (find_device() sits
                # outside _connect_once's try), leaving a daemon that answers the socket
                # forever but never reconnects. Never let that happen again.
                print(f"[connect_loop] Unhandled error: {e!r}")
                await asyncio.sleep(5)
            await asyncio.sleep(1)

    async def _connect_once(self):
        if self.connected:
            return
        print("Scanning for soundbar...")
        device = await find_device()
        if not device:
            print("Soundbar not found, retrying in 5s...")
            await asyncio.sleep(5)
            return

        try:
            self.client = BleakClient(device.address, disconnected_callback=self.on_disconnect)
            await asyncio.wait_for(self.client.connect(), timeout=CONNECT_TIMEOUT)

            # Windows BLE bug workaround: force cache clear and service enumeration
            await asyncio.sleep(2.0)
            try:
                await self.client.clear_cache()
            except:
                pass  # Not present in every bleak version; the settle sleep is what matters
            _ = self.client.services

            if self.client.services.get_characteristic(NOTIFY_CHAR) is None:
                # Windows handed back only the standard 1800/1801 services. This happens once
                # a previous BLE session in THIS process has leaked its GATT handles: from then
                # on every connect - ours and other processes' - sees the Sony services missing,
                # and reconnecting can never fix it because the handles die with the process.
                raise StaleServicesError(
                    f"Windows returned {len(list(self.client.services))} services without the Sony profile")

            def handler(sender, data):
                # Record liveness before any filtering: even a notification we
                # intend to drop still proves the link is alive.
                self.last_rx = time.time()

                if time.time() < self.ignore_ble_until:
                    # We are in the debounce window!
                    # Actively drop incoming BLE data and flush the buffer
                    # to completely destroy any queued/stale notifications.
                    self.all_notifs.clear()
                    return

                self.all_notifs.extend(data)
                # Bound the buffer to prevent memory leak over days of uptime
                if len(self.all_notifs) > 1024:
                    self.all_notifs = self.all_notifs[-512:]

                # Real-time event-driven volume parsing!
                hex_str = self.all_notifs.hex()
                target = "0a025661120208"
                idx = hex_str.rfind(target)
                if idx != -1 and len(hex_str) >= idx + 16:
                    try:
                        vol = int(hex_str[idx + 14: idx + 16], 16)
                        self.cached_vol = vol
                    except: pass

            await self.client.start_notify(NOTIFY_CHAR, handler)
            self.last_rx = time.time()
            self.heartbeat_misses = 0

            print("Connected! Initializing volume cache...")
            # Seed the cache BEFORE announcing readiness, so no client command can
            # reach set_volume() while this method still holds conn_lock.
            await self._query_volume_from_soundbar()
            self.connected = True
            self.connect_failures = 0
            self.stale_services = 0
            print(f"Ready for instant commands! Current volume: {self.cached_vol}")

        except StaleServicesError as e:
            self.connected = False
            self.stale_services += 1
            print(f"Connection failed: {e} ({self.stale_services}/{STALE_SERVICE_MAX_TRIES})")
            await self._teardown_client()
            if self.stale_services >= STALE_SERVICE_MAX_TRIES:
                self._restart_process()
            await asyncio.sleep(min(30, 2 ** self.stale_services))

        except Exception as e:
            print(f"Connection failed: {e}")
            self.connected = False
            self.connect_failures += 1
            await self._teardown_client()
            # Back off instead of hammering the radio once a second for days on end
            await asyncio.sleep(min(30, 2 ** min(self.connect_failures, 5)))

    def _restart_process(self):
        """Respawn the daemon and exit.

        Leaked WinRT GATT handles belong to the process, so killing it is the only thing
        that releases them - reconnecting in place cannot. The stamp file rate-limits this
        across restarts, since an in-memory timestamp would reset in the fresh process.
        """
        now = time.time()
        try:
            last = os.path.getmtime(RESTART_STAMP) if os.path.exists(RESTART_STAMP) else 0
        except OSError:
            last = 0
        if now - last < RESTART_MIN_INTERVAL:
            print("[Watchdog] Restart suppressed: one already happened recently")
            return

        print("[Watchdog] Restarting process to release leaked BLE handles")
        try:
            with open(RESTART_STAMP, "w") as f:
                f.write(str(now))
            subprocess.Popen(
                [sys.executable, SCRIPT_PATH, "daemon"],
                cwd=SCRIPT_DIR,
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
        except Exception as e:
            print(f"[Watchdog] Respawn failed, staying up: {e}")
            return
        sys.stdout.flush()
        os._exit(0)  # Immediate: a clean shutdown would just hand the handles back to bleak

    async def _teardown_client(self):
        """Drop the current BLE client, tolerating a stack that is already wedged."""
        client, self.client = self.client, None
        if not client:
            return
        try:
            await asyncio.wait_for(client.stop_notify(NOTIFY_CHAR), timeout=WRITE_TIMEOUT)
        except: pass
        try:
            await asyncio.wait_for(client.disconnect(), timeout=CONNECT_TIMEOUT)
        except: pass

    async def force_reconnect(self, reason: str):
        """Tear the link down so connect_loop rebuilds it on its next pass."""
        async with self.conn_lock:
            if not self.connected and self.client is None:
                return
            print(f"[Watchdog] Forcing reconnect: {reason}")
            self.connected = False
            self.cached_vol = None
            self.ignore_ble_until = 0
            self.heartbeat_misses = 0
            await self._teardown_client()

    def on_disconnect(self, client):
        print("Disconnected! Will reconnect automatically...")
        self.connected = False
        self.client = None
        self.ignore_ble_until = 0
        self.heartbeat_misses = 0
        self.cached_vol = None

    async def poll_volume_loop(self):
        """Continuously monitor cache for changes."""
        last_notified_vol = None
        
        while True:
            if self.connected and self.cached_vol is not None:
                if self.cached_vol != last_notified_vol:
                    if last_notified_vol is not None:
                        print(f"[Poller] Background volume sync: {self.cached_vol}")
                        self.osd.show_volume(self.cached_vol)
                    last_notified_vol = self.cached_vol
            # Local memory check is instant and costs 0 network traffic
            await asyncio.sleep(0.05)

    async def _write(self, data: bytes):
        """Every BLE write goes through here so a stalled Windows BLE stack can never
        block forever while holding ble_lock (which would freeze the whole daemon)."""
        client = self.client
        if client is None:
            raise RuntimeError("no BLE client")
        await asyncio.wait_for(client.write_gatt_char(WRITE_CHAR, data, response=False), timeout=WRITE_TIMEOUT)

    async def heartbeat_loop(self):
        """Detect a soundbar that stopped answering while Windows still reports 'connected'.

        Only probes when the link has gone quiet, to keep BLE traffic minimal."""
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)

            if not self.connected or self.client is None:
                continue
            # Cheap check first: catches a drop the disconnect callback never reported
            if not self.client.is_connected:
                await self.force_reconnect("BLE stack reports the link is down")
                continue
            # Recent traffic (or a volume transaction still settling) already proves liveness
            if time.time() - self.last_rx < HEARTBEAT_INTERVAL or time.time() < self.ignore_ble_until:
                self.heartbeat_misses = 0
                continue

            before = self.last_rx
            try:
                async with self.ble_lock:
                    await self._write(MINI_VOL_QUERY)
                    for _ in range(int(HEARTBEAT_REPLY_TIMEOUT / 0.1)):
                        await asyncio.sleep(0.1)
                        if self.last_rx > before:
                            break
                    # Close the transaction even on silence, so a merely slow soundbar
                    # is not left with a half-open state machine.
                    await self._write(TX_CLOSE)
            except Exception as e:
                await self.force_reconnect(f"heartbeat write failed: {e}")
                continue

            if self.last_rx > before:
                self.heartbeat_misses = 0
            else:
                self.heartbeat_misses += 1
                print(f"[Heartbeat] No reply ({self.heartbeat_misses}/{HEARTBEAT_MAX_MISSES})")
                if self.heartbeat_misses >= HEARTBEAT_MAX_MISSES:
                    await self.force_reconnect("soundbar stopped answering heartbeats")

    async def _query_volume_from_soundbar(self):
        """Fetch volume once and properly close the transaction to free the soundbar state machine.

        Write errors propagate: a query that cannot even be sent means the link is dead,
        and the caller must not treat the connection as usable."""
        async with self.ble_lock:
            await self._write(MINI_VOL_QUERY)
            # Wait for the soundbar to reply and the real-time handler to update cached_vol
            await asyncio.sleep(0.5)
            # TX4: Close/End the query transaction!
            await self._write(TX_CLOSE)

    async def get_volume(self):
        if not self.connected: return None
        # The real-time handler guarantees cached_vol is always up-to-date
        if self.cached_vol is None:
            # Connected but never got a reading (e.g. the soundbar was silent at connect
            # time). Re-query instead of failing every command from here on.
            try:
                await self._query_volume_from_soundbar()
            except Exception as e:
                asyncio.create_task(self.force_reconnect(f"volume re-query failed: {e}"))
                return None
        return self.cached_vol

    async def set_volume(self, vol: int):
        if not self.connected or vol < MIN_VOL or vol > MAX_VOL: return False
        failure = None
        async with self.ble_lock:
            try:
                # Protect the entire duration of the BLE transaction
                self.ignore_ble_until = time.time() + 9.0
                
                header = bytes([0x3e, 0x07, 0x00, 0x00, 0x00, 0x00, 0x10, 0x00, 0x00, 0x02,
                                0x02, 0x0a, 0x0a, 0x0a, 0x02, 0x56, 0x61, 0x10, 0x01, 0x22])
                vv = vol & 0xFF
                cc = (vv & 0xFE) + 0x30
                cmd = bytes([0x02, 0x08, vv, cc, 0x3c])
                # TX1 & TX2
                await self._write(header)
                await asyncio.sleep(0.02)
                await self._write(cmd)

                # TX3: iOS Echo ACK
                await asyncio.sleep(0.2)
                await self._write(TX_ACK)

                # TX4: Close Connection Gracefully
                await asyncio.sleep(0.2)
                await self._write(TX_CLOSE)

                # After the transaction fully completes, give the Soundbar 6.0s to clear its queue
                self.ignore_ble_until = time.time() + 6.0
                return True
            except Exception as e:
                print(f"Write error: {e}")
                self.ignore_ble_until = 0
                failure = e
                return False
            finally:
                if failure is not None:
                    # The link is unusable (timed out, or died mid-transaction). Rebuild it
                    # now rather than letting every future command fail the same way.
                    # Scheduled instead of awaited so we release ble_lock first.
                    asyncio.create_task(self.force_reconnect(f"volume write failed: {failure}"))

    async def handle_client(self, reader, writer):
        try:
            data = await asyncio.wait_for(reader.read(100), timeout=2.0)
            message = data.decode().strip()
            parts = message.split()
            if not parts: return

            try:
                response = await asyncio.wait_for(self._dispatch(parts), timeout=COMMAND_TIMEOUT)
            except asyncio.TimeoutError:
                # The BLE transaction is stuck; answer the client and rebuild the link.
                response = "ERROR: Timed out, reconnecting"
                asyncio.create_task(self.force_reconnect("command timed out"))

            writer.write(response.encode())
            await writer.drain()
        except Exception as e:
            print(f"Client handler error: {e}")
        finally:
            writer.close()

    async def _dispatch(self, parts) -> str:
        cmd = parts[0].lower()
        response = "ERROR: Unknown command"

        if not self.connected:
            response = "ERROR: Soundbar not connected to daemon"
        elif cmd == "status":
            vol = await self.get_volume()
            response = f"OK {vol}" if vol is not None else "ERROR: Read failed"
        elif cmd == "set" and len(parts) > 1:
            vol = int(parts[1])
            self.cached_vol = vol
            success = await self.set_volume(vol)
            response = f"OK {vol}" if success else "ERROR: Set failed"
        elif cmd in ("up", "down"):
            step = int(parts[1]) if len(parts) > 1 else 1
            if self.cached_vol is None:
                self.cached_vol = await self.get_volume()

            if self.cached_vol is not None:
                new_vol = min(MAX_VOL, self.cached_vol + step) if cmd == "up" else max(MIN_VOL, self.cached_vol - step)
                if new_vol == self.cached_vol:
                    response = f"OK {self.cached_vol}"
                else:
                    self.cached_vol = new_vol
                    success = await self.set_volume(new_vol)
                    response = f"OK {new_vol}" if success else "ERROR: Set failed"
            else:
                response = "ERROR: Read failed (cache empty)"

        return response

    async def _supervise(self, coro_factory, name):
        """Restart a background loop if it ever raises.

        An unhandled exception in a bare create_task() task is swallowed until garbage
        collection, so a dead watchdog looks exactly like a healthy one from outside."""
        while True:
            try:
                await coro_factory()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[{name}] Crashed, restarting in 5s: {e!r}")
                await asyncio.sleep(5)

    async def run(self):
        asyncio.create_task(self._supervise(self.connect_loop, "connect_loop"))
        asyncio.create_task(self._supervise(self.poll_volume_loop, "poll_volume_loop"))
        asyncio.create_task(self._supervise(self.heartbeat_loop, "heartbeat_loop"))
        server = None
        for attempt in range(10):
            try:
                server = await asyncio.start_server(self.handle_client, '127.0.0.1', DAEMON_PORT)
                break
            except OSError as e:
                # A self-restart briefly overlaps with the process it replaces
                print(f"Port {DAEMON_PORT} busy ({e}), retrying...")
                await asyncio.sleep(1)
        if server is None:
            print("Could not bind the daemon port; another instance owns it. Exiting.")
            return
        print(f"Daemon listening on TCP port {DAEMON_PORT}")
        try:
            async with server:
                await server.serve_forever()
        except asyncio.CancelledError:
            pass
        finally:
            if self.client:
                print("Cleaning up BLE connection...")
                await self._teardown_client()


# ==========================================
# CLI Frontend
# ==========================================

def send_daemon_command(cmd_str: str) -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(('127.0.0.1', DAEMON_PORT))
            s.sendall(cmd_str.encode())
            s.settimeout(10.0)
            data = s.recv(1024)
            return data.decode()
    except Exception:
        # Any socket error (ConnectionRefused, Timeout, etc.) means 
        # the daemon is not running or unavailable. Trigger fallback.
        return None


def print_vol_bar(vol: int):
    width = 50  # Fixed width so a 0-100 range still fits one terminal line
    filled = round(vol * width / MAX_VOL)
    bar = "#" * filled + "-" * (width - filled)
    print(f"Volume: {vol}/{MAX_VOL}")
    print(f"  [{bar}]")


async def main():
    if len(sys.argv) < 2:
        print("Sony HT-B600 BLE Volume Controller")
        print("=" * 40)
        print("Commands:")
        print("  daemon         Start the background service (run this once)")
        print("  set <0-100>    Set volume")
        print("  up [step]      Volume up (default: 1)")
        print("  down [step]    Volume down (default: 1)")
        print("  status         Show current volume")
        return

    action = sys.argv[1].lower()

    if action == "daemon":
        # pythonw.exe gives the daemon no stdout, so every print() would vanish
        # Keep printing to the console too when there is one, so a foreground run still
        # shows its diagnostics live instead of only landing in the log file.
        sys.stdout = sys.stderr = DaemonLog(LOG_PATH, mirror=sys.stdout)
        print("=" * 50)
        print(f"Daemon starting (pid {os.getpid()})")
        daemon = BraviaDaemon()
        await daemon.run()
        return

    # Client commands
    cmd_str = " ".join(sys.argv[1:])
    
    # Try daemon first (instant)
    response = send_daemon_command(cmd_str)
    
    if response and response.startswith("OK"):
        vol = int(response.split()[1])
        print("[Daemon] Instant response:")
        print_vol_bar(vol)
        return

    # Fallback to direct connection (slow). Also covers a daemon that is listening but has
    # no BLE link - otherwise its error would leave the user with no way to change volume.
    if response:
        print(f"[Daemon] {response}")
        print("Falling back to direct connection (slow)...")
    else:
        print("Daemon not running. Falling back to direct connection (slow)...")
    print("Tip: Run 'python bravia_control.py daemon' in the background for zero latency.")
    
    if action == "status":
        vol = await direct_get_volume()
        if vol is not None:
            print_vol_bar(vol)
        else:
            print("Failed to read volume.")

    elif action == "set":
        vol = int(sys.argv[2])
        if await direct_set_volume(vol):
            print(f"Volume set to: {vol}")

    elif action in ("up", "down"):
        step = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        new_vol = await direct_change_volume(action, step)
        if new_vol is None:
            print("Failed to change volume.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nDaemon gracefully shut down.")
