# 语音流推送对接接口

## 连接地址

```
WSS wss://sqasr.telewave.com.cn:8443/asr?callId=<callId>
```

---

## 上行（外部公司 → 我方）

### call.started — 通话开始

```json
{
  "eventType": "call.started",
  "callId": "call-20260506-0001",
  "streamId": "stream-main",
  "seq": 1,
  "timestampMs": 0,
  "payload": {}
}
```

### audio.frame — 音频帧

```json
{
  "eventType": "audio.frame",
  "callId": "call-20260506-0001",
  "seq": 10,
  "timestampMs": 320,
  "payload": {
    "speaker": "caller",
    "direction": "inbound",
    "startTimeMs": 320,
    "endTimeMs": 640,
    "codec": "pcm_s16le",
    "sampleRate": 16000,
    "channels": 1,
    "frameDurationMs": 320,
    "audioBase64": "..."
  }
}
```

音频要求：`pcm_s16le`，16kHz，单声道，Base64 编码。

### call.ended — 通话结束

```json
{
  "eventType": "call.ended",
  "callId": "call-20260506-0001",
  "seq": 999,
  "timestampMs": 45200,
  "payload": {}
}
```

---

## 下行（我方 → 外部公司）

### ack — 每帧确认

```json
{
  "type": "ack",
  "callId": "call-20260506-0001",
  "receivedSeq": 10,
  "accepted": true,
  "message": "ok"
}
```

`accepted: false` 时 `message` 取值：`INVALID_JSON`、`MISSING_REQUIRED_FIELD`、`ASR upstream unavailable`。

### speech.final — 识别结果

```json
{
  "eventType": "speech.final",
  "callId": "call-20260506-0001",
  "seq": 1,
  "payload": {
    "text": "深圳大学南区三栋二单元五楼有人被困",
    "confidence": 0.9,
    "language": "zh-CN"
  }
}
```

---

## 收发示例

```
→ call.started
← ack

→ audio.frame
← ack
→ audio.frame
← ack
  ...

→ call.ended
← ack
← speech.final
```
