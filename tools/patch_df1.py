#!/usr/bin/env python3
"""
patch_df1.py - find and clear the lock (PF) flag in a DF1 dump of an
LGC battery with Renesas R2J240 / 51F51 controller. Works OFFLINE on a file.

DF1 layout observed on Lenovo L11L6Y01 (LGC, 51F51):
    0x00..          one 0x00 byte per log entry (here: "00 00" = 2 entries), then 0xFF
    0x10            device name, e.g. "L11L6Y01"
    0x40 + k*0x50   log entry k (0x50 bytes each)
        +0x06  u16  BatteryMode            (e.g. 0xE000)
        +0x08  u16  FullChargeCapacity     (10 mWh units)
        +0x0C  u16  CycleCount
        +0x2B  u8   LOCK FLAGS  0x16/0x17 = locked, 0x03 = OK

Set 0x03, NOT 0x00 (reported: with 0x00 the pack shuts off after a few seconds).

Usage:
    python3 patch_df1.py df1.bin df1_fixed.bin
"""
import sys

ENTRY_BASE, ENTRY_SIZE, FLAG_OFF = 0x40, 0x50, 0x2B
LOCKED, OK = (0x16, 0x17), 0x03


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    src, dst = sys.argv[1], sys.argv[2]
    d = bytearray(open(src, "rb").read())
    if len(d) != 0x400:
        sys.exit("DF1 dump must be exactly 1024 bytes")

    n = 0
    while n < 16 and d[n] == 0x00:
        n += 1
    print(f"log entries (leading 0x00 bytes): {n}")
    if n == 0:
        sys.exit("no log entries found - unexpected layout, stop")

    changed = []
    for k in range(n):
        base = ENTRY_BASE + k * ENTRY_SIZE
        mode = d[base + 6] | d[base + 7] << 8
        fcc = d[base + 8] | d[base + 9] << 8
        cyc = d[base + 12] | d[base + 13] << 8
        flag = d[base + FLAG_OFF]
        print(f"entry {k} @0x{base:03X}: BatteryMode=0x{mode:04X} FCC={fcc * 10} mWh "
              f"Cycles={cyc} flag@0x{base + FLAG_OFF:03X}=0x{flag:02X}")
        if (mode & 0xE000) != 0xE000:
            sys.exit("  BatteryMode does not look right - layout differs from L11L6Y01, stop")
        if flag in LOCKED:
            d[base + FLAG_OFF] = OK
            changed.append(base + FLAG_OFF)
        elif flag != OK:
            sys.exit(f"  unknown flag value 0x{flag:02X} - stop, check manually")

    if not changed:
        print("nothing to patch (no locked flags)")
        return
    open(dst, "wb").write(d)
    print("patched offsets:", ", ".join(f"0x{o:03X}" for o in changed), "->", dst)


if __name__ == "__main__":
    main()
