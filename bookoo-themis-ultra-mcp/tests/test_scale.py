from bookoo_themis_ultra_mcp.protocol import AutoModeEvent, PowderWeight
from bookoo_themis_ultra_mcp.scale import BookooScale

AUTO_MODE_EVENT = bytearray.fromhex("03 0d 00 00 75 30 2b 00 0e 10 2b 00 c8 00 00 00 00 00 00 9d")
POWDER_WEIGHT = bytearray.fromhex("03 0f 2b 00 07 3a 00 00 00 00 00 00 00 00 00 00 00 00 00 1a")


def test_notifications_keep_the_latest_packets() -> None:
    scale = BookooScale()
    scale._on_notification(None, POWDER_WEIGHT)
    scale._on_notification(None, AUTO_MODE_EVENT)
    scale._on_notification(None, bytearray.fromhex("03 0f 2b"))  # malformed: ignored

    assert scale.powder_weight == PowderWeight(weight_g=18.5)
    assert scale.auto_mode_event == AutoModeEvent(event="stopped", timer_ms=30000, weight_g=36.0, result=2.0)
