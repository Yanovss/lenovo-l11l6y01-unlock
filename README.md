# Unlocking a Lenovo L11L6Y01 battery (LGC, Renesas 51F51) with a Raspberry Pi

**English** | [Русский](README.ru.md)

This is a write-up of recovering one locked laptop battery. Nothing has to be bought: a Raspberry Pi,
a multimeter, a soldering iron and two resistors are enough. No programmer (FX2LP, CP2112, EZP) is needed.

> **Warning.** Li-ion packs are dangerous when mishandled. Everything below is at your own risk.
> Unlocking **does not repair worn cells**, it only removes the software lock.
> If the cell groups are unbalanced or any group is below ~3.0 V, deal with the cells first.

---

## 1. Symptoms

- The laptop sees the battery but **does not charge it**: 0 %, `Not charging` / `pending-charge`.
- Same in another laptop. **Check this first**: it immediately proves the battery is at fault.
- Feeding the pack from an external supply, leaving it on the charger, resetting the EC: **no effect**.

`upower` before the repair:

```
$ upower -i /org/freedesktop/UPower/devices/battery_BAT0
  vendor:               LGC
  model:                L11L6Y01
  state:                pending-charge
  energy:               0 Wh
  energy-full:          32.99 Wh
  energy-full-design:   47.52 Wh
  voltage:              10.208 V
  percentage:           0%
```

Cause: after a long deep discharge the battery controller wrote a lock flag (Permanent Failure) into
its flash. From then on it **asks the laptop for 0 mA and 0 mV** itself.

---

## 2. The battery

| Parameter | Value |
|---|---|
| Model | **Lenovo L11L6Y01** (3INR19/65-2), P/N 121500042 |
| Manufacturer | LGC (LG Chem) |
| Rating | 10.8 V, 48 Wh, 4400 mAh, 3S2P, 18650 cells |
| Laptops | IdeaPad G480/G580, Z480/Z580, Y480/Y580, G500/G505/G510, G700/G710 |
| Related models (not tested) | L11S6Y01, L11M6Y01, L11L6F01, L11P6R01 |
| **Controller** | **Renesas R2J240, "51F51" variant**, TSSOP-38, marking `333B1000 / 51F51` |

![Label](images/01_label.jpg)

**This is not a TI bq chip.** Texas Instruments unseal keys (`0x0414/0x3672` etc.) and
"Clear PF via ManufacturerAccess" **do not work** here. The flag can only be cleared by editing the
flash through the built-in boot ROM.

### Connector (7 pins)

Looking at the connector with the label on top. The case has ⊕ on the left and ⊖ on the right.

```
 ⊕ [1][2] ▮ [3][4][5][6][7] ⊖
   B+  B+ key CLK DAT T/ID B− B−
```

| Pin | Signal | Verified by |
|---|---|---|
| 1, 2 | B+ (~10–12.6 V) | multimeter |
| 3 | **SMBCLK** | communication with the Pi |
| 4 | **SMBDAT** | communication with the Pi |
| 5 | thermistor / ID (~10 kΩ to B−) | multimeter |
| 6, 7 | B− | multimeter |

![Connector](images/02_connector.jpg)

The SMBus lines **are not pulled up to pack voltage**: on a disconnected battery they read 0 V,
with ~10 kΩ to B−. So they can go straight to the Raspberry Pi GPIO (3.3 V) without a level shifter.

---

## 3. What you need

| Item | Why |
|---|---|
| Raspberry Pi (tested on **Pi 3 Model B**) | software SMBus and boot ROM access |
| **Power bank** 5 V / 2 A to power the Pi | **required**: a mains PSU caused noise and broke the write |
| Multimeter | checking points and cells |
| Soldering iron, thin wire | wires to TP1 and RESET |
| **2 × 1 kΩ resistors** | for BOOT and RESET |
| 3 female jumper wires | battery connector → GPIO |

### Pi system (at the time of writing)

```
$ uname -a
Linux rpi3 7.1.6-1-aarch64-ARCH #1 SMP PREEMPT_DYNAMIC Wed Aug  5 00:20:13 UTC 2026 aarch64 GNU/Linux
```

- **Arch Linux ARM (aarch64)**, user `alarm`, `sudo` configured.
- Only `python3` from the base system is required. **No `smbus2`, no `i2c-tools`, no internet.**
- The tool accesses GPIO registers directly through `/dev/mem`, so it runs as root.

Other distributions (Raspberry Pi OS etc.) should work the same way. For a Pi 4 pass
`--peri-base 0xFE000000`.

---

## 4. Wiring to the Raspberry Pi

```
Battery                 Raspberry Pi (40-pin)
───────                 ─────────────────────
pin 4 (SMBDAT) ───────► Pin 3  (GPIO2 / SDA)
pin 3 (SMBCLK) ───────► Pin 5  (GPIO3 / SCL)
pin 7 (B−)     ───────► Pin 6  (GND)
pins 1,2 (B+)           DO NOT connect
pin 5                   DO NOT connect
```

- Pin 3 sits next to Pin 2 and Pin 4 (5 V). **Move wires only with the Pi powered off**:
  once the Pi rebooted after an accidental touch.
- Power the Pi **from a power bank**.

### Why not the hardware I2C

The hardware I2C of the BCM2835/2837 (Pi 1/2/3) **handles clock stretching incorrectly**, and the
battery controller stretches the clock all the time. With hardware I2C, `i2cdetect` shows `0x0B`,
but registers read as `0xFFFF` or with errors. That is why `r2j240.py` uses **software I2C** on the
same GPIO2/3 pins and temporarily unbinds the kernel driver while it runs.

---

## 5. Step 1 — diagnostics (read-only)

Copy `tools/r2j240.py` to the Pi and run:

```
$ sudo python3 r2j240.py sbs
```

Output **before the repair** (key lines):

```
[0x09] Voltage             : 10208 mV (10.208 V)
[0x0D] RelativeSOC         : 0 %
[0x10] FullChargeCapacity  : 3299 10mWh
[0x14] ChargingCurrent     : 0 mA
[0x15] ChargingVoltage     : 0 mV (0.000 V)
[0x16] BatteryStatus       : 0x0290
[0x17] CycleCount          : 630
=== Verdict ===
  ChargingCurrent=0 mA, ChargingVoltage=0 mV -> PACK BLOCKS CHARGING (locked)
```

Zeros in `0x14` and `0x15` are the lock: the pack itself refuses to charge.
The flags in `0x16` (`FD`, `RCA`, `INIT`) only mean "empty"; standard SBS does not expose the PF flag here.

---

## 6. Step 2 — open the pack and find the points

The case is glued. Open it carefully along the seam. **Do not touch the cells or the wires from them to the board.**

### Front side: the controller

![Board, front side](images/03_board_front.jpg)
![Controller close-up](images/04_chip_closeup.jpg)

The chip is `51F51` (TSSOP-38). The dot marks pin 1. Relevant pins:

| Pin | Signal | Idle voltage (to V−) |
|---|---|---|
| 3 | VCC | 3.3 V |
| 10 | **BOOT** (active low) | 3.3 V |
| 12 | analog signal, **do not touch** | 1.61 V |
| 13 | **RESET** (active low) | 3.3 V |

Below the chip, on the connector side, there are two round gold pads. They go to **pins 12 and 13**.
RESET uses the **pin 13** pad.

### Back side: TP1 = BOOT

![Board, back side, TP1](images/05_board_back_tp1.jpg)

**TP1** is the round pad next to resistor R11, below diode D7. This is **BOOT** (pin 10).
Check: TP1 reads 3.3 V to V−, and TP1 has continuity to pin 10.

The **V−** point (controller ground) is the pad marked `V-` next to the `R005` shunt.

---

## 7. Step 3 — solder the wires

**Disconnect the wires to the Pi while soldering** (otherwise the iron's mains earth gets connected to the
battery ground through the Pi and the computer). **Do not disconnect the cells**: the controller must stay powered.

```
TP1     ───[1 kΩ]───○ BOOT  (free end)
pin 13  ───[1 kΩ]───○ RESET (free end)
V−      ────────────○ GROUND
```

- Use **two separate** resistors: BOOT and RESET are released at different times.
- Tip at 300–330 °C, no longer than 2–3 s per touch, non-acid flux.
- After soldering check: TP1 = 3.3 V, pin 13 = 3.3 V, pin 12 = 1.61 V. That means no solder bridges.
- **Insulate** the free ends. If RESET accidentally touches ground, the controller keeps rebooting
  and communication falls apart.

RESET wire soldered to the pin 13 pad (the second gold pad, pin 12, stays free):

![RESET wire, close-up](images/06_reset_wire_closeup.jpg)
![RESET wire, overview next to the connector](images/07_reset_wire_overview.jpg)

Then reconnect the Pi and check the board is alive:

```
$ sudo python3 r2j240.py mode
Firmware is running (DeviceChemistry = LION). Not in Boot ROM.
```

---

## 8. Step 4 — enter the boot ROM

Order matters:

| # | Action |
|---|---|
| 1 | touch BOOT to V− and **hold** |
| 2 | touch RESET to V− |
| 3 | wait 1 second |
| 4 | **release RESET** (BOOT still grounded) |
| 5 | wait 2 seconds |
| 6 | release BOOT |

Check:

```
$ sudo python3 r2j240.py mode
BOOT ROM ACTIVE. DF3 @0xC040: 00 00 00 4c 31 31 4c 36 59 30 31 00 00 00 00 00 | ...L11L6Y01..... |
```

The `L11L6Y01` string read from flash means the boot ROM is running and memory is readable.

If communication is flaky (works, then fails), it is almost certainly **noise from a mains power
supply**. Power the Pi from a power bank, unplug the USB-UART adapter and work over Wi-Fi/SSH.

---

## 9. Step 5 — dump the flash (read-only)

```
$ sudo python3 r2j240.py dump all -o backup1
dumping df1 0x03000..0x033FF ...
  saved backup1/df1.bin  md5=9719294932b802e3b1d0f963411916c1
dumping df2 0x03400..0x037FF ...
  saved backup1/df2.bin  md5=9a8918b11878da506f761bc1c9c4ce17
dumping df3 0x0C000..0x0DFFF ...
  saved backup1/df3.bin  md5=320d7f9d99f537f96649036a7a7ece2e

$ sudo python3 r2j240.py dump df1 -o backup2
$ md5sum backup1/df1.bin backup2/df1.bin      # must match
```

| Area | Address | Size | Contents |
|---|---|---|---|
| DF1 | 0x3000 | 1 KB | **dynamic data, log, lock flag** (no checksum) |
| DF2 | 0x3400 | 1 KB | empty (FF) on this pack |
| DF3 | 0xC000 | 8 KB | static data (name, capacity), **checksummed, do not touch** |

**Keep the dumps in several places** before writing anything.

---

## 10. Step 6 — find and fix the flag

```
$ python3 patch_df1.py backup1/df1.bin df1_fixed.bin
log entries (leading 0x00 bytes): 2
entry 0 @0x040: BatteryMode=0xE000 FCC=32990 mWh Cycles=630 flag@0x06B=0x16
entry 1 @0x090: BatteryMode=0xE000 FCC=32990 mWh Cycles=630 flag@0x0BB=0x17
patched offsets: 0x06B, 0x0BB -> df1_fixed.bin
```

How it is laid out:

```
00000000: 0000 ffff ffff ...                       ← "00 00" = 2 log entries
00000010: 4c31 314c 3659 3031 ...                  ← L11L6Y01
00000040: b801 db01 0a00 00e0 e30c f50a 7602 ...   ← entry 0: BatteryMode E000, FCC 0CE3, Cycles 0276
00000060: 0000 0000 0000 0060 93f5 01[16] 0101 ... ← entry 0, flag @0x6B = 0x16
00000090: b801 db01 0a00 00e0 e30c f50a 7602 ...   ← entry 1
000000b0: 0000 0000 0000 0060 93f5 01[17] 6001 ... ← entry 1, flag @0xBB = 0x17
```

- `0x16` / `0x17` (bit 4 = lock) → **`0x03`**.
- **Not `0x00`**: reportedly, with zero the pack shuts off after a few seconds.
- **Only these 2 bytes** change, nothing else.

If your battery has a different layout, `patch_df1.py` stops without writing anything.
Then compare your dump with the examples in `dumps/` manually.

---

## 11. Step 7 — write and start

```
$ sudo python3 r2j240.py write df1 df1_fixed.bin --expect backup1/df1.bin --confirm
precheck: reading current DF1 ...
erase 0x3000 ...
erase OK
write ...
readback md5 26b20b0f8bf877f9b4ef745ef5b2599d  target md5 26b20b0f8bf877f9b4ef745ef5b2599d
VERIFY OK

$ sudo python3 r2j240.py execute
execute sent, firmware should start now
```

What `write` does:
1. Reads DF1 and compares it with `--expect`. If it differs, **nothing is erased**.
2. Erases the block and checks that it is blank.
3. Writes and verifies by reading back.

The boot ROM lives in mask ROM and cannot be erased. If a write fails, enter the boot ROM again and
repeat `write`: a blank (`FF`) block is also accepted as the starting state.

### Check

```
$ sudo python3 r2j240.py sbs
[0x14] ChargingCurrent     : 2800 mA
[0x15] ChargingVoltage     : 12600 mV (12.600 V)
=== Verdict ===
  ChargingCurrent=2800 mA, ChargingVoltage=12600 mV -> pack requests charge (OK)
```

**Before 0 / 0, after 2800 mA / 12600 mV.** The pack asks for charge again.

---

## 12. Step 8 — cells, reassembly, first charge

1. Measure **each of the 3 groups** (B−→T1, T1→T2, T2→B+):

   | Group | Example (this pack) |
   |---|---|
   | 1 | 3.40 V |
   | 2 | 3.42 V |
   | 3 | 3.42 V |

   - difference **up to 0.1 V** and all **above 3.0 V**: OK to charge;
   - imbalance: charge the low group separately (bench supply set to the other groups' voltage, 0.3–0.5 A);
   - a group below ~2.5 V or one that sags within a day: **replace the cells**.
2. Desolder or insulate the TP1/RESET wires, close the case.
3. Insert into the laptop and plug in the charger:

   ```
   $ cat /sys/class/power_supply/BAT0/status
   Charging
   $ upower -i /org/freedesktop/UPower/devices/battery_BAT0 | grep -E "state|percentage|energy-rate|voltage"
       state:               charging
       voltage:             11.34 V
       energy-rate:         25.333 W
       percentage:          10%
   ```

4. First charge **under supervision**, on a non-flammable surface. If the case gets hot, disconnect immediately.

   About 40 minutes later (laptop on, charging at ~2.7 A):

   ```
   $ upower -i /org/freedesktop/UPower/devices/battery_BAT0
       state:               charging
       energy:              23.63 Wh
       energy-full:         28.05 Wh
       energy-full-design:  47.52 Wh
       energy-rate:         32.083 W
       voltage:             11.927 V
       time to full:        8.2 minutes
       percentage:          84%
       capacity:            59.0278%
   ```

   After real charge started flowing, the controller re-estimated the full capacity:
   32.99 Wh → **28.05 Wh (59 % of design)**. That is the real state of these cells
   (630 cycles plus a long deep discharge); a calibration cycle will refine it.
5. After a full charge, calibrate: full discharge, then full charge again.

The cells are worn (630 cycles, ~59 % capacity). The battery works, but holds roughly
half to two thirds of a new one.

---

## 13. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `0x0B` shows in `i2cdetect`, registers read `0xFFFF` / N/A | Pi hardware I2C vs. clock stretching | use `r2j240.py` (software I2C) |
| Random NACKs, bit errors, boot ROM answers on and off | mains PSU noise / ground loop | **power the Pi from a power bank**, unplug USB-UART, work over SSH/Wi-Fi |
| The Pi rebooted while moving wires | touched 5 V on Pin 2/4 | move wires only with the Pi off |
| Communication lost after soldering | free RESET end touches ground | insulate BOOT/RESET ends |
| Multi-line paste into `screen` over UART merges commands | UART console quirk | paste one line at a time or use SSH |
| `mode`: "No firmware answer and no Boot ROM answer" | bad contact, noise, chip held in reset | check wires and power, enter the boot ROM again |

---

## 14. Credits and sources

This work would not have been possible without other people's research. Only sources that were
actually used are listed.

### Main sources

| Source | What was taken from it |
|---|---|
| **Viktor (karosium)**, "Hacking the R2J240 with LGC firmware"<br><https://www.karosium.com/2016/08/hacking-r2j240-lgc.html> | the controller is Renesas R2J240; BOOT pin + TP1; flash areas DF1/DF2/DF3; DF1 has no checksum; log entries counted by leading `0x00` bytes |
| **Viktor (karosium)**, `smbusb_r2j240flasher.c`<br><https://github.com/karosium/smbusb/blob/master/tools/smbusb_r2j240flasher.c> | boot ROM protocol: read `0xFC`, write `0xFD`, erase `0x20`, execute `0x00 = 0xD001`; area addresses and sizes. `r2j240.py` re-implements this protocol |
| **Viktor (karosium)**, SMBusb project<br><https://github.com/karosium/smbusb>, <https://www.karosium.com/p/smbusb.html> | the original USB SMBus tool (FX2LP) this approach comes from |
| **Viktor (karosium)**, "SMBusb - Hacking smart batteries"<br><http://www.karosium.com/2016/08/smbusb-hacking-smart-batteries.html> | general method: dump, find the error flag, rewrite |

### Comments under the karosium R2J240 article

| Author | Contribution |
|---|---|
| **h4tr3d** | 51F51 = R2J240 in TSSOP-38; **pin 10 = BOOT, pin 13 = RESET**, pin 3 = VCC; BOOT goes to TP1; lock byte `0x17 → 0x03` turns ChargingCurrent/Voltage from 0 into normal values. i2c-dev port of the tools: <https://github.com/h4tr3d/smbusb> |
| **ladislav** | DF1 layout of an LGC pack: `0x6B` lock flags (`0x17` locked, `0x03` OK), `0x46` BatteryMode, `0x48` FCC, `0x4C` CycleCount |
| **Man-Eating Monkey** | BOOT/RESET sequence; **set `0x03`, not `0x00`** (with `0x00` the pack shuts off after a few seconds) |
| **Вета** | confirmation that TP1 is the bootloader point on LGC packs with 51F51 |
| **0r10nV** | how to find the PF flag: compare dumps before and after the fault |
| **burgekin** | entering boot mode may require power-up with BOOT/RESET already held low |

### Other

| Source | What was taken from it |
|---|---|
| ThinkPad battery pinout, PinoutGuide<br><https://pinoutguide.com/Power/thinkpad_battery_pinout.shtml> | Lenovo packs have **no internal pull-ups on SMBus**; thermistor is a 10 kΩ NTC to GND |
| laptopu.ro, L11L6Y01 pinout<br><https://www.laptopu.ro/community/pinputs-for-ibm-lenovo-batteries/l11l6y01-3inr19-65-2-battery-pinout/> | L11L6Y01 shares its pinout with L11S6Y01 / L09S6Y01 |
| **Viktor (karosium)**, blog, September 2016<br><http://www.karosium.com/2016/09/> | notes on the thermal fuse in smart battery packs |

Everything specific to this battery (connector pinout, pins 12/13 pads, DF1 offsets `0x6B`/`0xBB`,
mains-noise problem and the power-bank fix, software I2C on the Pi) was checked by hand during this repair.

---

## Repository contents

```
tools/r2j240.py        software SMBus for Raspberry Pi: sbs / mode / dump / write / execute
tools/patch_df1.py     offline lock-flag finder/fixer for a DF1 dump
dumps/                 real dumps of this battery (before and after) for reference
images/                photos of the label, connector and board
```

**Verification status.** The whole procedure was performed on one L11L6Y01 battery with a
Raspberry Pi 3B. Communication with the battery used debugging scripts; `r2j240.py` merges them into
one tool and **has not yet been run end-to-end on hardware**. `patch_df1.py` was tested on the real dump:
its output matches the file that was written byte for byte.

## License

Code: MIT. Text: CC BY 4.0.
