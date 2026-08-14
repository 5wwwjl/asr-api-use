# ASR 基站逻辑子库 RabbitMQ 订阅设计

## 1. 目标与范围

在现有 `asr_api_use` 网关进程内订阅定位服务的
`address.scope.ready.v1` 事件。事件携带的 `data.sessionId` 与 ASR
`callId` 约定完全一致；消费者由此把本次定位生成的 `scopeId` 关联到
对应 ASR 会话。

本阶段完成可靠、可观测的 RabbitMQ 订阅、会话关联、REST 地址子库分页读取
和地址热词排队。ASR 不直连 PostGIS；地址词只进入会话的待用热词快照，并从
下一个 VAD 语音段的 FunASR 握手生效。

## 2. 已确认约束

- ASR 不直连 PostGIS；后续地址数据读取使用定位服务 REST 接口。
- `data.sessionId == ASR callId`。
- RabbitMQ 使用 AMQP 0-9-1，vhost 为 `/location`。
- ASR 仅消费定位服务分配的热词队列
  `location.address-scope.hotword.v1`，不声明、修改或删除 exchange、
  queue、binding。
- MQ 凭据仅以环境变量或密钥引用提供，不能写入代码、文档、测试输出或
  日志。

## 3. 方案选择

采用 `pika` 阻塞消费者线程加主事件循环回调的方式：项目已使用 `pika`，
无需引入新的异步客户端依赖。消费者线程负责连接、收包、ACK/NACK 和重连；
ASR 主事件循环负责访问 `_active_sessions` 及待绑定状态，避免跨线程修改
`BridgeSession`。

不采用独立消费者服务，避免增加服务间回调和部署单元；不采用按会话临时订阅，
避免基站事件早于 ASR WebSocket 到达时丢失。

## 4. 数据流

```text
定位服务
  -> RabbitMQ: location.address-scope.hotword.v1
  -> AddressScopeRabbitMQConsumer（后台线程）
  -> CloudEvent 契约校验与幂等判断
  -> 主 asyncio 事件循环
      -> 活动 callId 存在：REST分页、地址词提取、更新待用热词快照
      -> 活动 callId 不存在：按 callId 暂存，call.started 后查询并更新
  -> 手动 ACK

后续阶段：已登记 scopeId -> REST 分页 -> 地址词提取 -> 下一 VAD 段热词快照
```

## 5. 消费与确认语义

消费者连接参数全部使用以 `ASR_ADDRESS_SCOPE_MQ_` 开头的环境变量，例如：

```text
ASR_ADDRESS_SCOPE_MQ_ENABLED=true
ASR_ADDRESS_SCOPE_MQ_HOST=192.168.173.167
ASR_ADDRESS_SCOPE_MQ_PORT=5672
ASR_ADDRESS_SCOPE_MQ_VHOST=/location
ASR_ADDRESS_SCOPE_MQ_USER=${secret-reference}
ASR_ADDRESS_SCOPE_MQ_PASSWORD=${secret-reference}
ASR_ADDRESS_SCOPE_MQ_QUEUE=location.address-scope.hotword.v1
ASR_ADDRESS_SCOPE_PENDING_TTL_SECONDS=1800
ASR_ADDRESS_SCOPE_PENDING_MAX_ENTRIES=10000
```

- 使用 `basic_qos(prefetch_count=1)` 和 `auto_ack=false`。
- 使用 `queue_declare(passive=true)` 验证定位侧已创建的队列；不创建任何
  RabbitMQ 资源。
- 活动会话中，完成事件校验、REST查询和地址热词排队后 ACK；REST暂时故障则
  NACK重回队列。会话尚未建立时先暂存句柄并 ACK，随后由会话侧查询；查询失败
  不阻塞ASR且保留原热词快照。
- JSON、CloudEvent 或字段校验失败时 NACK 且不重回队列，防止毒消息阻塞；
  定位侧 DLQ 负责留存。
- 临时连接或主事件循环故障时 NACK 并重回队列；连接按有上限的指数退避重连。
- 网关停止时先停止接收，再等待当前消息处理结束，最后关闭连接和线程。

## 6. 事件校验与关联

仅接收：

```text
type = address.scope.ready.v1
specversion = 1.0
data.eventType = address.scope.ready.v1
```

还必须满足：

- `id == data.eventId`；
- `subject == data.addressScopeRef.scopeId`；
- `sessionId` 和 `scopeId` 非空；
- `scopeId`、`locationResolutionId` 是合法 UUID；
- `locationResolutionVersion >= 1`；
- `inventoryVersion` 非空；
- `itemsPath` 与 `scopeId` 一致。

`eventId` 是幂等键。重复事件不重复登记业务状态，但作为成功消费 ACK。

已存在的 `callId` 直接使用 REST `POST /api/v1/address-scopes/{scopeId}/items`
分页读取 `BUILDING/AOI/POI`；每页版本必须与事件一致，按 `inventoryId`
去重。提取 `standardName`、`aoiName`、`shortName` 和可选 `aliases[]`，不使用
完整地址、坐标或 LOI。标准名与AOI权重20，简称/别名权重15；合并后受800告警、
1000稳定截断约束。

未创建的 `callId` 写入有界、带 TTL 的待绑定表；相同 `callId` 的较新事件替换
旧事件。会话注册时消费待绑定记录并后台查询。待绑定记录到期后清理并记录告警，
不会把旧 `scopeId` 复用到其他通话。

## 7. 可观测性

日志只包含：`eventId`、`callId`、`scopeId`、库存版本、定位版本、状态和
耗时；不得输出 MQ 密码、完整地址数组、音频或对话文本。

指标至少包括：

- 消费连接状态、重连次数和最后连接错误；
- `received/acked/requeued/rejected/duplicate` 计数；
- `bound_active/pending_session/pending_expired` 计数；
- REST查询记录数、地址候选词数、查询耗时、截断和告警状态；
- 当前待绑定条数和最老待绑定年龄。

## 8. 测试与验收

自动化测试覆盖：

1. 合法事件被校验并关联活动会话；
2. 事件先于 `call.started` 到达时被暂存，并在会话注册后绑定；
3. 重复 `eventId` 幂等；
4. 非法契约事件拒绝且不污染状态；
5. 新 scope 替换同一 `callId` 的旧 scope；
6. TTL 和最大容量清理；
7. 消费线程重连、关闭和 ACK/NACK 行为。

联调验收：消费者在定位服务发布一条真实 `address.scope.ready.v1` 后，日志
可观察到同一 `eventId/sessionId/scopeId`、REST查询数量和地址词数量，队列消息
在成功排队后 ACK；新VAD段的 FunASR握手热词数随地址词更新。真实识别提升另行
通过A/B音频集验收。

## 9. 后续边界

后续仅补充两项：定位服务在REST响应中稳定返回 `aliases[]`，以及在维护窗口
启用MQ凭据、重启网关并完成真实FunASR的音频A/B验收。
