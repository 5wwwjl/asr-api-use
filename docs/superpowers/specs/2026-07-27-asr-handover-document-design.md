# ASR 全链路技术交接文档设计

日期：2026-07-27

状态：已完成

## 1. 目标

为可以直接登录当前服务器的后端或算法同事提供一份可独立使用的 ASR 技术交接文档，
覆盖 CTI 音频接入、实时识别、VAD 分段、模型切换、地址纠错、规则高亮、录音、结果
分发、落库、部署运维和服务商替换。

交接文档既描述当前 `192.168.173.167` 环境，也定义更换 ASR 服务商时必须保持稳定的
外部协议和内部适配协议。接手人不需要依赖聊天记录即可理解、联调和维护完整链路。

## 2. 交付物

新建一份 Markdown 主文档：

```text
asr_api_use/ASR全链路技术交接与服务商对接协议.md
```

采用“单一主文档 + 文内附录”结构，不再拆成多份相互依赖的文档。现有文档保留为历史
资料，主文档明确指出权威口径和已过期内容。

文档包含实际路径、内网地址、端口、容器名、脚本名和可执行命令；不写任何真实密码、
API Key、Secret、Token、证书私钥或数据库口令，只列出环境变量名和密钥交接要求。

## 3. 权威口径

文档内容以当前生产代码和配置为准，重点引用以下实现：

- `https_gateway.py`：WSS/HTTPS 入口、Bridge 首帧识别、路由、模型切换和 CTI 控制接口；
- `asr_bridge.py`：会话、ACK、VAD、转写、双路配对、后处理和事件扇出；
- `asr_providers.py`：FunASR、科大讯飞和供应商工厂；
- `asr_ai_postprocessor.py`：地址库拼音对齐纠错和规则高亮；
- `asr_message_service.py`、`asr_rabbitmq.py`、`asr_database.py`：结果输出；
- `.env` 中不涉及秘密的运行开关，以及启动、守护和容器脚本。

必须明确：当前 `/asr` 强制使用包含 `eventType` 的 Bridge JSON 协议。`README.md` 中
“先发 `is_speaking`、再发二进制 PCM”的直连 FunASR 方式属于旧版麦克风协议，不是
CTI 生产接入协议。

## 4. 文档结构

### 4.1 快速接手

- 模块职责和当前生产链路；
- 代码位置、进程、容器、端口、外部依赖；
- 接手后首先执行的只读健康检查；
- 密钥、证书和数据库权限的线下交接清单。

### 4.2 全链路架构

使用 Mermaid 流程图和时序图说明：

```text
CTI双路音频 → 8443网关 → BridgeSession → VAD/预处理
→ ASR供应商 → 稳定turn → 地址库纠错 → 规则高亮
→ 客户端/monitor/消息服务/RabbitMQ/PostgreSQL/日志
```

说明同一通电话通常有 `agent`、`caller` 两条 WebSocket 和两个 `callId`，两路通过
`(project, callfrom, callto)` 配对，业务展示可按电话对聚合。

### 4.3 CTI 输入协议

- `WSS /asr` 地址、TLS 和连接生命周期；
- `call.started → audio.frame × N → call.ended` 顺序；
- 公共事件外层字段和每种事件的完整 JSON 示例；
- `audio.frame.payload` 中的 `speaker`、`direction`、`callfrom`、`callto`、时间戳、
  `pcm_s16le/16000Hz/mono/Base64` 要求；
- 推荐帧长、`seq` 递增、ACK 处理、限流、断线和重连规则；
- `stage.changed`、`asr.hotwords.switch` 和心跳兼容行为；
- 双路通话、转接、保持和挂断边界。

### 4.4 控制面协议

- `POST /cti/events` 的保持与取消保持事件；
- `POST /asr/model/switch` 的请求、统一五字段返回体和业务错误码；
- 任意一路 `agent/caller callId` 作为模型切换锚点；
- `asr.model.state/pending/changed/switch.failed` 状态事件；
- HTTP 200 只代表业务请求已处理，最终切换结果以状态事件为准。

### 4.5 ASR 服务商适配协议

将当前实现整理为稳定的内部 Provider 契约：

- 工厂能力：`availability`、`available_providers`、`create`；
- 实例生命周期：`start`、`send_audio`、`finish`、`events`、`close`；
- 标准输入：一个 VAD 段内的 16kHz、16bit、单声道 PCM；
- 标准输出：`ProviderResult(provider, segment_id, text, is_final, mode,
  error_code, error_message, sid)`；
- 热词由工厂 `create(..., hotwords=...)` 传入，供应商自行转换，不允许影响 CTI 协议；
- 供应商异常必须转换为稳定错误码，不向日志和前端泄漏凭据；
- 供应商专有 token、标点和流式增量必须在适配层归一化；
- 新服务商注册点、网关白名单、模型切换、页面标签、配置和测试修改清单。

外部 CTI 和结果消费协议不得随服务商更换而改变。

### 4.6 后处理和输出协议

- 当前固定链路：ASR 原文 → 地址库拼音对齐纠错并生成 `correctedText` → 规则只提取
  `keywords` 且不再改写 `correctedText` → 推送 `call.corrected`；
- `turns`、`keywords`、`correctionProvider`、`correctionMode`、`highlightProvider`、
  `replacements` 和耗时字段的语义；
- progressive `speech.final` 与带 `finalSource` 的稳定 turn 文本区别；
- `audio.segment`、`segmentId/segmentIds` 和录音绑定；
- 同一上游事件在 CTI WebSocket、`/monitor`、消息服务 CloudEvent、RabbitMQ、数据库和
  业务日志中的包装差异；
- 消费端幂等键、乱序合并和双 callId 聚合规则。

### 4.7 部署、测试与运维

- 当前 GPU 生产模型 10099、CPU 测试模型 10097、网关 8443；
- Docker 容器、`start_all_services.sh`、`watchdog.sh`、重启和升级步骤；
- `.env` 配置按网关、供应商、数据库、消息服务、RabbitMQ、录音和后处理分类；
- 健康检查、日志位置、常用诊断命令和常见错误表；
- 自动化测试、`bridge_test_client.py`、真实双路电话和服务商替换验收清单；
- 当前未配置日志自动清理等已知风险，只描述现状和建议，不在本任务中修改运行逻辑。

## 5. 示例和字段要求

所有协议示例同时给出通用占位地址和当前环境地址。JSON 示例必须是有效 JSON，不使用
注释或省略必填层级。每个对外事件至少说明：

- 发送方、接收方、传输方式；
- 必填字段、可选字段和枚举；
- 时序、幂等键和聚合键；
- 成功、失败与重试语义；
- 更换服务商时是否允许改变。

文档明确以下稳定约束：

1. CTI 的 Bridge 事件字段不因模型厂商变化而变化。
2. `speech.final`、`call.corrected` 和 `audio.segment` 的业务字段不因厂商变化而变化。
3. `provider/providers` 可以反映实际识别来源。
4. `correctedText` 只由地址后处理链路决定，供应商适配层只产出原始识别文本。
5. `callId + segmentId` 用于单段幂等，`segmentIds` 用于 turn 级文本、纠错和录音绑定。
6. 实际密钥永不进入 Git、diff、日志示例或交接文档。

## 6. 自检与验收

完成主文档后执行以下检查：

1. 对照代码核验所有路由、字段、枚举和当前端口。
2. 搜索并消除真实 Secret、Token、API Key 和数据库密码。
3. 验证所有 JSON 示例可解析、Mermaid 代码块闭合、内部文件链接存在。
4. 检查文档不存在未完成占位项或未解释的历史口径。
5. 确认主文档明确指出旧版直连协议不适用于 CTI。
6. 确认新服务商接入清单覆盖适配、配置、注册、切换、回退、测试和上线。
7. 按用户约定生成 diff-only，不创建新快照。
