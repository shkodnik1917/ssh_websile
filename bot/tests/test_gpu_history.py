from datetime import datetime
from zoneinfo import ZoneInfo

from server_access_bot.gpu_history import (
    RETENTION_SECONDS,
    GpuLoadHistory,
    GpuLoadSample,
    format_gpu_load_history,
    sample_from_status,
)
from server_access_bot.monitoring import GpuDeviceStatus, ServerGpuStatus

MSK = ZoneInfo("Europe/Moscow")


def device(index: int, used: int, util: int) -> GpuDeviceStatus:
    return GpuDeviceStatus(
        index=index,
        name="NVIDIA H200",
        total_memory_mb=1000,
        used_memory_mb=used,
        utilization_percent=util,
    )


def test_sample_averages_cards_and_reads_slurm_pool_allocation() -> None:
    gpus = ServerGpuStatus(
        host="vv_h200",
        reachable=True,
        devices=(device(0, 500, 100), device(1, 100, 20)),
    )
    sample = sample_from_status(gpus, 10)
    assert sample == GpuLoadSample("vv_h200", 10, 60.0, 30.0, 2)

    pool = ServerGpuStatus(
        host="ipavlov_slurm",
        reachable=True,
        devices=(),
        summary="GPU   ███░░ 8/16 занято (50%)\ngpu13 6/8 · gpu14 2/8",
    )
    assert sample_from_status(pool, 10) == GpuLoadSample(
        "ipavlov_slurm", 10, 50.0, None, 16
    )

    down = ServerGpuStatus(host="brain_lab", reachable=False, devices=())
    assert sample_from_status(down, 10) is None


def test_history_keeps_a_month_and_drops_older_rows(tmp_path) -> None:
    history = GpuLoadHistory(tmp_path / "bot.sqlite3")
    now = 10 * RETENTION_SECONDS
    history.record([GpuLoadSample("brain_lab", now - RETENTION_SECONDS - 1, 50, 50, 4)], now=now - RETENTION_SECONDS - 1)
    history.record([GpuLoadSample("brain_lab", now, 70, 40, 4)], now=now)

    rows = history.samples_since(0)
    assert rows == [GpuLoadSample("brain_lab", now, 70.0, 40.0, 4)]


def test_load_history_message_shows_averages_and_week_chart() -> None:
    now = datetime(2026, 11, 9, 13, 0, tzinfo=MSK)
    base = int(now.timestamp())
    samples = [
        GpuLoadSample("vv_h200", base - 3 * 86400, 20.0, 10.0, 8),
        GpuLoadSample("vv_h200", base - 600, 100.0, 90.0, 8),
        GpuLoadSample("ipavlov_slurm", base - 600, 50.0, None, 16),
    ]
    text = format_gpu_load_history(
        samples, ("vv_h200", "ipavlov_slurm", "brain_lab"), now=now
    )

    assert "📈 <b>Нагрузка серверов за 7 дней</b>" in text
    assert "🟢 <b>H200</b> · 8 GPU" in text
    assert "сейчас   UTIL 100% · VRAM  90%" in text
    assert "7 дней   UTIL  60% · VRAM  50%" in text
    assert "🟢 <b>iPavlov · H200 Slurm</b> · 16 GPU" in text
    assert "сейчас   занято  50%" in text
    assert "⚪ <b>Новый сервер (BrainLab)</b>\nДанных пока нет." in text
    assert "     вт пн ср чт пт сб вс" not in text
    assert "     вт ср чт пт сб вс пн" in text
    week_line = next(line for line in text.splitlines() if line.startswith("UTIL ▁") or line.startswith("UTIL ·"))
    assert "▂" in week_line and "█" in week_line
