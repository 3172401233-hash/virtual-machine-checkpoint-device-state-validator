"""检查点一致性校验逻辑。

校验规则：
1. 内存脏页、设备寄存器、队列描述符、未完成 I/O、DMA 映射必须来自同一暂停边界。
2. 快照顺序必须满足：先 pause，设备保存在暂停之后执行。
3. 每个未完成 I/O 的描述符必须已保存、DMA 映射有效、且缓冲区页面在内存快照中；
   否则描述符不可恢复，必须明确失败（fail_io）。
4. 设备恢复顺序：内存 -> DMA 映射 -> 设备（按依赖拓扑排序）。
5. 恢复日志必须动作成对、重试合法，且与检查点同一边界。
"""

from typing import Dict, List, Optional, Sequence, Set

from .models import (
    DMAMapping,
    DeviceState,
    DirtyPage,
    InFlightIO,
    QueueDescriptor,
    ReplayOperation,
    RestoreLogEntry,
    SnapshotEvent,
    ValidationReport,
)

_SNAPSHOT_KINDS = {"pause", "memory_start", "memory_end", "device_save"}
_LOG_ACTIONS = {"restore_start", "restore_ok", "restore_fail", "retry"}


class CheckpointValidator:
    """校验虚拟机检查点能否安全恢复。"""

    def __init__(self, dirty_pages, devices, descriptors, in_flight,
                 dma_mappings, snapshot_events, restore_log):
        # type: (Sequence[DirtyPage], Sequence[DeviceState], Sequence[QueueDescriptor], Sequence[InFlightIO], Sequence[DMAMapping], Sequence[SnapshotEvent], Sequence[RestoreLogEntry]) -> None
        self.dirty_pages = list(dirty_pages)
        self.devices = list(devices)
        self.descriptors = list(descriptors)
        self.in_flight = list(in_flight)
        self.dma_mappings = list(dma_mappings)
        self.snapshot_events = sorted(snapshot_events, key=lambda e: e.sequence)
        self.restore_log = sorted(restore_log, key=lambda e: e.sequence)

    def validate(self):
        # type: () -> ValidationReport
        report = ValidationReport(restorable=True)
        boundary = self._check_boundary(report)
        self._check_snapshot_order(report)
        failed_devices = self._check_devices(report)
        report.failed_devices = sorted(failed_devices)
        report.restore_order = self._restore_order(failed_devices)
        if boundary is not None:
            self._check_in_flight_dma(report, failed_devices)
            self._check_pending_interrupts(report, failed_devices)
        self._check_restore_log(report, boundary)
        if report.errors:
            report.restorable = False
        return report

    def _check_boundary(self, report):
        # type: (ValidationReport) -> Optional[str]
        groups = {
            "内存脏页": {p.boundary_id for p in self.dirty_pages},
            "设备寄存器": {d.boundary_id for d in self.devices},
            "队列描述符": {d.boundary_id for d in self.descriptors},
            "未完成I/O": {io.boundary_id for io in self.in_flight},
            "DMA映射": {m.boundary_id for m in self.dma_mappings},
            "快照事件": {e.boundary_id for e in self.snapshot_events},
        }
        all_ids = set()  # type: Set[str]
        for name, ids in groups.items():
            if len(ids) > 1:
                report.errors.append("%s 内部存在多个暂停边界: %s" % (name, sorted(ids)))
            all_ids |= ids
        if len(all_ids) > 1:
            report.errors.append(
                "内存与设备状态不来自同一暂停边界: %s" % sorted(all_ids)
            )
            return None
        return next(iter(all_ids)) if all_ids else None

    def _check_snapshot_order(self, report):
        # type: (ValidationReport) -> None
        paused = False
        seen_pause = False
        for event in self.snapshot_events:
            if event.kind not in _SNAPSHOT_KINDS:
                report.errors.append("未知快照事件: %s" % event.kind)
                continue
            if event.kind == "pause":
                paused = True
                seen_pause = True
            elif event.kind == "device_save" and not paused:
                report.errors.append(
                    "设备 %s 在暂停边界之前保存，状态可能不一致" % event.device_id
                )
            elif event.kind == "memory_start" and not paused:
                report.errors.append("内存快照在虚拟机暂停之前开始")
        if self.snapshot_events and not seen_pause:
            report.errors.append("快照序列缺少 pause 事件，不存在一致的暂停边界")

    def _check_devices(self, report):
        # type: (ValidationReport) -> Set[str]
        failed = set()  # type: Set[str]
        known = {d.device_id for d in self.devices}
        for device in self.devices:
            if not device.saved:
                report.errors.append(
                    "设备 %s 只保存了部分状态（如仅队列指针），无法恢复" % device.device_id
                )
                failed.add(device.device_id)
            for dep in device.depends_on:
                if dep not in known:
                    report.errors.append(
                        "设备 %s 依赖的设备 %s 不在检查点中" % (device.device_id, dep)
                    )
                    failed.add(device.device_id)
        changed = True
        while changed:
            changed = False
            for device in self.devices:
                if device.device_id in failed:
                    continue
                if any(dep in failed for dep in device.depends_on):
                    report.errors.append(
                        "设备 %s 依赖的设备不可恢复，级联失败" % device.device_id
                    )
                    failed.add(device.device_id)
                    changed = True
        return failed

    def _restore_order(self, failed_devices):
        # type: (Set[str]) -> List[str]
        order = ["guest-memory", "dma-mappings"]
        devices = {
            d.device_id: d for d in self.devices if d.device_id not in failed_devices
        }
        placed = set()  # type: Set[str]
        while devices:
            ready = sorted(
                device_id
                for device_id, device in devices.items()
                if all(dep in placed for dep in device.depends_on)
            )
            if not ready:
                order.extend(sorted(devices))
                break
            order.extend(ready)
            placed.update(ready)
            for device_id in ready:
                del devices[device_id]
        return order

    def _check_in_flight_dma(self, report, failed_devices):
        # type: (ValidationReport, Set[str]) -> None
        saved_pages = {p.gpa for p in self.dirty_pages}
        descriptors = {d.descriptor_id: d for d in self.descriptors}  # type: Dict[str, QueueDescriptor]
        for io in self.in_flight:
            descriptor = descriptors.get(io.descriptor_id)
            reason = self._unrecoverable_reason(io, descriptor, saved_pages, failed_devices)
            if reason is None:
                report.replay_operations.append(
                    ReplayOperation(
                        kind="replay_io",
                        device_id=io.device_id,
                        request_id=io.request_id,
                        detail="重建未完成 DMA 请求 %s（描述符 %s）"
                        % (io.request_id, io.descriptor_id),
                    )
                )
            else:
                report.unrecoverable_descriptors.append(io.descriptor_id)
                report.replay_operations.append(
                    ReplayOperation(
                        kind="fail_io",
                        device_id=io.device_id,
                        request_id=io.request_id,
                        detail="明确失败请求 %s：%s" % (io.request_id, reason),
                    )
                )
        report.unrecoverable_descriptors.sort()

    def _unrecoverable_reason(self, io, descriptor, saved_pages, failed_devices):
        # type: (InFlightIO, Optional[QueueDescriptor], Set[int], Set[str]) -> Optional[str]
        if io.device_id in failed_devices:
            return "所属设备状态未完整保存"
        if descriptor is None:
            return "描述符 %s 未保存（快照只保存了队列指针）" % io.descriptor_id
        if not descriptor.saved:
            return "描述符 %s 内容丢失（快照只保存了队列指针）" % io.descriptor_id
        mapping = self._find_mapping(descriptor)
        if mapping is None:
            return "描述符 %s 缺少 DMA 映射" % descriptor.descriptor_id
        if not mapping.active:
            return "描述符 %s 指向旧 DMA 地址 0x%x，映射已失效" % (
                descriptor.descriptor_id,
                descriptor.gpa,
            )
        missing = [p for p in descriptor.pages if p not in saved_pages]
        if missing:
            return "描述符 %s 缓冲区页面不在内存快照中: %s" % (
                descriptor.descriptor_id,
                ["0x%x" % p for p in missing],
            )
        return None

    def _find_mapping(self, descriptor):
        # type: (QueueDescriptor) -> Optional[DMAMapping]
        for mapping in self.dma_mappings:
            if mapping.device_id != descriptor.device_id:
                continue
            if mapping.covers(descriptor.gpa, descriptor.length):
                return mapping
        return None

    def _check_pending_interrupts(self, report, failed_devices):
        # type: (ValidationReport, Set[str]) -> None
        for device in self.devices:
            if device.device_id in failed_devices:
                continue
            if device.pending_interrupt:
                report.replay_operations.append(
                    ReplayOperation(
                        kind="replay_interrupt",
                        device_id=device.device_id,
                        detail="重放设备 %s 挂起的队列中断 0x%x"
                        % (device.device_id, device.pending_interrupt),
                    )
                )

    def _check_restore_log(self, report, boundary):
        # type: (ValidationReport, Optional[str]) -> None
        state = {}  # type: Dict[str, str]
        for entry in self.restore_log:
            if entry.action not in _LOG_ACTIONS:
                report.errors.append("未知恢复动作: %s" % entry.action)
                continue
            if boundary is not None and entry.boundary_id != boundary:
                report.errors.append(
                    "恢复日志中设备 %s 的边界 %s 与检查点边界 %s 不一致"
                    % (entry.device_id, entry.boundary_id, boundary)
                )
            prev = state.get(entry.device_id)
            if entry.action == "restore_start":
                if prev not in (None, "retry"):
                    report.errors.append(
                        "设备 %s 重复 restore_start（前一状态 %s）"
                        % (entry.device_id, prev)
                    )
            elif entry.action in ("restore_ok", "restore_fail"):
                if prev != "restore_start":
                    report.errors.append(
                        "设备 %s 的 %s 缺少对应的 restore_start"
                        % (entry.device_id, entry.action)
                    )
            elif entry.action == "retry":
                if prev != "restore_fail":
                    report.errors.append(
                        "设备 %s 在未失败的情况下重试恢复" % entry.device_id
                    )
            state[entry.device_id] = entry.action
