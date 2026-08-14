# ASR三模块API接口文档

![ASR Framework 流程图](./ASR-framework-flowchart.svg)

<style>
table th:first-of-type {
    width: 100px;
}
</style>

## 文档总览

### 模块划分总览

| 模块 | 模块定位 | 对外/集成接口 | 输入 | 输出 | 主要验证目标 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| 实时ASR接入与分发模块 | 音频进入、ASR识别、结果实时分发 | `WSS /asr`、`WSS /monitor`、RabbitMQ、PostgreSQL | 通话事件、Base64 PCM音频帧、监控订阅连接 | ACK、`speech.final`、RabbitMQ消息、数据库记录 | 验证语音流接入、ACK响应、识别结果返回和结果分发能力 |
| VAD分段与录音绑定模块 | 判断语音边界、合并业务轮次、绑定录音与文本 | `/monitor`事件、`GET /recordings/...`、`GET /audio/{record_id}` | `/asr`音频帧、VAD分段PCM、speaker/direction元数据 | `speech.vad`、`audio.segment`、WAV录音地址 | 验证VAD切分、录音保存、文本录音绑定准确性 |
| 地址库后处理模块 | 对稳定识别文本做地址库纠错 | `/monitor`事件、RabbitMQ | 稳定`speech.final`文本、callId、segmentId、speaker | `call.corrected`、correctedText、replacements | 验证地址库纠错结果、替换明细和失败隔离能力 |

## 一、实时ASR接入与分发模块

### 1.1 语音流接入
---
#### 接口说明

| URL | request | version | status |
| :--- | :--- | :--- | :--- |
| [wss://server_ip:8443/asr?callId={callId}](wss://server_ip:8443/asr?callId=call-001) | WebSocket | 1.0 | true |

#### 请求参数说明

| 请求参数 | 类型 | 必填 | 参数说明 | 示例 |
| :--- | :--- | :--- | :--- | :--- |
| callId | String | true | 通话唯一标识，可放在 query 或消息体中 | call-001 |
| eventType | String | true | 上行事件类型：call.started、audio.frame、heartbeat、call.ended | audio.frame |
| callfrom | String | false | 主叫号码 | 8015 |
| callto | String | false | 被叫号码 | 8014 |
| streamId | String | false | 音频流标识 | stream-main |
| seq | Integer | true | 同一通话内递增序号 | 2 |
| timestampMs | Integer | true | 相对通话开始的毫秒时间 | 320 |
| payload.speaker | String | audio.frame必填 | 说话人：caller、agent、system、unknown | caller |
| payload.direction | String | audio.frame必填 | 方向：inbound、outbound、mixed、unknown | inbound |
| payload.startTimeMs | Integer | audio.frame必填 | 当前音频帧开始时间，毫秒 | 0 |
| payload.endTimeMs | Integer | audio.frame必填 | 当前音频帧结束时间，毫秒 | 320 |
| payload.codec | String | audio.frame必填 | 音频编码，目前推荐 pcm_s16le | pcm_s16le |
| payload.sampleRate | Integer | audio.frame必填 | 采样率，推荐 16000 | 16000 |
| payload.channels | Integer | audio.frame必填 | 声道数，推荐 1 | 1 |
| payload.audioBase64 | String | audio.frame必填 | PCM 音频 Base64 | ... |

#### 请求示例JSON

```json
{
  "eventType": "audio.frame",
  "callId": "call-001",
  "callfrom": "8015",
  "callto": "8014",
  "streamId": "stream-main",
  "seq": 2,
  "timestampMs": 320,
  "payload": {
    "speaker": "caller",
    "direction": "inbound",
    "startTimeMs": 0,
    "endTimeMs": 320,
    "codec": "pcm_s16le",
    "sampleRate": 16000,
    "channels": 1,
    "audioBase64": "<base64 pcm>"
  }
}
```

#### 返回参数说明

| 返回参数 | 参数类型 | 参数说明 |
| :--- | :--- | :--- |
| type | String | ACK 类型，固定为 ack |
| callId | String | 通话唯一标识 |
| callfrom | String | 主叫号码 |
| callto | String | 被叫号码 |
| receivedSeq | Integer | 已接收的上行 seq |
| accepted | Boolean | 是否接收成功 |
| message | String | ok 或错误原因 |
| eventType | String | 识别结果事件类型：speech.final |
| payload.segmentId | String | ASR 分段标识 |
| payload.speaker | String | 说话人 |
| payload.direction | String | 方向 |
| payload.text | String | 识别文本 |
| payload.confidence | Number | 置信度 |
| payload.language | String | 语言 |

#### 返回示例JSON

```json
{
  "type": "ack",
  "callId": "call-001",
  "callfrom": "8015",
  "callto": "8014",
  "receivedSeq": 2,
  "accepted": true,
  "message": "ok"
}
```

```json
{
  "eventType": "speech.final",
  "callId": "call-001",
  "callfrom": "8015",
  "callto": "8014",
  "streamId": "stream-main",
  "seq": 1,
  "payload": {
    "segmentId": "caller-0001",
    "speaker": "caller",
    "direction": "inbound",
    "text": "深圳软件园一期七栋有人被困",
    "confidence": 0.9,
    "language": "zh-CN"
  }
}
```

#### code码说明

| code | msg | desc |
| :--- | :--- | :--- |
| 200 | ok | 接收成功 |
| 400 | INVALID_JSON | JSON 解析失败 |
| 400 | MISSING_REQUIRED_FIELD | 缺少必填字段 |
| 503 | ASR upstream unavailable | ASR 上游不可用 |

#### 接口详细说明

```text
该接口用于第三方系统实时推送通话音频流。
服务端收到每条上行事件后返回 ack；audio.frame 会进入音频预处理、ASR 识别桥接和结果分发链路。
验证关注：连接建立、audio.frame 接收、ACK 返回、识别文本返回和异常帧错误提示。
```

---

#### 备注
```text
音频推荐格式：pcm_s16le、16000Hz、mono。生产环境使用 WSS。
```

### 1.2 实时监控订阅
---
#### 接口说明

| URL | request | version | status |
| :--- | :--- | :--- | :--- |
| [wss://server_ip:8443/monitor](wss://server_ip:8443/monitor) | WebSocket | 1.0 | true |

#### 请求参数说明

| 请求参数 | 类型 | 必填 | 参数说明 | 示例 |
| :--- | :--- | :--- | :--- | :--- |
| 无 | - | false | 连接建立后服务端主动推送事件 | - |
| command | String | false | 监控控制命令，目前支持 force_end_all | force_end_all |

#### 请求示例JSON

```json
{
  "command": "force_end_all"
}
```

#### 返回参数说明

| 返回参数 | 参数类型 | 参数说明 |
| :--- | :--- | :--- |
| event | String | 事件类型：call.started、speech.vad、speech.final、audio.segment、call.corrected、call.ended、rabbitmq.message |
| callId | String | 通话唯一标识 |
| callfrom | String | 主叫号码 |
| callto | String | 被叫号码 |
| segmentId | String | 分段标识 |
| speaker | String | 说话人 |
| direction | String | 方向 |
| text | String | 识别文本 |
| startTimeMs | Integer | 开始时间，毫秒 |
| endTimeMs | Integer | 结束时间，毫秒 |
| durationMs | Integer | 持续时间，毫秒 |
| sendTimeMs | Integer | 服务端发送时间，Unix 毫秒 |

#### 返回示例JSON

```json
{
  "event": "speech.final",
  "callId": "call-001",
  "segmentId": "caller-0001",
  "callfrom": "8015",
  "callto": "8014",
  "speaker": "caller",
  "direction": "inbound",
  "text": "深圳软件园一期七栋有人被困",
  "startTimeMs": 1200,
  "endTimeMs": 3500,
  "durationMs": 2300,
  "sendTimeMs": 1781510400000
}
```

#### code码说明

| code | msg | desc |
| :--- | :--- | :--- |
| 200 | connected | WebSocket 连接成功 |
| 400 | invalid command | 非法控制命令会被忽略 |

#### 接口详细说明

```text
该接口用于业务系统或监控页面实时消费 ASR 服务广播事件。
实时 ASR 接入与分发模块主要输出 speech.final，同时也会转发 RabbitMQ 投递预览事件 rabbitmq.message。
验证关注：monitor 连接建立、事件字段完整性、callId/segmentId 关联和控制命令响应。
```

---

#### 备注
```text
客户端应按 callId + segmentId 合并 speech.final、audio.segment、call.corrected 等事件。
```

### 1.3 RabbitMQ事件分发
---
#### 接口说明

| URL | request | version | status |
| :--- | :--- | :--- | :--- |
| exchange:ids:asr / exchange:ids:qs | Publish | 1.0 | true |

#### 请求参数说明

| 请求参数 | 类型 | 必填 | 参数说明 | 示例 |
| :--- | :--- | :--- | :--- | :--- |
| event | String | true | 可分发事件：speech.final、audio.segment、call.corrected | speech.final |
| callId | String | true | 通话唯一标识 | call-001 |
| callto | String | false | 用于生成 routingKey | 8014 |
| data | Object | true | 原始事件内容 | {...} |

#### 请求示例JSON

```json
{
  "event": "speech.final",
  "callId": "call-001",
  "callto": "8014",
  "data": {
    "event": "speech.final",
    "callId": "call-001",
    "segmentId": "caller-0001",
    "speaker": "caller",
    "direction": "inbound",
    "text": "深圳软件园一期七栋有人被困"
  }
}
```

#### 返回参数说明

| 返回参数 | 参数类型 | 参数说明 |
| :--- | :--- | :--- |
| id | String | CloudEvent ID |
| source | String | 事件源，默认 ids:asr |
| type | String | CloudEvent 类型 |
| specversion | String | CloudEvent 版本 |
| time | String | 事件时间 |
| data | Object | 原始 ASR 事件 |

#### 返回示例JSON

```json
{
  "id": "f5c4b7d8c2a14f2a8f9f2a5d2b0e1111",
  "source": "ids:asr",
  "type": "ids:asr:speech.final",
  "specversion": "1.0",
  "time": "2026-07-01T11:30:00",
  "data": {
    "event": "speech.final",
    "callId": "call-001",
    "segmentId": "caller-0001",
    "callto": "8014",
    "text": "深圳软件园一期七栋有人被困"
  }
}
```

#### code码说明

| code | msg | desc |
| :--- | :--- | :--- |
| 200 | published | 已提交发布任务 |
| 500 | publish failed | RabbitMQ 投递失败，记录日志 |

#### 接口详细说明

```text
RabbitMQ 为内部集成分发通道，不由外部主动调用。
routingKey 默认按 callto 分区，例如 asr.8014。
验证关注：CloudEvent 字段完整性、routingKey 生成、事件类型映射和投递失败日志记录。
```

---

#### 备注
```text
该模块还会按配置写入 PostgreSQL，用于业务侧历史记录查询。
```

## 二、VAD分段与录音绑定模块

### 2.1 VAD状态事件
---
#### 接口说明

| URL | request | version | status |
| :--- | :--- | :--- | :--- |
| [wss://server_ip:8443/monitor](wss://server_ip:8443/monitor) | Server Push | 1.0 | true |

#### 请求参数说明

| 请求参数 | 类型 | 必填 | 参数说明 | 示例 |
| :--- | :--- | :--- | :--- | :--- |
| audio.frame | Object | true | VAD 输入来自 /asr 的音频帧 | {...} |

#### 请求示例JSON

```json
{
  "eventType": "audio.frame",
  "callId": "call-001",
  "seq": 10,
  "timestampMs": 1600,
  "payload": {
    "speaker": "caller",
    "direction": "inbound",
    "startTimeMs": 1200,
    "endTimeMs": 1600,
    "codec": "pcm_s16le",
    "sampleRate": 16000,
    "channels": 1,
    "audioBase64": "<base64 pcm>"
  }
}
```

#### 返回参数说明

| 返回参数 | 参数类型 | 参数说明 |
| :--- | :--- | :--- |
| event | String | 固定为 speech.vad |
| callId | String | 通话唯一标识 |
| callfrom | String | 主叫号码 |
| callto | String | 被叫号码 |
| speaker | String | 说话人 |
| direction | String | 方向 |
| vadState | String | speaking、silence、ended |
| silenceDurationMs | Integer | 连续静音时长，毫秒 |
| volumeDb | Number | 当前音量，dBFS |
| audioLevel | Integer | 0-100 音量等级 |
| startTimeMs | Integer | 当前语音段开始时间 |
| endTimeMs | Integer | 当前语音段结束时间 |
| sendTimeMs | Integer | 服务端发送时间 |

#### 返回示例JSON

```json
{
  "event": "speech.vad",
  "callId": "call-001",
  "callfrom": "8015",
  "callto": "8014",
  "speaker": "caller",
  "direction": "inbound",
  "vadState": "speaking",
  "silenceDurationMs": 0,
  "volumeDb": -25.4,
  "audioLevel": 58,
  "startTimeMs": 1200,
  "endTimeMs": 1800,
  "sendTimeMs": 1781510400000
}
```

#### code码说明

| code | msg | desc |
| :--- | :--- | :--- |
| 200 | speaking | 检测到说话中 |
| 200 | silence | 当前处于静音 |
| 200 | ended | 当前语音段结束 |

#### 接口详细说明

```text
VAD 模块没有独立外部入口，输入来自 /asr 的 audio.frame，输出通过 /monitor 广播。
验证关注：speaking/silence/ended 状态转换、说话结束识别、背景噪声下的状态稳定性。
```

---

#### 备注
```text
vadState=ended 后通常会触发 ASR 分段结束、录音缓存和后续 audio.segment 事件。
```

### 2.2 录音绑定事件
---
#### 接口说明

| URL | request | version | status |
| :--- | :--- | :--- | :--- |
| [wss://server_ip:8443/monitor](wss://server_ip:8443/monitor) | Server Push | 1.0 | true |

#### 请求参数说明

| 请求参数 | 类型 | 必填 | 参数说明 | 示例 |
| :--- | :--- | :--- | :--- | :--- |
| callId | String | true | 通话唯一标识，来自 /asr | call-001 |
| segmentId | String | true | VAD/ASR 分段标识 | caller-0001 |
| pcm | Binary | true | VAD 切分后的 PCM 音频 | - |

#### 请求示例JSON

```json
{
  "callId": "call-001",
  "segmentId": "caller-0001",
  "segmentIds": ["caller-0001", "caller-0002"],
  "speaker": "caller",
  "direction": "inbound",
  "startTimeMs": 1200,
  "endTimeMs": 3500,
  "pcm": "<binary pcm>"
}
```

#### 返回参数说明

| 返回参数 | 参数类型 | 参数说明 |
| :--- | :--- | :--- |
| event | String | 固定为 audio.segment |
| callId | String | 通话唯一标识 |
| segmentId | String | 主分段标识 |
| segmentIds | Array | 合并后的 VAD 分段列表 |
| recordId | String | 远程文件服务记录 ID |
| callfrom | String | 主叫号码 |
| callto | String | 被叫号码 |
| speaker | String | 说话人 |
| direction | String | 方向 |
| audioUrl | String | 远程文件服务播放地址 |
| localAudioUrl | String | 本地 HTTPS 播放地址 |
| audioDurationMs | Integer | 录音时长 |
| startTimeMs | Integer | 录音开始时间 |
| endTimeMs | Integer | 录音结束时间 |
| sendTimeMs | Integer | 服务端发送时间 |

#### 返回示例JSON

```json
{
  "event": "audio.segment",
  "callId": "call-001",
  "segmentId": "caller-0001",
  "segmentIds": ["caller-0001", "caller-0002"],
  "recordId": "rec-123",
  "callfrom": "8015",
  "callto": "8014",
  "speaker": "caller",
  "direction": "inbound",
  "audioUrl": "http://file-service/preview/rec-123",
  "localAudioUrl": "/recordings/2026-06-15/call-001/caller-0001.wav",
  "audioDurationMs": 2300,
  "startTimeMs": 1200,
  "endTimeMs": 3500,
  "sendTimeMs": 1781510401000
}
```

#### code码说明

| code | msg | desc |
| :--- | :--- | :--- |
| 200 | saved | 录音保存成功 |
| 200 | uploaded | 录音上传成功 |
| 500 | upload failed | 远程上传失败，查看 localAudioUrl |
| 500 | save failed | 本地保存失败 |

#### 接口详细说明

```text
录音绑定模块负责把 VAD 小段合并为业务 turn，并通过 callId + segmentId / segmentIds 关联文本与录音。
同一 turn 的 speech.final 与 audio.segment 到达顺序不固定，消费方需要按 callId + segmentId 缓存合并。
验证关注：录音保存结果、远程上传结果、audioUrl/localAudioUrl 字段和文本录音绑定关系。
```

---

#### 备注
```text
audioUrl 面向业务系统和 RabbitMQ/DB；localAudioUrl 面向本地监控播放。
```

### 2.3 录音播放
---
#### 接口说明

| URL | request | version | status |
| :--- | :--- | :--- | :--- |
| [https://server_ip:8443/recordings/{date}/{callId}/{filename}.wav](https://server_ip:8443/recordings/2026-06-15/call-001/caller-0001.wav) | GET | 1.0 | true |
| [https://server_ip:8443/audio/{record_id}](https://server_ip:8443/audio/rec-123) | GET | 1.0 | true |

#### 请求参数说明

| 请求参数 | 类型 | 必填 | 参数说明 | 示例 |
| :--- | :--- | :--- | :--- | :--- |
| date | String | 本地录音必填 | 录音日期 | 2026-06-15 |
| callId | String | 本地录音必填 | 通话唯一标识 | call-001 |
| filename | String | 本地录音必填 | WAV 文件名 | caller-0001.wav |
| record_id | String | 远程代理必填 | 远程文件服务记录 ID | rec-123 |

#### 请求示例

```http
GET /recordings/2026-06-15/call-001/caller-0001.wav HTTP/1.1
Host: server_ip:8443
```

```http
GET /audio/rec-123 HTTP/1.1
Host: server_ip:8443
```

#### 返回参数说明

| 返回参数 | 参数类型 | 参数说明 |
| :--- | :--- | :--- |
| body | Binary | WAV 音频文件 |
| Content-Type | String | audio/wav 或远程文件服务返回类型 |

#### 返回示例JSON

```json
{
  "contentType": "audio/wav",
  "body": "<binary wav>"
}
```

#### code码说明

| code | msg | desc |
| :--- | :--- | :--- |
| 200 | success | 获取录音成功 |
| 404 | not found | 录音不存在或远程代理失败 |

#### 接口详细说明

```text
/recordings 用于访问本地保存的 WAV 文件。
/audio/{record_id} 用于通过 ASR HTTPS 网关代理远程录音，避免浏览器 Mixed Content 问题。
验证关注：录音文件可访问性、远程代理返回结果、浏览器播放地址和 404 错误处理。
```

---

#### 备注
```text
业务系统优先使用 audio.segment.audioUrl；监控页面可使用 localAudioUrl。
```

## 三、地址库后处理模块

### 3.1 地址库纠错事件
---
#### 接口说明

| URL | request | version | status |
| :--- | :--- | :--- | :--- |
| [wss://server_ip:8443/monitor](wss://server_ip:8443/monitor) | Server Push | 1.0 | true |
| exchange:ids:asr / exchange:ids:qs | Publish | 1.0 | true |

#### 请求参数说明

| 请求参数 | 类型 | 必填 | 参数说明 | 示例 |
| :--- | :--- | :--- | :--- | :--- |
| speech.final | Object | true | 地址库后处理输入，来自稳定 ASR 识别结果 | {...} |
| callId | String | true | 通话唯一标识 | call-001 |
| segmentId | String | true | 分段标识 | caller-0001 |
| speaker | String | false | 说话人 | caller |
| direction | String | false | 方向 | inbound |
| text | String | true | 原始 ASR 文本 | 深圳软件园一77栋有人被困 |

#### 请求示例JSON

```json
{
  "event": "speech.final",
  "callId": "call-001",
  "callfrom": "8015",
  "callto": "8014",
  "segmentId": "caller-0001",
  "speaker": "caller",
  "direction": "inbound",
  "text": "深圳软件园一77栋有人被困"
}
```

#### 返回参数说明

| 返回参数 | 参数类型 | 参数说明 |
| :--- | :--- | :--- |
| event | String | 固定为 call.corrected |
| callId | String | 通话唯一标识 |
| callfrom | String | 主叫号码 |
| callto | String | 被叫号码 |
| correctionScope | String | 修正范围，turn 表示单轮修正 |
| correctionProvider | String | 纠错来源，地址库对齐模式为 db_align |
| correctionMode | String | 纠错模式，地址库模式为 align |
| segmentId | String | 分段标识 |
| segmentIds | Array | 合并轮次包含的分段列表 |
| speaker | String | 说话人 |
| direction | String | 方向 |
| originalText | String | 修正前文本 |
| correctedText | String | 地址库纠错后的文本 |
| replacements | Array | 地址库命中的替换明细 |
| turns | Array | 分轮修正明细 |
| elapsedMs | Number | 地址库纠错耗时，毫秒 |
| sendTimeMs | Integer | 服务端发送时间 |

#### 返回示例JSON

```json
{
  "event": "call.corrected",
  "callId": "call-001",
  "callfrom": "8015",
  "callto": "8014",
  "correctionScope": "turn",
  "correctionProvider": "db_align",
  "correctionMode": "align",
  "segmentId": "caller-0001",
  "segmentIds": ["caller-0001"],
  "speaker": "caller",
  "direction": "inbound",
  "originalText": "报警人：深圳软件园一77栋有人被困",
  "correctedText": "深圳软件园一期七栋有人被困",
  "replacements": [
    {
      "original": "一77栋",
      "corrected": "一期七栋",
      "source": "address_db"
    }
  ],
  "turns": [
    {
      "segmentId": "caller-0001",
      "speaker": "caller",
      "direction": "inbound",
      "originalText": "深圳软件园一77栋有人被困",
      "correctedText": "深圳软件园一期七栋有人被困"
    }
  ],
  "elapsedMs": 18.4,
  "sendTimeMs": 1781510402000
}
```

#### code码说明

| code | msg | desc |
| :--- | :--- | :--- |
| 200 | corrected | 地址库后处理成功 |
| 204 | disabled | 地址库后处理未启用 |
| 204 | empty text | 文本为空，不触发后处理 |
| 500 | address db failed | 地址库加载或纠错失败，记录日志，不影响 ASR 主链路 |

#### 接口详细说明

```text
地址库后处理模块没有独立外部入口，由稳定 speech.final 事件触发。
该模块使用地址库候选词，对 ASR 文本中的地址、楼栋、楼层、房号等近音或错字进行对齐纠错。
输出 call.corrected，通过 /monitor 和 RabbitMQ 分发；失败时只记录日志，不阻塞 ASR 主链路。
```

---

#### 备注
```text
地址库后处理只修正有地址库依据且发音或文本相近的地址相关内容，不补充原文中没有的信息。
启用地址库模式时，建议配置 ASR_CORRECTION_PROVIDER=db_align、address_db 或 address_align。
```

---
#### Author

| Coder | 创建时间 | 更新时间 | 联系方式 |
| :--- | :--- | :--- | :--- |
| asr-team | 2026.7.1 | 2026.7.1 | - |
