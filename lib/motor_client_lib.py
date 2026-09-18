# Acquisition Manager - spectral acquisition suite for XIMEA cameras
# Copyright (C) 2026  Alessio Turchi
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for details.
#
# You should have received a copy of the GNU General Public License along with
# this program.  If not, see <https://www.gnu.org/licenses/>.
"""
motor_client_lib.py
-------------------
Client library to communicate with a PI C-863 Mercury Controller via TCP.
Implements a GCS-command-based actuator movement sequence.

Movement sequence:
  1. VER?    - verify controller is alive (firmware version)
  2. GOH     - go to home position
  3. POS?    - verify position is at home (0.0)
  4. MVR     - relative move by specified distance
  5. ONT?    - wait for on-target confirmation
  6. POS?    - verify final position matches expected

Reference: C-863 Mercury Controller, GCS Command Set (MS205Equ, ver. 2.0.0)

Author: Alessio Turchi (2026)
"""

import logging
import socket
import time

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults (can be overridden via config dict passed by caller)
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "host":          "193.206.154.132",
    "port":          2002,
    "axis":          "1",           # GCS axis identifier
    "timeout":       10.0,          # socket timeout in seconds
    "home_tol":      0.1,         # tolerance to accept as "at home" (units)
    "move_tol":      0.1,          # tolerance for final position check (units)
    "ont_retries":   100,            # max polls for ONT? (on-target)
    "ont_delay":     0.5,           # seconds between ONT? polls
    "move_delay":    2.5,           # seconds to wait after GOH before polling
}

MSG_TERMINATOR = b"\n"
BUFFER_SIZE    = 4096

# Telnet IAC byte (0xFF) - sent by some PI controllers during TCP negotiation
_TELNET_IAC = 0xFF


# ---------------------------------------------------------------------------
# Low-level TCP helpers
# ---------------------------------------------------------------------------

def _strip_telnet_iac(data: bytes) -> bytes:
    """
    Remove Telnet IAC negotiation sequences from raw bytes.
    Sequences have the form: IAC (0xFF) + CMD (1 byte) + OPT (1 byte).
    Escaped literal 0xFF is encoded as IAC IAC (0xFF 0xFF) -> single 0xFF,
    but since GCS responses are ASCII we discard those too.
    Bytes > 0x7F that are not part of IAC sequences are also dropped.
    """
    out = bytearray()
    i = 0
    while i < len(data):
        b = data[i]
        if b == _TELNET_IAC:
            # Skip IAC + next 2 bytes (CMD + OPT), or just IAC if at end
            i += 3
        elif b > 0x7F:
            # Non-ASCII, non-IAC byte - skip
            i += 1
        else:
            out.append(b)
            i += 1
    return bytes(out)


def _send_command(sock: socket.socket, cmd: str) -> str:
    """
    Send a GCS command string and return the stripped response line.
    Telnet IAC negotiation bytes are filtered before decoding.
    Commands are terminated with \\n; responses end with \\n.
    """
    raw_cmd = (cmd.strip() + "\n").encode("ascii")
    log.debug(f">> {cmd.strip()}")
    sock.sendall(raw_cmd)

    buf = b""
    while MSG_TERMINATOR not in _strip_telnet_iac(buf):
        chunk = sock.recv(BUFFER_SIZE)
        if not chunk:
            raise ConnectionError("Connection closed by controller before response received.")
        buf += chunk
        log.debug(f"raw bytes: {buf!r}")

    clean = _strip_telnet_iac(buf)
    resp  = clean.split(MSG_TERMINATOR, 1)[0].decode("ascii").strip()
    log.debug(f"<< {resp!r}")
    return resp


def _open_socket(host: str, port: int, timeout: float) -> socket.socket:
    """Create and connect a TCP socket to the Mercury controller."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect((host, port))
    return sock


# ---------------------------------------------------------------------------
# GCS command wrappers
# ---------------------------------------------------------------------------

def _cmd_svo(sock: socket.socket, axis: str, enable: bool) -> None:
    """SVO - Enable (1) or disable (0) servo mode."""
    _send_command_no_reply(sock, f"SVO {axis} {1 if enable else 0}")

def _cmd_ron(sock:  socket.socket, axis: str, enable: bool) -> None:
    """RON  - Enable (1) or disable the referencing requirement."""
    _send_command_no_reply(sock, f"RON {axis} {1 if enable else 0}")

def _cmd_pos_home(sock: socket.socket, axis: str) -> None:
    """POS? - Set home position after disabling reference."""
    _send_command_no_reply(sock, f"POS {axis} 0.000000")

def _cmd_fnl(sock: socket.socket, axis: str) -> None:
    """FNL - Send axis to negative limit switch (homing)."""
    _send_command_no_reply(sock, f"FNL {axis}")

def _cmd_mov(sock: socket.socket, axis: str, position: float) -> None:
    """MOV - Absolute move to target position."""
    _send_command_no_reply(sock, f"MOV {axis} {position:.6f}")

def _cmd_tmn(sock: socket.socket, axis: str) -> float:
    """TMN? - Get minimum travel range limit. Returns float."""
    resp = _send_command(sock, f"TMN? {axis}")
    try:
        return float(resp.split("=")[1])
    except (IndexError, ValueError) as e:
        raise ValueError(f"Cannot parse TMN? response: {resp!r}") from e

def _cmd_tmx(sock: socket.socket, axis: str) -> float:
    """TMX? - Get maximum travel range limit. Returns float."""
    resp = _send_command(sock, f"TMX? {axis}")
    try:
        return float(resp.split("=")[1])
    except (IndexError, ValueError) as e:
        raise ValueError(f"Cannot parse TMX? response: {resp!r}") from e

def _cmd_ver(sock: socket.socket) -> str:
    """VER? - Get firmware version. Returns version string."""
    return _send_command(sock, "VER?")

def _cmd_goh(sock: socket.socket, axis: str) -> None:
    """GOH - Go to home position. No response expected from controller."""
    _send_command_no_reply(sock, f"GOH {axis}")

def _cmd_pos(sock: socket.socket, axis: str) -> float:
    """POS? - Get real position. Returns float value."""
    resp = _send_command(sock, f"POS? {axis}")
    # Expected format: "1=0.00000\n" (axis=value)
    try:
        return float(resp.split("=")[1])
    except (IndexError, ValueError) as e:
        raise ValueError(f"Cannot parse POS? response: {resp!r}") from e

def _cmd_mvr(sock: socket.socket, axis: str, distance: float) -> None:
    """MVR - Relative move. No response expected from controller."""
    _send_command_no_reply(sock, f"MVR {axis} {distance:.6f}")

def _cmd_ont(sock: socket.socket, axis: str) -> bool:
    """ONT? - Get on-target state. Returns True if on target."""
    resp = _send_command(sock, f"ONT? {axis}")
    # Expected format: "1=1" (on target) or "1=0" (moving)
    try:
        return int(resp.split("=")[1]) == 1
    except (IndexError, ValueError):
        return False


def _send_command_no_reply(sock: socket.socket, cmd: str) -> None:
    """
    Send a GCS command that produces no response (GOH, MVR, etc.).
    The Mercury controller does NOT echo these commands.
    """
    raw_cmd = (cmd.strip() + "\n").encode("utf-8")
    log.debug(f">> {cmd.strip()} [no reply expected]")
    sock.sendall(raw_cmd)


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------

def execute_move(position: float, config: dict = None) -> tuple:
    """
    Execute a full actuator movement sequence on the C-863 Mercury Controller.

    Sequence:
      1. VER?      - check controller alive
      2. SVO 1 1   - enable servo
      3. RON 0     - disable reference
      4. POS?      - read home position
      5. MVR       - relative move to `position`
      6. ONT?      - wait until on-target
      7. POS?      - verify final position
      8. SVO 1 0   - disable servo

    Args:
        position: target position in degrees.
        config:   Optional dict to override DEFAULT_CONFIG fields.

    Returns:
        (success: bool, info: dict)
        info keys: "firmware", "pos_home", "pos_final", "tmn", "tmx", "reason"
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    host    = cfg["host"]
    port    = cfg["port"]
    axis    = cfg["axis"]
    timeout = cfg["timeout"]

    info = {
        "firmware": None, "pos_home": None, "pos_final": None, "reason": ""
    }

    try:
        sock = _open_socket(host, port, timeout)
    except Exception as e:
        reason = f"Cannot connect to {host}:{port} - {e}"
        log.error(reason)
        info["reason"] = reason
        return False, info

    try:
        firmware = _cmd_ver(sock)
        info["firmware"] = firmware
        log.info(f"Controller alive. Firmware: {firmware}")

#        log.info("Enabling servo (SVO 1 1)...")
#        _cmd_svo(sock, axis, enable=True)
#        time.sleep(0.2)

        log.info("Disabling referencing requirement")
        _cmd_ron(sock, axis, enable=False)
        time.sleep(0.2)

        log.info("Set home position")        
        _cmd_pos_home(sock, axis)
        time.sleep(0.2)

        pos_home = _cmd_pos(sock, axis)
        info["pos_home"] = pos_home
        log.info(f"Home position: {pos_home}")

        log.info(f"Moving to position {position:.6f} (MOV)...")
        _cmd_mvr(sock, axis, position)
        time.sleep(cfg["move_delay"])

        if not _wait_on_target(sock, axis, cfg):
            reason = "Timeout waiting for ONT? after moving"
            log.error(reason)
            info["reason"] = reason
            return False, info

        # Step 9 - Verify final position
        pos_final = _cmd_pos(sock, axis)
        info["pos_final"] = pos_final
        pos_rel=pos_final-pos_home
        error = abs(pos_rel - position)
        log.info(f"Final movement: {pos_rel} (expected {pos_rel:.6f}, error {error:.6f})")
        log.info(f"Final position: {pos_final}")

        if error > cfg["move_tol"]:
            reason = (f"Position mismatch: expected {position:.6f}, "
                      f"got {pos_final:.6f}, error {error:.6f} > tol {cfg['move_tol']}")
            log.error(reason)
            info["reason"] = reason
            return False, info

        log.info("Movement sequence completed successfully.")
        return True, info

    except Exception as e:
        reason = f"Unexpected error during movement sequence: {e}"
        log.exception(reason)
        info["reason"] = reason
        return False, info

    finally:
        # Step 10 - Always disable servo on exit
#        try:
#            _cmd_svo(sock, axis, enable=False)
#            log.info("Servo disabled (SVO 1 0).")
#        except Exception:
#            pass
        sock.close()


def _wait_on_target(sock: socket.socket, axis: str, cfg: dict) -> bool:
    """Poll ONT? until on-target or max retries exceeded."""
    for i in range(cfg["ont_retries"]):
        if _cmd_ont(sock, axis):
            log.debug(f"ONT? confirmed on poll {i+1}/{cfg['ont_retries']}")
            return True
        time.sleep(cfg["ont_delay"])
    return False


def motor_servo_enable(config: dict = None) -> None:
    """Enable servo mode (SVO 1 1). Call once before the measurement loop."""
    cfg  = {**DEFAULT_CONFIG, **(config or {})}
    sock = _open_socket(cfg["host"], cfg["port"], cfg["timeout"])
    try:
        _cmd_svo(sock, cfg["axis"], enable=True)
        log.info("Servo enabled (SVO 1 1).")
    finally:
        sock.close()


def motor_servo_disable(config: dict = None) -> None:
    """Disable servo mode (SVO 1 0). Call once after the measurement loop."""
    cfg  = {**DEFAULT_CONFIG, **(config or {})}
    sock = _open_socket(cfg["host"], cfg["port"], cfg["timeout"])
    try:
        _cmd_svo(sock, cfg["axis"], enable=False)
        log.info("Servo disabled (SVO 1 0).")
    finally:
        sock.close()

# ---------------------------------------------------------------------------
# Debug / diagnostic functions (public)
# ---------------------------------------------------------------------------

def debug_raw_connection(host: str, port: int, timeout: float = 5.0, read_bytes: int = 256) -> None:
    """
    Open a raw TCP connection and print the bytes received at connect time
    (typically Telnet IAC negotiation). Does NOT send any command.
    Useful to inspect what the controller sends before any interaction.
    """
    print(f"[debug_raw_connection] Connecting to {host}:{port} ...")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((host, port))
        print(f"[debug_raw_connection] Connected. Waiting for initial data ({read_bytes} bytes max)...")
        try:
            data = sock.recv(read_bytes)
            print(f"[debug_raw_connection] Raw bytes received : {data!r}")
            print(f"[debug_raw_connection] Hex               : {data.hex(' ')}")
            cleaned = _strip_telnet_iac(data)
            print(f"[debug_raw_connection] After IAC strip   : {cleaned!r}")
        except socket.timeout:
            print("[debug_raw_connection] No data received within timeout (controller may not send a banner).")
        finally:
            sock.close()
    except Exception as e:
        print(f"[debug_raw_connection] Connection failed: {e}")


def debug_send_command(cmd: str, host: str, port: int,
                       timeout: float = 5.0, read_bytes: int = 256) -> None:
    """
    Send a single raw GCS command and print the raw and cleaned response.
    Useful to test individual commands and inspect IAC filtering.

    Args:
        cmd:       GCS command string (e.g. "VER?", "POS? 1", "ERR?").
        host/port: Controller address.
        timeout:   Socket timeout in seconds.
        read_bytes: Max bytes to read per recv call.
    """
    print(f"[debug_send_command] Connecting to {host}:{port} ...")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((host, port))
        print(f"[debug_send_command] Connected.")

        # Drain any initial banner (Telnet negotiation)
        try:
            banner = sock.recv(read_bytes)
            print(f"[debug_send_command] Banner raw  : {banner!r}")
            print(f"[debug_send_command] Banner hex  : {banner.hex(' ')}")
        except socket.timeout:
            print("[debug_send_command] No banner received.")

        # Send command
        raw_cmd = (cmd.strip() + "\n").encode("ascii")
        print(f"[debug_send_command] Sending     : {raw_cmd!r}")
        sock.sendall(raw_cmd)

        # Receive response with timeout
        buf = b""
        try:
            while True:
                chunk = sock.recv(read_bytes)
                if not chunk:
                    break
                buf += chunk
                if MSG_TERMINATOR in _strip_telnet_iac(buf):
                    break
        except socket.timeout:
            pass  # Partial response is still printed

        print(f"[debug_send_command] Raw response : {buf!r}")
        print(f"[debug_send_command] Hex          : {buf.hex(' ')}")
        cleaned = _strip_telnet_iac(buf)
        decoded = cleaned.split(MSG_TERMINATOR, 1)[0].decode("ascii", errors="replace").strip()
        print(f"[debug_send_command] Cleaned      : {cleaned!r}")
        print(f"[debug_send_command] Decoded      : {decoded!r}")

        sock.close()
    except Exception as e:
        print(f"[debug_send_command] Error: {e}")


def debug_full_handshake(config: dict = None) -> None:
    """
    Run VER?, ERR?, SVO?, POS?, ONT?, TMN?, TMX? in sequence and print all
    raw/cleaned responses. Useful to verify IAC stripping and response parsing
    end-to-end before attempting a real movement.
    """
    cfg  = {**DEFAULT_CONFIG, **(config or {})}
    host = cfg["host"]
    port = cfg["port"]
    axis = cfg["axis"]

    print(f"[debug_full_handshake] Target: {host}:{port}  axis={axis}")
    cmds = [
        "VER?",
        "ERR?",
        f"SVO? {axis}",
        f"POS? {axis}",
        f"ONT? {axis}",
        f"TMN? {axis}",
        f"TMX? {axis}",
    ]

    try:
        sock = _open_socket(host, port, cfg["timeout"])

        # Drain banner
        try:
            banner = sock.recv(256)
            print(f"[debug_full_handshake] Banner: {banner!r}  -> cleaned: {_strip_telnet_iac(banner)!r}")
        except socket.timeout:
            print("[debug_full_handshake] No banner.")

        for cmd in cmds:
            try:
                resp = _send_command(sock, cmd)
                print(f"[debug_full_handshake]   {cmd:<12} -> {resp!r}")
            except Exception as e:
                print(f"[debug_full_handshake]   {cmd:<12} -> ERROR: {e}")

        sock.close()
    except Exception as e:
        print(f"[debug_full_handshake] Connection failed: {e}")

# ---------------------------------------------------------------------------
# Example / self-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )

    custom_config = {
        "host": "193.206.154.132",
        "port": 2001,
        "axis": "1",
        "move_tol": 0.05,
    }

    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "move"

    if mode == "raw":
        # Test 1: inspect initial bytes sent by controller at connect time
        debug_raw_connection(custom_config["host"], custom_config["port"])

    elif mode == "cmd":
        # Test 2: send a single command and inspect raw/cleaned response
        cmd = sys.argv[2] if len(sys.argv) > 2 else "VER?"
        debug_send_command(cmd, custom_config["host"], custom_config["port"])

    elif mode == "handshake":
        # Test 3: run VER?, ERR?, POS?, ONT? in sequence
        debug_full_handshake(custom_config)

    else:
        # Default: run full movement sequence
        custom_config = {
            "host":          "193.206.154.132",
            "port":          2002,
            "axis":          "1",           # GCS axis identifier
            "timeout":       10.0,          # socket timeout in seconds
            "home_tol":      0.1,           # tolerance to accept as "at home" (units)
            "move_tol":      0.1,           # tolerance for final position check (units)
            "ont_retries":   100,            # max polls for ONT? (on-target)
            "ont_delay":     0.5,           # seconds between ONT? polls
            "move_delay":    2.5,           # seconds to wait after GOH before polling
}
        motor_servo_enable(custom_config)
        ok, info = execute_move(position=5.0, config=custom_config)
        motor_servo_disable(custom_config)

        print(f"Move {'OK' if ok else 'FAILED'}")
        print(f"  Firmware : {info['firmware']}")
        print(f"  Home pos : {info['pos_home']}")
        print(f"  Final pos: {info['pos_final']}")
        if not ok:
            print(f"  Reason   : {info['reason']}")
