# virtual-machine-checkpoint-device-state-validator

验证虚拟机检查点中的设备状态和 DMA 一致性。

> **注意：本仓库中所有设备状态、寄存器、DMA 映射和 I/O 请求均为模拟数据**，
> 仅用于演示检查点校验逻辑，不与任何真实虚拟机监控器交互。

## 背景

虚拟机内存快照成功，但某个半虚拟化设备只保存了队列指针，没有保存未完成的
DMA 描述符。恢复后设备会继续处理指向旧内存的请求，造成数据损坏。本工具在
恢复前对检查点做一致性校验，判断快照能否安全恢复。

## 校验规则

1. **同一暂停边界**：内存脏页、设备寄存器、队列描述符、未完成 I/O、DMA
   映射、快照事件必须携带相同的 `boundary_id`。
2. **快照顺序**：必须先 `pause`，设备保存与内存快照必须发生在暂停之后。
3. **未完成 DMA**：每个未完成 I/O 的描述符必须已保存、DMA 映射有效、且
   缓冲区页面包含在内存快照中；否则描述符不可恢复，必须明确失败
   （`fail_io`），可恢复的则重建（`replay_io`）。
4. **恢复顺序**：`guest-memory` -> `dma-mappings` -> 设备（按依赖拓扑排序）。
5. **恢复日志**：动作必须成对（`restore_start` 后才有 `restore_ok` /
   `restore_fail`），`retry` 只允许出现在失败之后，且日志边界必须与
   检查点一致。

## 输出

`CheckpointValidator.validate()` 返回 `ValidationReport`：

- `restorable`：快照能否安全恢复。
- `errors`：一致性错误列表。
- `restore_order`：设备恢复顺序。
- `failed_devices`：状态未完整保存（部分保存）而失败的设备。
- `unrecoverable_descriptors`：不可恢复的描述符。
- `replay_operations`：需要重放的操作（`replay_io` / `fail_io` /
  `replay_interrupt`）。

## 用法

```python
from checkpoint_validator import CheckpointValidator, ...

report = CheckpointValidator(
    dirty_pages=..., devices=..., descriptors=..., in_flight=...,
    dma_mappings=..., snapshot_events=..., restore_log=...,
).validate()
```

## 测试

需要 Python 3.8.2+。测试覆盖队列中断重放、旧 DMA 地址、部分保存的设备和
恢复重试：

```sh
python -m unittest
```
