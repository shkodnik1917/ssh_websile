"""GPU load history per server and its 7-day text chart for Telegram.

Every SAMPLE_INTERVAL_SECONDS the status scan is reduced to one row per server:
mean GPU utilization and mean VRAM share over its cards. Slurm pools (Alibaba,
iPavlov H200) report only how many GPUs are allocated, so their row carries the
allocated share and no VRAM. Rows older than RETENTION_SECONDS are dropped:
about 7 servers x 144 rows a day x 31 days, a few MB.
"""

import asyncio
import html
import logging
import re
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .monitoring import (
    MOSCOW_TIMEZONE,
    PUBLIC_SERVER_NAMES,
    STATUS_ONLY_SERVER_NAMES,
    GpuStatusScanner,
    ServerGpuStatus,
)

__all__ = [
    "LOAD_HISTORY_BUTTON",
    "GpuLoadHistory",
    "GpuLoadSample",
    "format_gpu_load_history",
    "run_gpu_load_history_loop",
    "sample_from_status",
]

LOGGER = logging.getLogger(__name__)
LOAD_HISTORY_BUTTON = "📈 Нагрузка за неделю"
SAMPLE_INTERVAL_SECONDS = 600
RETENTION_SECONDS = 31 * 86400
CHART_DAYS = 7
BUCKETS_PER_DAY = 3
SPARK = "▁▂▃▄▅▆▇█"
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
_SUMMARY_GPU_RE = re.compile(r"(\d+)/(\d+) занято")


@dataclass(frozen=True)
class GpuLoadSample:
    host: str
    sampled_at: int
    utilization_percent: float
    memory_percent: float | None
    cards: int


def sample_from_status(status: ServerGpuStatus, now: int) -> GpuLoadSample | None:
    if not status.reachable or status.driver_hung:
        return None
    if status.devices:
        cards = len(status.devices)
        utilization = sum(d.utilization_percent for d in status.devices) / cards
        shares = [
            100 * d.used_memory_mb / d.total_memory_mb
            for d in status.devices
            if d.total_memory_mb
        ]
        memory = sum(shares) / len(shares) if shares else None
        return GpuLoadSample(status.host, now, utilization, memory, cards)
    if status.summary:
        match = _SUMMARY_GPU_RE.search(status.summary.splitlines()[0])
        if match and int(match.group(2)):
            allocated, total = int(match.group(1)), int(match.group(2))
            return GpuLoadSample(status.host, now, 100 * allocated / total, None, total)
    return None


class GpuLoadHistory:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS gpu_load_samples (
                    host TEXT NOT NULL,
                    sampled_at INTEGER NOT NULL,
                    utilization_percent REAL NOT NULL,
                    memory_percent REAL,
                    cards INTEGER NOT NULL,
                    PRIMARY KEY (host, sampled_at)
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def record(self, samples: Sequence[GpuLoadSample], *, now: int) -> None:
        with self._connect() as db:
            db.executemany(
                "INSERT OR REPLACE INTO gpu_load_samples VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        s.host,
                        s.sampled_at,
                        s.utilization_percent,
                        s.memory_percent,
                        s.cards,
                    )
                    for s in samples
                ],
            )
            db.execute(
                "DELETE FROM gpu_load_samples WHERE sampled_at < ?",
                (now - RETENTION_SECONDS,),
            )

    def samples_since(self, since: int) -> list[GpuLoadSample]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT host, sampled_at, utilization_percent, memory_percent, cards "
                "FROM gpu_load_samples WHERE sampled_at >= ? ORDER BY sampled_at",
                (since,),
            ).fetchall()
        return [GpuLoadSample(*row) for row in rows]


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _percent(value: float | None) -> str:
    return "  —" if value is None else f"{round(value):3d}%"


def _spark(
    samples: Sequence[GpuLoadSample],
    pick: str,
    start_ts: int,
    now_ts: int,
) -> str:
    bucket_seconds = 86400 // BUCKETS_PER_DAY
    buckets: list[list[float]] = [[] for _ in range(CHART_DAYS * BUCKETS_PER_DAY)]
    for sample in samples:
        value = getattr(sample, pick)
        index = (sample.sampled_at - start_ts) // bucket_seconds
        if value is not None and 0 <= index < len(buckets):
            buckets[index].append(value)
    chars = []
    for index, values in enumerate(buckets):
        if start_ts + index * bucket_seconds > now_ts:
            chars.append(" ")
        elif values:
            mean = sum(values) / len(values)
            chars.append(SPARK[min(len(SPARK) - 1, round(mean / 100 * (len(SPARK) - 1)))])
        else:
            chars.append("·")
    return "".join(chars).rstrip()


def format_gpu_load_history(
    samples: Sequence[GpuLoadSample],
    hosts: Sequence[str],
    *,
    now: datetime,
) -> str:
    now = now.astimezone(MOSCOW_TIMEZONE)
    now_ts = int(now.timestamp())
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=CHART_DAYS - 1)
    start_ts = int(start.timestamp())
    day_labels = "".join(
        WEEKDAYS[(start + timedelta(days=day)).weekday()].ljust(BUCKETS_PER_DAY)
        for day in range(CHART_DAYS)
    ).rstrip()
    sections = [
        f"📈 <b>Нагрузка серверов за {CHART_DAYS} дней</b>",
        (
            f"Обновлено: {now:%d.%m.%Y, %H:%M} МСК\n"
            "Средние по всем картам сервера: UTIL — загрузка GPU, VRAM — занятая "
            "память. Для Slurm-пулов — доля выделенных карт. "
            f"Столбик = {24 // BUCKETS_PER_DAY} ч, · — нет данных."
        ),
    ]
    for host in hosts:
        name = html.escape(
            PUBLIC_SERVER_NAMES.get(host) or STATUS_ONLY_SERVER_NAMES.get(host, host)
        )
        own = [s for s in samples if s.host == host]
        if not own:
            sections.append(f"⚪ <b>{name}</b>\nДанных пока нет.")
            continue
        latest = own[-1]
        week = [s for s in own if s.sampled_at >= now_ts - CHART_DAYS * 86400]
        span_days = (now_ts - own[0].sampled_at) // 86400 + 1
        pool = latest.memory_percent is None
        label = "GPU " if pool else "UTIL"

        def averages(rows: Sequence[GpuLoadSample]) -> str:
            utilization = _mean([r.utilization_percent for r in rows])
            if pool:
                return f"занято {_percent(utilization)}"
            memory = _mean([r.memory_percent for r in rows if r.memory_percent is not None])
            return f"UTIL {_percent(utilization)} · VRAM {_percent(memory)}"

        lines = [
            f"сейчас   {averages([latest])}",
            f"{CHART_DAYS} дней   {averages(week)}",
        ]
        if span_days > CHART_DAYS:
            lines.append(f"{min(span_days, 31):2d} дней  {averages(own)}")
        lines.append("")
        lines.append(f"{label} {_spark(week, 'utilization_percent', start_ts, now_ts)}")
        if not pool:
            lines.append(f"VRAM {_spark(week, 'memory_percent', start_ts, now_ts)}")
        lines.append(f"     {day_labels}")
        sections.append(
            f"🟢 <b>{name}</b> · {latest.cards} GPU\n<code>"
            + html.escape("\n".join(lines))
            + "</code>"
        )
    return "\n\n".join(sections)


async def run_gpu_load_history_loop(
    scanner: GpuStatusScanner,
    history: GpuLoadHistory,
    *,
    interval_seconds: int = SAMPLE_INTERVAL_SECONDS,
) -> None:
    while True:
        try:
            statuses = await asyncio.to_thread(scanner.scan)
            now = int(time.time())
            samples = [
                sample
                for status in statuses
                if (sample := sample_from_status(status, now)) is not None
            ]
            await asyncio.to_thread(history.record, samples, now=now)
        except Exception:
            LOGGER.exception("GPU load history sampling failed")
        await asyncio.sleep(interval_seconds)
