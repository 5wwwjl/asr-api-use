# 地址范围消息审计与链路展示设计

## 1. 目标

对每个 `address.scope.ready.v1` 事件保留可追溯的三段结果，以同一
`eventId` 关联：

1. RabbitMQ 消息原文；
2. 地址服务返回的完整小地址库；
3. 从小地址库筛选得到的地址热词及权重。

用于验证“新 MQ 消息 -> scopeId -> 小地址库 -> 地址热词”的实际链路。

## 2. 存储与保留

- 根目录由 `ASR_ADDRESS_SCOPE_AUDIT_DIR` 配置，默认
  `logs/address_scope_audit`。
- 每个事件写入一个 JSON 文件，文件名使用 UTC 时间戳与 `eventId` 的安全
  化值；同一事件覆盖更新，不产生多份记录。
- 保存完整事件 JSON 与 REST 返回的完整 `items` 数组，保留筛选后热词、
  字段来源、权重、数量、耗时及处理状态。
- 每次写入时清理修改时间超过 7 天的审计文件；保留天数由
  `ASR_ADDRESS_SCOPE_AUDIT_RETENTION_DAYS` 配置。
- 审计目录权限限制为服务运行用户可读写；不写入 MQ 密码、HTTP 凭据、音频、
  ASR 转写文本或其他环境变量。

## 3. 处理流程

```text
MQ 原始 JSON
  -> CloudEvent 校验
  -> 写入 received 审计记录（消息原文）
  -> REST 分页读取 scopeId 小地址库
  -> 写入 resolved 审计记录（完整小地址库）
  -> 提取 standardName/aoiName/shortName/aliases
  -> 写入 filtered 审计记录（词、权重、来源字段）
  -> 更新该 callId 的下一段热词快照
```

校验、REST 或热词更新失败时仍更新同一审计记录为 `failed`，包括安全的错误类型
和阶段；原消息不丢失。失败时不得把异常详情中的认证信息写入文件。

## 4. 查询与展示

新增只读接口：

- `GET /api/address-scope-audit/latest`：返回最新一条完整审计记录；
- `GET /api/address-scope-audit/{eventId}`：返回指定事件审计记录。

不存在时返回 `404`；文件损坏或 JSON 无效时返回 `500`，不影响 ASR/MQ 消费。
接口仅用于受控内网验收，不增加新的鉴权模型。

## 5. 验收

定位服务发布一条合法新消息后：

1. `latest` 返回的 `rawMessage` 与发布消息逐字段一致；
2. `addressScope.items` 为对应 `scopeId` 的 REST 小地址库；
3. `filteredHotwords` 仅来自允许字段，且包含词、字段来源、权重；
4. 地址热词在相同 `callId` 下一 VAD 段加载；
5. 重复 `eventId` 不新增业务状态，审计记录仍可查询；
6. 清理逻辑不影响 7 天内文件。
