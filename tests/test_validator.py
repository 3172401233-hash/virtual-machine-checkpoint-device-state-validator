"""检查点校验器单元测试（所有设备状态均为模拟数据）。"""

import unittest

from checkpoint_validator import (
    CheckpointValidator,
    DMAMapping,
    DeviceState,
    DirtyPage,
    InFlightIO,
    QueueDescriptor,
    RestoreLogEntry,
    SnapshotEvent,
)

BOUNDARY = "boundary-1"
PAGE = 4096


def make_snapshot_events():
    return [
        SnapshotEvent(sequence=1, kind="pause", boundary_id=BOUNDARY),
        SnapshotEvent(sequence=2, kind="memory_start", boundary_id=BOUNDARY),
        SnapshotEvent(sequence=3, kind="device_save", boundary_id=BOUNDARY, device_id="net0"),
        SnapshotEvent(sequence=4, kind="memory_end", boundary_id=BOUNDARY),
    ]


def make_restore_log():
    return [
        RestoreLogEntry(sequence=1, device_id="net0", action="restore_start", boundary_id=BOUNDARY),
        RestoreLogEntry(sequence=2, device_id="net0", action="restore_ok", boundary_id=BOUNDARY),
    ]


def make_validator(**overrides):
    kwargs = dict(
        dirty_pages=[DirtyPage(gpa=0x1000, boundary_id=BOUNDARY)],
        devices=[
            DeviceState(
                device_id="net0",
                registers={"queue_ptr": 0x1000, "pending_interrupt": 0},
                boundary_id=BOUNDARY,
            )
        ],
        descriptors=[
            QueueDescriptor(
                descriptor_id="desc-1",
                device_id="net0",
                queue_id=0,
                index=0,
                gpa=0x1000,
                length=256,
                boundary_id=BOUNDARY,
            )
        ],
        in_flight=[
            InFlightIO(
                request_id="req-1",
                device_id="net0",
                descriptor_id="desc-1",
                boundary_id=BOUNDARY,
            )
        ],
        dma_mappings=[
            DMAMapping(device_id="net0", gpa=0x0, length=0x2000, boundary_id=BOUNDARY)
        ],
        snapshot_events=make_snapshot_events(),
        restore_log=make_restore_log(),
    )
    kwargs.update(overrides)
    return CheckpointValidator(**kwargs)


class TestHappyPath(unittest.TestCase):
    def test_consistent_checkpoint_is_restorable(self):
        report = make_validator().validate()
        self.assertTrue(report.restorable, report.errors)
        self.assertEqual(report.errors, [])
        self.assertEqual(report.unrecoverable_descriptors, [])
        kinds = [op.kind for op in report.replay_operations]
        self.assertIn("replay_io", kinds)

    def test_restore_order_memory_first_then_dependencies(self):
        devices = [
            DeviceState(device_id="blk0", registers={}, boundary_id=BOUNDARY,
                        depends_on=("ioapic",)),
            DeviceState(device_id="ioapic", registers={}, boundary_id=BOUNDARY),
            DeviceState(device_id="net0", registers={}, boundary_id=BOUNDARY,
                        depends_on=("ioapic",)),
        ]
        report = make_validator(devices=devices, in_flight=[], descriptors=[]).validate()
        order = report.restore_order
        self.assertEqual(order[0], "guest-memory")
        self.assertEqual(order[1], "dma-mappings")
        self.assertLess(order.index("ioapic"), order.index("blk0"))
        self.assertLess(order.index("ioapic"), order.index("net0"))


class TestBoundaryConsistency(unittest.TestCase):
    def test_mismatched_boundary_is_rejected(self):
        devices = [
            DeviceState(device_id="net0", registers={}, boundary_id="boundary-2")
        ]
        report = make_validator(devices=devices).validate()
        self.assertFalse(report.restorable)
        self.assertTrue(any("同一暂停边界" in e for e in report.errors))

    def test_device_saved_before_pause_is_rejected(self):
        events = [
            SnapshotEvent(sequence=1, kind="device_save", boundary_id=BOUNDARY, device_id="net0"),
            SnapshotEvent(sequence=2, kind="pause", boundary_id=BOUNDARY),
        ]
        report = make_validator(snapshot_events=events).validate()
        self.assertFalse(report.restorable)
        self.assertTrue(any("暂停边界之前" in e for e in report.errors))


class TestQueueInterrupt(unittest.TestCase):
    def test_pending_interrupt_is_replayed(self):
        devices = [
            DeviceState(
                device_id="net0",
                registers={"queue_ptr": 0x1000, "pending_interrupt": 0x3},
                boundary_id=BOUNDARY,
            )
        ]
        report = make_validator(devices=devices).validate()
        interrupts = [op for op in report.replay_operations if op.kind == "replay_interrupt"]
        self.assertEqual(len(interrupts), 1)
        self.assertEqual(interrupts[0].device_id, "net0")
        self.assertIn("0x3", interrupts[0].detail)

    def test_no_interrupt_replay_for_failed_device(self):
        devices = [
            DeviceState(
                device_id="net0",
                registers={"pending_interrupt": 0x1},
                boundary_id=BOUNDARY,
                saved=False,
            )
        ]
        report = make_validator(devices=devices).validate()
        kinds = [op.kind for op in report.replay_operations]
        self.assertNotIn("replay_interrupt", kinds)


class TestStaleDMAAddress(unittest.TestCase):
    def test_inactive_mapping_makes_descriptor_unrecoverable(self):
        mappings = [
            DMAMapping(device_id="net0", gpa=0x0, length=0x2000,
                       boundary_id=BOUNDARY, active=False)
        ]
        report = make_validator(dma_mappings=mappings).validate()
        self.assertEqual(report.unrecoverable_descriptors, ["desc-1"])
        fails = [op for op in report.replay_operations if op.kind == "fail_io"]
        self.assertEqual(len(fails), 1)
        self.assertIn("旧 DMA 地址", fails[0].detail)

    def test_buffer_page_missing_from_memory_snapshot(self):
        report = make_validator(dirty_pages=[]).validate()
        self.assertEqual(report.unrecoverable_descriptors, ["desc-1"])
        fails = [op for op in report.replay_operations if op.kind == "fail_io"]
        self.assertTrue(any("不在内存快照" in op.detail for op in fails))

    def test_missing_mapping_is_unrecoverable(self):
        report = make_validator(dma_mappings=[]).validate()
        self.assertEqual(report.unrecoverable_descriptors, ["desc-1"])


class TestPartialDevice(unittest.TestCase):
    def test_partially_saved_device_fails_and_cascades(self):
        devices = [
            DeviceState(device_id="net0", registers={}, boundary_id=BOUNDARY,
                        saved=False),
            DeviceState(device_id="blk0", registers={}, boundary_id=BOUNDARY,
                        depends_on=("net0",)),
        ]
        report = make_validator(devices=devices).validate()
        self.assertFalse(report.restorable)
        self.assertEqual(report.failed_devices, ["blk0", "net0"])
        self.assertNotIn("net0", report.restore_order)
        self.assertNotIn("blk0", report.restore_order)

    def test_in_flight_io_of_partial_device_is_failed(self):
        devices = [
            DeviceState(device_id="net0", registers={}, boundary_id=BOUNDARY,
                        saved=False)
        ]
        report = make_validator(devices=devices).validate()
        self.assertEqual(report.unrecoverable_descriptors, ["desc-1"])
        fails = [op for op in report.replay_operations if op.kind == "fail_io"]
        self.assertEqual(len(fails), 1)

    def test_descriptor_content_not_saved(self):
        descriptors = [
            QueueDescriptor(
                descriptor_id="desc-1", device_id="net0", queue_id=0, index=0,
                gpa=0x1000, length=256, boundary_id=BOUNDARY, saved=False,
            )
        ]
        report = make_validator(descriptors=descriptors).validate()
        self.assertEqual(report.unrecoverable_descriptors, ["desc-1"])
        fails = [op for op in report.replay_operations if op.kind == "fail_io"]
        self.assertTrue(any("队列指针" in op.detail for op in fails))


class TestRestoreRetry(unittest.TestCase):
    def test_fail_then_retry_then_ok_is_accepted(self):
        log = [
            RestoreLogEntry(1, "net0", "restore_start", BOUNDARY),
            RestoreLogEntry(2, "net0", "restore_fail", BOUNDARY),
            RestoreLogEntry(3, "net0", "retry", BOUNDARY),
            RestoreLogEntry(4, "net0", "restore_start", BOUNDARY),
            RestoreLogEntry(5, "net0", "restore_ok", BOUNDARY),
        ]
        report = make_validator(restore_log=log).validate()
        self.assertTrue(report.restorable, report.errors)

    def test_retry_without_failure_is_rejected(self):
        log = [
            RestoreLogEntry(1, "net0", "restore_start", BOUNDARY),
            RestoreLogEntry(2, "net0", "restore_ok", BOUNDARY),
            RestoreLogEntry(3, "net0", "retry", BOUNDARY),
        ]
        report = make_validator(restore_log=log).validate()
        self.assertFalse(report.restorable)
        self.assertTrue(any("重试" in e for e in report.errors))

    def test_restore_ok_without_start_is_rejected(self):
        log = [RestoreLogEntry(1, "net0", "restore_ok", BOUNDARY)]
        report = make_validator(restore_log=log).validate()
        self.assertFalse(report.restorable)

    def test_restore_log_boundary_mismatch_is_rejected(self):
        log = [
            RestoreLogEntry(1, "net0", "restore_start", BOUNDARY),
            RestoreLogEntry(2, "net0", "restore_ok", "boundary-other"),
        ]
        report = make_validator(restore_log=log).validate()
        self.assertFalse(report.restorable)
        self.assertTrue(any("边界" in e and "不一致" in e for e in report.errors))


if __name__ == "__main__":
    unittest.main()
