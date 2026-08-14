# ASR 模型切换支持任意一路 callId 设计

日期：2026-07-27

状态：已实施并发布

## 1. 问题与目标

真实通话同时建立坐席 `agent` 和客户 `caller` 两条音频流，两路拥有不同的
`callId`。当前 `POST /asr/model/switch` 只允许坐席流作为锚点；前端传入客户流
`callId` 时，即使双路都在线，也会返回 `CALL_ID_NOT_AGENT_STREAM`。

本次调整后，前端可传同一通电话的任意一路 `callId`，服务端自动定位另一条流并
向双路提交同一个模型切换请求。

## 2. 请求与响应契约

请求体字段保持不变：

```json
{
  "callId": "agent-or-caller-stream-call-id",
  "model": "xfyun",
  "seatId": "8012"
}
```

- `callId` 必须对应一条未结束、角色已确定为 `agent` 或 `caller` 的活跃音频流。
- `seatId` 必须与锚点流的 `callto` 一致。
- `model` 继续只允许 `xfyun` 或 `funasr`。
- 统一五字段响应体和“可预期业务失败返回 HTTP 200”的约定不变。
- 成功响应的 `data.callIds` 固定按 `[agentCallId, callerCallId]` 排列，不受传入
  哪一路影响。

## 3. 配对逻辑

1. 按请求 `callId` 查找活跃锚点流；不存在或已结束时返回 `CALL_NOT_FOUND`。
2. 校验锚点角色属于 `agent/caller`，否则返回 `CALL_ID_NOT_SWITCHABLE_STREAM`。
3. 校验锚点 `callto` 与 `seatId` 一致，否则返回 `SEAT_ID_MISMATCH`。
4. 使用现有 `(project, callfrom, callto)` 作为通话配对键：
   - 锚点为 `agent` 时查找唯一 `caller`；
   - 锚点为 `caller` 时查找唯一 `agent`。
5. 未找到对端继续返回 `PAIRED_STREAM_NOT_FOUND`；匹配到多条继续返回
   `PAIRED_STREAM_AMBIGUOUS`。
6. 按坐席、客户顺序将两路交给现有 `switch_active_session_models`，不改变模型连接、
   音频缓存、回退和状态广播逻辑。

## 4. 修改边界

- 修改双路会话定位函数的锚点语义和相关注释。
- 更新模型切换接口注释及双路配对单元测试。
- 不修改请求字段、成功/失败返回体字段、ASR转写、地址纠错、规则高亮和逐句模型标记。
- 不新增按 `seatId` 全局搜索，避免同一坐席存在并发通话时误配。

## 5. 验证

自动化测试至少覆盖：

1. 传坐席流 `callId` 时双路切换成功。
2. 传客户流 `callId` 时双路切换成功，且返回顺序仍为坐席、客户。
3. 任意锚点的 `seatId` 不匹配时拒绝，双路都不收到切换请求。
4. 对端流缺失或不唯一时沿用现有失败语义。
5. 未知角色流不会被误配。
6. 网关接口响应契约测试和相关 ASR 回归测试通过。
7. 只生成本次修改 diff，不创建新快照。

## 6. 实施结果

- 双路定位现已接受 `agent` 或 `caller` 任意一路 `callId`，并按相反角色查找唯一
  配对流。
- 成功提交切换时，`data.callIds` 始终按坐席流、客户流排列。
- 任意一路锚点、错误坐席、未知角色、对端缺失和对端不唯一均有自动化测试覆盖。
- ASR 正式测试套件共 242 项通过。
- 8443 网关已重启为新进程，POST 路由、统一 HTTP 200 业务响应和 GPU Paraformer
  10099 监听均正常。下一通真实电话可直接使用任意一路 `callId` 验收双路切换。
