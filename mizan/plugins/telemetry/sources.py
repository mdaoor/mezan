from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone

from mizan.plugins.base import TelemetrySampleData, TelemetrySource

_now = lambda: datetime.now(timezone.utc)  # noqa: E731


class NoTelemetry(TelemetrySource):
    kind = "none"

    async def sample(self) -> list[TelemetrySampleData]:
        return []


def _f(x: str):
    try:
        return float(x)
    except ValueError:
        return None  # nvidia-smi prints "[N/A]"


class NvidiaSMI(TelemetrySource):
    """Polls ``nvidia-smi`` (must be on PATH of the telemetry worker)."""
    kind = "nvidia_smi"
    QUERY = "index,utilization.gpu,memory.used,temperature.gpu,power.draw"

    async def sample(self) -> list[TelemetrySampleData]:
        try:
            p = await asyncio.create_subprocess_exec(
                self.config.get("binary", "nvidia-smi"), f"--query-gpu={self.QUERY}", "--format=csv,noheader,nounits",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(p.communicate(), 5)
        except (FileNotFoundError, asyncio.TimeoutError):
            return []
        ts, res = _now(), []
        for line in out.decode().strip().splitlines():
            c = [x.strip() for x in line.split(",")]
            if len(c) >= 5:
                res.append(TelemetrySampleData(ts, f"gpu{c[0]}", _f(c[1]), _f(c[2]), _f(c[3]), _f(c[4])))
        return res


class DCGMPrometheus(TelemetrySource):
    """Scrapes a DCGM exporter ``/metrics`` endpoint (config: ``url``)."""
    kind = "dcgm_prometheus"
    FIELDS = {"DCGM_FI_DEV_GPU_UTIL": "util_pct", "DCGM_FI_DEV_FB_USED": "mem_used_mb",
              "DCGM_FI_DEV_GPU_TEMP": "temp_c", "DCGM_FI_DEV_POWER_USAGE": "power_w"}
    LINE = re.compile(r'^(DCGM_FI_DEV_\w+)\{([^}]*)\}\s+([-+0-9.eE]+)')

    async def sample(self) -> list[TelemetrySampleData]:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                text = (await c.get(self.config.get("url", "http://localhost:9400/metrics"))).text
        except httpx.HTTPError:
            return []
        devs: dict[str, TelemetrySampleData] = {}
        ts = _now()
        for line in text.splitlines():
            m = self.LINE.match(line)
            if not m or m[1] not in self.FIELDS:
                continue
            gpu = re.search(r'gpu="(\d+)"', m[2])
            d = devs.setdefault(gpu[1] if gpu else "0", TelemetrySampleData(ts, f"gpu{gpu[1] if gpu else 0}"))
            setattr(d, self.FIELDS[m[1]], float(m[3]))
        return list(devs.values())
