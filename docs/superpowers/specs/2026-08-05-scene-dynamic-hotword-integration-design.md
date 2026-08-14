# ASR 场景动态热词接入设计

## 1. 目标

将 `/home/twai/wjl/DynamicHotwordLoading/hotwords` 中的 13 个预置热词库
接入现有 ASR 服务。每个通话会话先挂载基础库和来电分类辅助库，再根据
来电分类、建筑用途、建筑结构信号更新热词；更新只影响下一个 VAD 语音段，
并通过现有 FunASR 握手的 `hotwords` 字段加载。

本阶段不接入基站定位、`scopeId`、地址小表和地址动态热词，不修改地址槽位
抽取、识别后纠错或模型切换逻辑。

## 2. 现有调用链

```text
客户端音频
  -> https_gateway
  -> BridgeSession
  -> VAD 划分语音段
  -> HotwordManager.current_hotwords()
  -> ASRProviderFactory.create(..., hotwords=...)
  -> FunASRProvider.start()
  -> FunASR WebSocket 握手 hotwords 字段
```

现有代码已经在每个新 VAD 语音段建立 FunASR Provider 时读取一次热词字符串，
因此不增加新的代理服务，不改变音频链路，也不需要在句中重连当前 Provider。

## 3. 模式与兼容性

`HotwordManager` 保留现有模式并新增：

- `full`：继续使用旧的全量文本热词文件。
- `dynamic`：继续使用旧的阶段文本热词库。
- `scene_dynamic`：使用本设计的 13 个 JSON 热词库和三维场景信号。

通过 `ASR_PREPROCESS_HOTWORD_MODE=scene_dynamic` 启用新模式。热词目录由
`ASR_SCENE_HOTWORD_DIR` 配置；当前服务器配置为：

```text
/home/twai/wjl/DynamicHotwordLoading/hotwords
```

未启用 `scene_dynamic` 时，所有旧行为保持不变。新模式加载失败时记录错误并
回退到旧全量热词文件，避免 ASR 因热词配置故障整体不可用。

## 4. 词库加载与校验

服务进程内按目录缓存不可变词库，通话期间不重复读取磁盘。必须存在：

- `baseline`
- `classification_assist.call_type`
- `call_type.fire_fighting`
- `call_type.social_assistance`
- `call_type.emergency_rescue`
- 五个 `building_usage.*` 库
- 三个 `building_structure.*` 库

加载规则：

- 热词文本去除首尾空白且不得为空。
- 权重必须是正整数。
- 单词长度不超过 32 个字符。
- 同一库内不允许重复文本。
- 相同文本跨库冲突时取最大权重。
- 合并后按权重降序、文本升序稳定排序。
- 单次快照默认上限 1000 条，达到 800 条记录告警。

## 5. 会话初始状态

每个 `BridgeSession` 创建独立的 `HotwordManager` 场景状态。新通话尚未收到场景
信号时，选择：

```text
baseline
classification_assist.call_type
```

首个 VAD 语音段创建 FunASR Provider 时，这两个库已经进入握手。会话之间不共享
场景选择和快照版本。

## 6. 场景信号协议

桥接层新增支持：

```json
{
  "eventType": "scene_signal.add",
  "callId": "call-001",
  "seq": 10,
  "signals": {
    "call_type": ["fire_fighting"],
    "building_usage": ["crowded_place"],
    "building_structure": ["highrise_multistory"]
  }
}
```

同时兼容 `type=scene_signal.add`。处理规则：

- `call_type` 每次必须且只能包含一个值；新值替换旧值。
- 首次确认 `call_type` 后移除 `classification_assist.call_type`。
- `building_usage` 和 `building_structure` 在同一会话中增量累加并去重。
- 完全相同的重复信号保持幂等，不增加热词版本。
- 未知维度、未知枚举或非法结构不修改快照，并返回拒绝 ACK。
- 旧的 `stage.changed` 和 `asr.hotwords.switch` 只作用于旧 `dynamic` 模式；
  `scene_dynamic` 不根据识别文本自动猜测场景。

## 7. 下一语音段生效

FunASR Provider 在一个 VAD 语音段内使用固定热词字符串：

1. 语音段开始时读取当前快照并写入握手。
2. 语音段中收到 `scene_signal.add` 时，只更新 Manager 的待用快照。
3. 当前 Provider 和当前语音段不受影响。
4. 下一个 VAD 语音段创建 Provider 时读取新快照。

这与“下一句话生效”的业务要求一致，且不破坏现有 VAD、模型切换、失败回放和
同一语音段结果聚合。

## 8. FunASR 协议

沿用当前握手，不增加字段：

```json
{
  "mode": "2pass",
  "wav_name": "call-001__caller-0002",
  "is_speaking": true,
  "chunk_interval": 10,
  "hotwords": "报警 被困 火灾扑救 高层建筑"
}
```

即使热词为空也显式发送 `hotwords`，防止服务端残留上一请求的上下文。权重只用于
本地合并、排序和截断；当前 FunASR 接口继续接收空格分隔的纯词字符串。

## 9. 可观测性

每次会话初始化和有效信号更新记录：

- `callId`
- `hotword_version`
- `library_ids`
- `hotword_count`
- `changed`
- `effective_from=next_segment`
- `warning_threshold_reached`
- `truncated`

日志只记录库标识和数量，不输出完整热词字符串。

## 10. 错误处理

| 场景 | 行为 |
|---|---|
| JSON目录不存在或缺少必需库 | 记录错误并回退旧全量库 |
| 单个JSON结构非法 | 整个场景目录加载失败并回退，避免部分加载 |
| 信号结构或枚举非法 | 拒绝 ACK，保留当前热词版本 |
| 重复信号 | ACK成功，版本不变 |
| 合并词数达到800 | 告警并继续 |
| 合并词数超过1000 | 稳定截断并记录 `truncated=true` |
| FunASR连接失败 | 沿用现有 Provider 失败和模型回退逻辑 |

## 11. 修改范围

- 扩展 `hotword_manager.py`：JSON目录加载、三维选库、快照和版本。
- 最小修改 `asr_bridge.py`：分发 `scene_signal.add` 并返回明确 ACK。
- 更新 `.env.example` 和部署配置说明。
- 扩展 `tests/test_hotword_manager.py` 和桥接测试。

不修改 `https_gateway.py` 的路由、VAD引擎、识别结果协议、模型切换、RabbitMQ、
纠错或地址机器人代码。

## 12. 验收标准

- 新会话首段握手包含基础库和分类辅助库。
- 来电分类确定后，下一个语音段移除分类辅助库并加载唯一分类详细库。
- 建筑用途、建筑结构信号可以在会话内继续增加热词库。
- 句中信号不改变当前 Provider 的热词，只影响下一段握手。
- 重复信号幂等，非法信号不污染会话状态。
- 多会话状态隔离。
- 合并、去重、排序、800告警和1000截断稳定可复现。
- 原 `full`、旧 `dynamic` 模式测试保持通过。
- FunASR Provider 握手继续显式携带空格分隔的 `hotwords`。
- ASR相关自动化测试通过，并执行 `diff/capture_asr_change.sh` 生成差异和快照。
