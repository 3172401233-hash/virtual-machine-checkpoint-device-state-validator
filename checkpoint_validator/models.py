"""检查点校验的输入/输出数据模型。

所有设备状态均为模拟数据，仅用于校验逻辑演示。
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

PAGE_SIZE = 4096


@dataclass(frozen=True)
class DirtyPage:
    """内存快照中记录的脏页（按页对齐的客户物理地址）。"""

    gpa: int
    boundary_id: str


@dataclass(frozen=True)
class DeviceState:
    """半虚拟化设备在暂停边界保存的寄存器状态。

    saved=False 表示检查点只保存了部分状态（例如只存了队列指针）。
    pending_interrupt 非零表示恢复后需要重放的队列中断。
    """

    device_id: str
    registers: Dict[str, int]
    boundary_id: str
    saved: bool = True
    depends_on: Tuple[str, ...] = ()

    @property
    def pending_interrupt(self) -> int:
        return self.registers.get("pending_interrupt", 0)


@dataclass(frozen=True)
class QueueDescriptor:
    """队列描述符。saved=False 表示快照只保存了队列指针，描述符内容丢失。"""

    descriptor_id: str
    device_id: str
    queue_id: int
    index: int
    gpa: int
    length: int
    boundary_id: str
    saved: bool = True

    @property
    def pages(self) -> List[int]:
        first = self.gpa // PAGE_SIZE
        last = (self.gpa + max(self.length, 1) - 1) // PAGE_SIZE
        return [page * PAGE_SIZE for page in range(first, last + 1)]


@dataclass(frozen=True)
class InFlightIO:
    """暂停时仍未完成的 I/O 请求。"""

    request_id: str
    device_id: str
    descriptor_id: str
    boundary_id: str


@dataclass(frozen=True)
class DMAMapping:
    """设备 DMA 地址映射。active=False 表示映射已在快照前失效（旧地址）。"""

    device_id: str
    gpa: int
    length: int
    boundary_id: str
    active: bool = True

    def covers(self, gpa: int, length: int) -> bool:
        return self.gpa <= gpa and gpa + length <= self.gpa + self.length


@dataclass(frozen=True)
class SnapshotEvent:
    """快照顺序事件。kind 取值为 pause / memory_start / memory_end / device_save。"""

    sequence: int
    kind: str
    boundary_id: str
    device_id: Optional[str] = None


@dataclass(frozen=True)
class RestoreLogEntry:
    """恢复日志。action 取值为 restore_start / restore_ok / restore_fail / retry。"""

    sequence: int
    device_id: str
    action: str
    boundary_id: str


@dataclass(frozen=True)
class ReplayOperation:
    """恢复后需要重放的操作。kind 取值为 replay_io / fail_io / replay_interrupt。"""

    kind: str
    device_id: str
    detail: str
    request_id: Optional[str] = None


@dataclass
class ValidationReport:
    """校验结果。"""

    restorable: bool
    errors: List[str] = field(default_factory=list)
    restore_order: List[str] = field(default_factory=list)
    failed_devices: List[str] = field(default_factory=list)
    unrecoverable_descriptors: List[str] = field(default_factory=list)
    replay_operations: List[ReplayOperation] = field(default_factory=list)
