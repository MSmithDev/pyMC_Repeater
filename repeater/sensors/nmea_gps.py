"""
NMEA GPS sensor plug-in.

Reads NMEA 0183 sentences directly from a serial GPS receiver and exposes
fix status, position, motion, accuracy, and satellite fields as sensor
readings.

The repeater's built-in GPS service must be disabled (gps.enabled: false in
config.yaml) when using this plug-in.  Both cannot share the serial port
simultaneously.  Set gps.api_fallback_to_config_location: true so the
repeater continues advertising the manually-configured lat/lon.

Requires: pyserial (already installed with pyMC_Repeater)

Config example:
  - type: nmea_gps
    name: "gps"
    enabled: true
    auto_install_packages: false
    settings:
      device: /dev/serial0      # Serial device path
      baud_rate: 9600           # GPS baud rate (usually 9600 or 115200)
      read_timeout_seconds: 3.0 # Max time to wait for a GGA+RMC sentence pair
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from .base import SensorBase
from .registry import SensorRegistry

_FIX_QUALITY = {
    "0": "no fix", "1": "GPS",      "2": "DGPS",
    "4": "RTK fixed", "5": "RTK float",
    "6": "estimated", "7": "manual", "8": "simulation",
}
_GSA_FIX_TYPE = {"1": "no fix", "2": "2D fix", "3": "3D fix"}


def _checksum_valid(sentence: str) -> bool:
    if "*" not in sentence:
        return True  # no checksum present — accept
    try:
        payload, cs_str = sentence[1:].rsplit("*", 1)
        expected = 0
        for ch in payload:
            expected ^= ord(ch)
        return expected == int(cs_str[:2], 16)
    except (ValueError, IndexError):
        return False


def _nmea_coord(value: str, hemisphere: str) -> Optional[float]:
    """Convert NMEA DDDMM.MMMMM + hemisphere to signed decimal degrees."""
    if not value:
        return None
    try:
        # Latitude is DDMM, longitude is DDDMM
        dot = value.index(".")
        degrees = float(value[: dot - 2])
        minutes = float(value[dot - 2 :])
        decimal = degrees + minutes / 60.0
        if hemisphere.upper() in ("S", "W"):
            decimal *= -1
        return round(decimal, 8)
    except (ValueError, IndexError):
        return None


def _to_float(value: str) -> Optional[float]:
    try:
        return float(value) if value else None
    except ValueError:
        return None


def _to_int(value: str) -> Optional[int]:
    try:
        return int(value) if value else None
    except ValueError:
        return None


def _parse_gga(fields: list) -> dict:
    """Parse $xxGGA sentence fields into a dict."""
    # $xxGGA,time,lat,N/S,lon,E/W,quality,numSV,HDOP,alt,M,sep,M,...
    try:
        return {
            "latitude":      _nmea_coord(fields[2], fields[3]) if len(fields) > 3 else None,
            "longitude":     _nmea_coord(fields[4], fields[5]) if len(fields) > 5 else None,
            "fix_quality":   fields[6] if len(fields) > 6 else "0",
            "satellites_used": _to_int(fields[7]) if len(fields) > 7 else None,
            "hdop":          _to_float(fields[8]) if len(fields) > 8 else None,
            "altitude_m":    _to_float(fields[9]) if len(fields) > 9 else None,
        }
    except Exception:
        return {}


def _parse_rmc(fields: list) -> dict:
    """Parse $xxRMC sentence fields into a dict."""
    # $xxRMC,time,status,lat,N/S,lon,E/W,speed_kn,course,date,...
    try:
        status = fields[2] if len(fields) > 2 else "V"
        date_str = fields[9] if len(fields) > 9 else ""
        time_str = fields[1] if len(fields) > 1 else ""
        utc_dt = None
        if len(date_str) == 6 and len(time_str) >= 6:
            d, m, y = date_str[0:2], date_str[2:4], date_str[4:6]
            h, mi, s = time_str[0:2], time_str[2:4], time_str[4:6]
            year = 2000 + int(y) if int(y) < 80 else 1900 + int(y)
            utc_dt = f"{year}-{m}-{d}T{h}:{mi}:{s}Z"
        speed_kn = _to_float(fields[7]) if len(fields) > 7 else None
        return {
            "fix_valid":      status == "A",
            "speed_kmh":      round(speed_kn * 1.852, 2) if speed_kn is not None else None,
            "course_degrees": _to_float(fields[8]) if len(fields) > 8 else None,
            "utc_datetime":   utc_dt,
        }
    except Exception:
        return {}


def _parse_gsa(fields: list) -> dict:
    """Parse $xxGSA sentence fields into a dict."""
    # $xxGSA,mode,fixType,sv...,PDOP,HDOP,VDOP[,systemId]
    try:
        fix_type = fields[2] if len(fields) > 2 else "1"
        # PDOP/HDOP/VDOP are at indices 15/16/17 (after 12 SV slots at 3-14)
        pdop = _to_float(fields[15]) if len(fields) > 15 else None
        hdop = _to_float(fields[16]) if len(fields) > 16 else None
        # VDOP field may contain trailing checksum — strip it
        vdop_raw = fields[17].split("*")[0] if len(fields) > 17 else ""
        vdop = _to_float(vdop_raw)
        return {
            "fix_type": fix_type,
            "pdop": pdop,
            "hdop": hdop,
            "vdop": vdop,
        }
    except Exception:
        return {}


@SensorRegistry.register("nmea_gps")
class NmeaGpsSensor(SensorBase):
    sensor_type = "nmea_gps"

    def __init__(self, name: str, config: Optional[Dict[str, Any]] = None, log=None):
        super().__init__(name=name, config=config, log=log)

        self.device       = self.settings.get("device", "/dev/serial0")
        self.baud_rate    = int(self.settings.get("baud_rate", 9600))
        self.read_timeout = float(self.settings.get("read_timeout_seconds", 3.0))

        self.available = False

        if not self.ensure_python_modules([("serial", "pyserial")]):
            return

        try:
            import serial  # type: ignore[import-not-found]
            self._serial = serial

            # Verify port is accessible
            port = serial.Serial(self.device, self.baud_rate, timeout=1.0)
            port.close()

            self.available = True
            self.log.info(
                "NMEA GPS initialized (device=%s, baud=%d)",
                self.device,
                self.baud_rate,
            )
        except Exception as exc:
            self.log.warning("NMEA GPS init failed (%s): %s", self.device, exc)

    def _read(self) -> Dict[str, Any]:
        """Read one sentence cycle from the GPS receiver and return parsed fields."""
        if not self.available:
            raise RuntimeError("NMEA GPS not available")

        gga: Optional[list] = None
        rmc: Optional[list] = None
        gsa: Optional[list] = None

        deadline = time.monotonic() + self.read_timeout

        try:
            port = self._serial.Serial(self.device, self.baud_rate, timeout=0.5)
        except Exception as exc:
            raise RuntimeError(f"NMEA GPS serial open failed: {exc}") from exc

        try:
            while time.monotonic() < deadline:
                try:
                    raw = port.readline()
                except Exception:
                    continue

                try:
                    line = raw.decode("ascii", errors="replace").strip()
                except Exception:
                    continue

                if not line.startswith("$") or not _checksum_valid(line):
                    continue

                # Sentence type is chars 3-5 (strip 2-char talker prefix, e.g. GN/GP/GL)
                fields = line.split(",")
                if len(fields[0]) < 6:
                    continue
                sentence_type = fields[0][3:]  # GGA, RMC, GSA, …

                if sentence_type == "GGA" and gga is None:
                    gga = fields
                elif sentence_type == "RMC" and rmc is None:
                    rmc = fields
                elif sentence_type == "GSA" and gsa is None:
                    gsa = fields

                if gga is not None and rmc is not None:
                    break  # GSA is optional; stop as soon as we have the essentials
        finally:
            port.close()

        if gga is None and rmc is None:
            raise RuntimeError(
                f"NMEA GPS: no sentences received within {self.read_timeout}s"
            )

        gga_data = _parse_gga(gga) if gga else {}
        rmc_data = _parse_rmc(rmc) if rmc else {}
        gsa_data = _parse_gsa(gsa) if gsa else {}

        fix_valid   = rmc_data.get("fix_valid", False)
        fix_quality = _FIX_QUALITY.get(gga_data.get("fix_quality", "0"), "no fix")
        fix_type    = _GSA_FIX_TYPE.get(gsa_data.get("fix_type", "1"), "no fix")

        # Only report position/motion when fix is valid
        latitude   = gga_data.get("latitude")   if fix_valid else None
        longitude  = gga_data.get("longitude")  if fix_valid else None
        altitude_m = gga_data.get("altitude_m") if fix_valid else None

        # HDOP: prefer GSA (averaged across all GNSS systems); fall back to GGA
        hdop = gsa_data.get("hdop") or gga_data.get("hdop")

        return {
            "fix_valid":        fix_valid,
            "fix_quality":      fix_quality,
            "fix_type":         fix_type,
            "latitude":         latitude,
            "longitude":        longitude,
            "altitude_m":       altitude_m,
            "speed_kmh":        rmc_data.get("speed_kmh")        if fix_valid else None,
            "course_degrees":   rmc_data.get("course_degrees")   if fix_valid else None,
            "hdop":             hdop,
            "pdop":             gsa_data.get("pdop"),
            "vdop":             gsa_data.get("vdop"),
            "satellites_used":  gga_data.get("satellites_used"),
            "utc_datetime":     rmc_data.get("utc_datetime"),
        }
