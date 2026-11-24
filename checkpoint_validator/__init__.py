"""虚拟机检查点设备状态与 DMA 一致性校验器（全部为模拟数据）。"""

from .models import (
    DirtyPage,
    DeviceState,
    QueueDescriptor,
    InFlightIO,
    DMAMapping,
    SnapshotEvent,
    RestoreLogEntry,
    ReplayOperation,
    ValidationReport,
)
from .validator import CheckpointValidator

__all__ = [
    "DirtyPage",
    "DeviceState",
    "QueueDescriptor",
    "InFlightIO",
    "DMAMapping",
    "SnapshotEvent",
    "RestoreLogEntry",
    "ReplayOperation",
    "ValidationReport",
    "CheckpointValidator",
]
