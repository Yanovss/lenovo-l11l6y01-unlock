#!/usr/bin/env python3
"""
r2j240.py - SMBus tool for LGC smart batteries with Renesas R2J240 / 51F51
controller, running on a Raspberry Pi with *software* (bit-banged) I2C.

Why bit-bang: the hardware I2C of BCM2835/2836/2837 (Pi 1/2/3) does not handle
SMBus clock stretching correctly, and these battery controllers stretch SCL a lot.

Wiring (Raspberry Pi 40-pin header):
    battery SMBDAT -> Pin 3 (GPIO2)
    battery SMBCLK -> Pin 5 (GPIO3)
    battery B-     -> Pin 6 (GND)

Commands:
    sbs                     read standard SBS registers (read-only)
    mode                    detect firmware mode vs Boot ROM (read-only)
    dump  df1|df2|df3|all   dump flash areas, Boot ROM required (read-only)
    write df1 FILE --expect ORIG --confirm
                            erase + write + verify DF1 (Boot ROM required)
    execute                 leave Boot ROM and start the firmware

Requires root (uses /dev/mem). No extra Python packages needed.
Protocol of the Boot ROM is taken from karosium/smbusb (smbusb_r2j240flasher.c).

USE AT YOUR OWN RISK. Always keep backups of every dump.
"""
import argparse
import gc
import hashlib
import mmap
import os
import struct
import sys
import time

ADDR = 0x0B
SDA, SCL = 2, 3
PRESETS = {"df1": (0x3000, 0x400), "df2": (0x3400, 0x400), "df3": (0xC000, 0x2000)}
I2C_DRV = "/sys/bus/platform/drivers/i2c-bcm2835"


# --------------------------------------------------------------------------- #
#  Bit-banged open-drain I2C on GPIO2/GPIO3 via /dev/mem
# --------------------------------------------------------------------------- #
class BitBangI2C:
    def __init__(self, peri_base):
        self._fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
        self._m = mmap.mmap(self._fd, 4096, mmap.MAP_SHARED,
                            mmap.PROT_READ | mmap.PROT_WRITE, offset=peri_base + 0x200000)
        self._orig_fsel0 = self._rd(0x00)
        self._wr(0x28, (1 << SDA) | (1 << SCL))  # output latch = 0 (we only ever drive LOW)
        self.release(SDA)
        self.release(SCL)

    # registers
    def _rd(self, off):
        return struct.unpack_from("<I", self._m, off)[0]

    def _wr(self, off, val):
        struct.pack_into("<I", self._m, off, val)

    def _fsel(self, pin, mode):
        v = self._rd(0x00)
        sh = pin * 3
        self._wr(0x00, (v & ~(7 << sh)) | (mode << sh))

    def level(self, pin):
        return (self._rd(0x34) >> pin) & 1

    def low(self, pin):
        self._fsel(pin, 1)        # output, latch is 0 -> line pulled low

    def release(self, pin):
        self._fsel(pin, 0)        # input -> line pulled high by pull-up resistors

    # bus primitives
    def scl_high(self):
        self.release(SCL)
        t0 = time.monotonic()
        while not self.level(SCL):                 # clock stretching
            if time.monotonic() - t0 > 0.05:
                raise OSError("SCL held low > 50 ms")

    def start(self):
        self.release(SDA)
        self.scl_high()
        self.low(SDA)
        self.low(SCL)

    def stop(self):
        self.low(SDA)
        self.scl_high()
        self.release(SDA)

    def write_byte(self, b):
        for i in range(8):
            (self.release if b & (0x80 >> i) else self.low)(SDA)
            self.scl_high()
            self.low(SCL)
        self.release(SDA)
        self.scl_high()
        ack = not self.level(SDA)
        self.low(SCL)
        return ack

    def read_byte(self, ack):
        self.release(SDA)
        v = 0
        for _ in range(8):
            self.scl_high()
            v = (v << 1) | self.level(SDA)
            self.low(SCL)
        (self.low if ack else self.release)(SDA)
        self.scl_high()
        self.low(SCL)
        self.release(SDA)
        return v

    def recover(self):
        self.release(SDA)
        for _ in range(9):
            if self.level(SDA):
                break
            self.low(SCL)
            self.scl_high()
        self.stop()

    def close(self):
        try:
            self.recover()
        finally:
            self._wr(0x00, self._orig_fsel0)
            self._m.close()
            os.close(self._fd)

    # transactions
    def write(self, addr, data):
        try:
            self.start()
            if not self.write_byte(addr << 1):
                raise OSError("NACK on address (write)")
            for i, b in enumerate(data):
                if not self.write_byte(b):
                    raise OSError(f"NACK on byte {i}")
        finally:
            self.stop()

    def write_read(self, addr, wdata, rlen, pause=0.0, block=False):
        """START addr+W wdata [pause] RESTART addr+R read rlen [STOP].
        If block=True the first byte read is a length byte (SMBus block read)."""
        try:
            self.start()
            if not self.write_byte(addr << 1):
                raise OSError("NACK on address (write)")
            for b in wdata:
                if not self.write_byte(b):
                    raise OSError("NACK on command/data")
            if pause:
                time.sleep(pause)
            self.start()
            if not self.write_byte((addr << 1) | 1):
                raise OSError("NACK on address (read)")
            out = []
            n = rlen
            i = 0
            while i < n:
                out.append(self.read_byte(i < n - 1))
                if block and i == 0:
                    n = min(out[0], 32) + 1
                i += 1
            return out
        finally:
            self.stop()


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
def realtime():
    gc.disable()
    try:
        os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(50))
    except (AttributeError, PermissionError, OSError):
        pass


def hw_i2c_devices():
    if not os.path.isdir(I2C_DRV):
        return []
    return [d for d in os.listdir(I2C_DRV) if d.endswith("804000.i2c")]


def hw_i2c_unbind():
    """Detach the kernel I2C1 driver so it does not fight over GPIO2/3."""
    devs = hw_i2c_devices()
    for d in devs:
        with open(os.path.join(I2C_DRV, "unbind"), "w") as f:
            f.write(d)
    return devs


def hw_i2c_rebind(devs):
    for d in devs:
        try:
            with open(os.path.join(I2C_DRV, "bind"), "w") as f:
                f.write(d)
        except OSError:
            pass


def majority(fn, tries=10, need=3):
    """Call fn() until the same result is seen `need` times."""
    seen = {}
    for _ in range(tries):
        try:
            r = fn()
        except OSError:
            BUS.recover()
            time.sleep(0.01)
            continue
        r = bytes(r)
        seen[r] = seen.get(r, 0) + 1
        if seen[r] >= need:
            return r
    return None


# --------------------------------------------------------------------------- #
#  SBS (firmware mode)
# --------------------------------------------------------------------------- #
def sbs_word(reg):
    r = majority(lambda: BUS.write_read(ADDR, [reg], 2), tries=12, need=2)
    return None if r is None else r[0] | (r[1] << 8)


def sbs_block(reg):
    r = majority(lambda: BUS.write_read(ADDR, [reg], 33, block=True), tries=12, need=2)
    if r is None:
        return None
    return r[1:1 + r[0]].decode("ascii", "replace")


STATUS_BITS = [(0x8000, "OCA over-charged alarm"), (0x4000, "TCA terminate-charge alarm"),
               (0x1000, "OTA over-temperature alarm"), (0x0800, "TDA terminate-discharge alarm"),
               (0x0200, "RCA remaining-capacity alarm"), (0x0100, "RTA remaining-time alarm"),
               (0x0080, "INIT initialized"), (0x0040, "DSG discharging"),
               (0x0020, "FC fully charged"), (0x0010, "FD fully discharged")]


def cmd_sbs(_args):
    print("=== Strings ===")
    for reg, name in [(0x20, "ManufacturerName"), (0x21, "DeviceName"), (0x22, "DeviceChemistry")]:
        print(f"[0x{reg:02X}] {name:20s}: {sbs_block(reg)}")
    print("\n=== Registers ===")
    regs = [(0x03, "BatteryMode", "hex"), (0x08, "Temperature", "temp"), (0x09, "Voltage", "mV"),
            (0x0A, "Current", "mAs"), (0x0D, "RelativeSOC", "%"), (0x0F, "RemainingCapacity", "10mWh"),
            (0x10, "FullChargeCapacity", "10mWh"), (0x14, "ChargingCurrent", "mA"),
            (0x15, "ChargingVoltage", "mV"), (0x16, "BatteryStatus", "hex"), (0x17, "CycleCount", ""),
            (0x18, "DesignCapacity", "10mWh"), (0x19, "DesignVoltage", "mV"),
            (0x1B, "ManufactureDate", "date"), (0x1C, "SerialNumber", "")]
    vals = {}
    for reg, name, kind in regs:
        v = sbs_word(reg)
        vals[reg] = v
        if v is None:
            s = "N/A"
        elif kind == "mV":
            s = f"{v} mV ({v / 1000:.3f} V)"
        elif kind == "mAs":
            s = f"{v - 0x10000 if v & 0x8000 else v} mA"
        elif kind == "temp":
            s = f"{v / 10 - 273.15:.1f} C"
        elif kind == "date":
            s = f"{1980 + (v >> 9)}-{(v >> 5) & 15:02d}-{v & 31:02d}"
        elif kind == "hex":
            s = f"0x{v:04X}"
        else:
            s = f"{v} {kind}".strip()
        print(f"[0x{reg:02X}] {name:20s}: {s}")
    st = vals.get(0x16)
    if st is not None:
        print("\n=== BatteryStatus flags ===")
        for m, d in STATUS_BITS:
            if st & m:
                print("  [SET]", d)
    cc, cv = vals.get(0x14), vals.get(0x15)
    if cc is not None and cv is not None:
        print("\n=== Verdict ===")
        if cc == 0 or cv == 0:
            print(f"  ChargingCurrent={cc} mA, ChargingVoltage={cv} mV -> PACK BLOCKS CHARGING (locked)")
        else:
            print(f"  ChargingCurrent={cc} mA, ChargingVoltage={cv} mV -> pack requests charge (OK)")


# --------------------------------------------------------------------------- #
#  Boot ROM
# --------------------------------------------------------------------------- #
def rom_read(addr, n):
    return BUS.write_read(ADDR, [0xFC, addr & 0xFF, (addr >> 8) & 0xFF, (addr >> 16) & 0xFF,
                                 n & 0xFF, (n >> 8) & 0xFF], n, pause=0.005)


def rom_dump(addr, size, chunk=32):
    out = bytearray()
    for off in range(0, size, chunk):
        d = majority(lambda: rom_read(addr + off, chunk), tries=40, need=3)
        if d is None:
            raise OSError(f"read failed at 0x{addr + off:05X}")
        out += d
    return bytes(out)


def in_bootrom():
    return majority(lambda: rom_read(0xC000, 16), tries=8, need=2) is not None


def cmd_mode(_args):
    chem = sbs_block(0x22)
    if chem == "LION":
        print("Firmware is running (DeviceChemistry = LION). Not in Boot ROM.")
    elif in_bootrom():
        d = majority(lambda: rom_read(0xC040, 16), tries=8, need=2)
        print("BOOT ROM ACTIVE. DF3 @0xC040:", d.hex(" ") if d else "?",
              "|", "".join(chr(c) if 32 <= c < 127 else "." for c in (d or b"")), "|")
    else:
        print("No firmware answer and no Boot ROM answer. Check wiring / noise / power.")


def cmd_dump(args):
    names = ["df1", "df2", "df3"] if args.area == "all" else [args.area]
    os.makedirs(args.out, exist_ok=True)
    for name in names:
        a, size = PRESETS[name]
        print(f"dumping {name} 0x{a:05X}..0x{a + size - 1:05X} ...", flush=True)
        data = rom_dump(a, size)
        fn = os.path.join(args.out, f"{name}.bin")
        with open(fn, "wb") as f:
            f.write(data)
        print(f"  saved {fn}  md5={hashlib.md5(data).hexdigest()}")


def cmd_write(args):
    if args.area != "df1":
        sys.exit("Only df1 is supported by this tool (dynamic area, no checksum).")
    if not args.confirm:
        sys.exit("This ERASES and rewrites flash. Re-run with --confirm if you are sure.")
    a, size = PRESETS["df1"]
    new = open(args.file, "rb").read()
    orig = open(args.expect, "rb").read()
    if len(new) != size or len(orig) != size:
        sys.exit("File size must be exactly 0x400 bytes.")

    print("precheck: reading current DF1 ...", flush=True)
    cur = rom_dump(a, size)
    if cur == new:
        print("DF1 already contains the target data. Nothing to do.")
        return
    if cur not in (orig, b"\xff" * size):
        sys.exit("ABORT: current DF1 differs from --expect file (and is not blank). Nothing erased.")

    print("erase 0x3000 ...", flush=True)
    for _ in range(5):
        try:
            BUS.write(ADDR, [0x20, 3, a & 0xFF, (a >> 8) & 0xFF, (a >> 16) & 0xFF])
            break
        except OSError as e:
            print("  retry:", e)
            BUS.recover()
            time.sleep(0.1)
    else:
        sys.exit("ERASE command failed. Nothing erased.")
    time.sleep(1.5)
    if rom_dump(a, size) != b"\xff" * size:
        sys.exit("ERASE not confirmed. Stop (nothing written). Re-enter Boot ROM and retry.")
    print("erase OK", flush=True)

    print("write ...", flush=True)
    for _ in range(5):
        try:
            BUS.write(ADDR, [0xFD, a & 0xFF, (a >> 8) & 0xFF, (a >> 16) & 0xFF,
                             size & 0xFF, size >> 8] + list(new))
            break
        except OSError as e:
            print("  retry:", e)
            BUS.recover()
            time.sleep(0.1)
    else:
        sys.exit("WRITE command failed. DF1 is blank now: re-enter Boot ROM and run write again.")
    time.sleep(0.5)

    rb = rom_dump(a, size)
    print("readback md5", hashlib.md5(rb).hexdigest(), " target md5", hashlib.md5(new).hexdigest())
    if rb == new:
        print("VERIFY OK")
    else:
        bad = [i for i in range(size) if rb[i] != new[i]]
        sys.exit(f"VERIFY FAIL at {len(bad)} bytes, first: {[hex(i) for i in bad[:10]]}")


def cmd_execute(_args):
    for _ in range(3):
        try:
            BUS.write(ADDR, [0x00, 0x01, 0xD0])     # WriteWord(0x00, 0xD001)
            print("execute sent, firmware should start now")
            return
        except OSError as e:
            print("  retry:", e)
            BUS.recover()
            time.sleep(0.1)
    sys.exit("execute failed")


# --------------------------------------------------------------------------- #
def main():
    global BUS
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--peri-base", type=lambda x: int(x, 0), default=0x3F000000,
                   help="BCM peripheral base: 0x3F000000 for Pi 2/3 (default), 0xFE000000 for Pi 4")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sbs")
    sub.add_parser("mode")
    d = sub.add_parser("dump")
    d.add_argument("area", choices=["df1", "df2", "df3", "all"])
    d.add_argument("-o", "--out", default=".")
    w = sub.add_parser("write")
    w.add_argument("area", choices=["df1"])
    w.add_argument("file")
    w.add_argument("--expect", required=True, help="original DF1 dump; abort if chip content differs")
    w.add_argument("--confirm", action="store_true")
    sub.add_parser("execute")
    args = p.parse_args()

    if os.geteuid() != 0:
        sys.exit("run as root (sudo)")
    realtime()
    devs = hw_i2c_unbind()
    BUS = BitBangI2C(args.peri_base)
    try:
        {"sbs": cmd_sbs, "mode": cmd_mode, "dump": cmd_dump,
         "write": cmd_write, "execute": cmd_execute}[args.cmd](args)
    finally:
        BUS.close()
        hw_i2c_rebind(devs)


BUS = None
if __name__ == "__main__":
    main()
