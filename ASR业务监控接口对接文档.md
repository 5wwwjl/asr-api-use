# ASR 业务监控接口对接文档

## 1. 文档用途

本文档面向需要消费 ASR 结果的业务系统，例如：

- 实时展示接警员与报警人的转写文本
- 按说话人展示 Q/A 对话
- 展示 VAD 状态、静音时长和当前音量
- 播放每个语音分段对应的录音

业务系统不需要复制或部署 ASR 源代码，也不需要向该接口上传音频。业务系统只需建立 WebSocket 连接，接收 ASR 服务主动广播的实时事件。

音频生产方使用 `/asr` 上传音频；业务消费方使用 `/monitor` 接收结果，两者职责不同。

## 2. 服务地址

### 2.1 实时事件 WebSocket

```text
wss://<ASR服务器>:8443/monitor
```

示例：

```text
wss://192.168.173.167:8443/monitor
```

连接建立后，客户端不需要发送订阅消息，持续接收服务端推送即可。

### 2.2 分段录音 HTTP 地址

`audio.segment` 事件中的 `audioUrl` 是相对路径：

```text
/recordings/2026-06-15/<callId>/agent-0001-....wav
```

业务系统需要拼接 ASR 服务的 HTTPS 地址：

```text
https://<ASR服务器>:8443${audioUrl}
```

## 3. 核心数据关系

每个通话使用 `callId` 标识，每个 VAD 语音分段使用 `segmentId` 标识。

业务系统必须使用以下组合键关联文本和录音：

```text
callId + segmentId
```

例如：

```text
call-001 + agent-0002
```

同一分段的 `speech.final` 和 `audio.segment` 到达顺序不固定，客户端应先缓存事件，再按组合键合并。

### 3.1 说话人字段

| `speaker` | 含义 | 推荐显示 |
|---|---|---|
| `agent` | 接警员 | Q |
| `caller` | 报警人 | A |
| `system` | 系统语音 | Q 或系统 |
| `unknown` | 未知 | 未知 |

Q/A 编号属于展示逻辑，不是接口字段。业务系统可按通话内事件顺序生成 `Q1`、`A1`、`Q2`、`A2`。

## 4. 事件类型

服务端通过 `/monitor` 推送以下 JSON 事件：

| `event` | 说明 |
|---|---|
| `call.started` | 通话开始 |
| `speech.vad` | VAD、静音时长及音量状态 |
| `speech.final` | 当前语音分段的转写文本 |
| `audio.segment` | 当前语音分段对应的 WAV 录音 |
| `call.ended` | 通话结束 |

## 5. 事件结构

### 5.1 `call.started`

```json
{
  "event": "call.started",
  "callId": "call-001",
  "callfrom": "8015",
  "callto": "8014",
  "streamId": "stream-main"
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `callId` | string | 音频流/通话标识 |
| `callfrom` | string | 主叫号码 |
| `callto` | string | 被叫号码 |
| `streamId` | string | 音频流标识 |

一通双向电话当前可能由两个 `callId` 表示。业务系统如需合并为一张通话卡片，可使用排序后的 `callfrom + callto` 作为电话对标识。

### 5.2 `speech.final`

```json
{
  "event": "speech.final",
  "callId": "call-001",
  "segmentId": "agent-0001",
  "callfrom": "8015",
  "callto": "8014",
  "speaker": "agent",
  "text": "请问具体地址在哪里？",
  "startTimeMs": 1200,
  "endTimeMs": 3500,
  "durationMs": 2300,
  "sendTimeMs": 1781510400000
}
```

注意：

- 同一 `callId + segmentId` 可能收到多次 `speech.final`。
- 后续文本通常是对前一次文本的修正或补全。
- 客户端必须覆盖同一分段的旧文本，不能每次追加成新记录。

### 5.3 `audio.segment`

```json
{
  "event": "audio.segment",
  "callId": "call-001",
  "segmentId": "agent-0001",
  "callfrom": "8015",
  "callto": "8014",
  "speaker": "agent",
  "direction": "outbound",
  "audioUrl": "/recordings/2026-06-15/call-001/agent-0001.wav",
  "audioDurationMs": 2300,
  "startTimeMs": 1200,
  "endTimeMs": 3500,
  "sendTimeMs": 1781510401000
}
```

播放地址：

```text
https://<ASR服务器>:8443/recordings/2026-06-15/call-001/agent-0001.wav
```

浏览器示例：

```html
<audio
  controls
  preload="metadata"
  src="https://192.168.173.167:8443/recordings/2026-06-15/call-001/agent-0001.wav">
</audio>
```

录音由 ASR 服务永久保存，当前不会自动删除。

### 5.4 `speech.vad`

```json
{
  "event": "speech.vad",
  "callId": "call-001",
  "callfrom": "8015",
  "callto": "8014",
  "speaker": "agent",
  "direction": "outbound",
  "vadState": "speaking",
  "silenceDurationMs": 0,
  "volumeDb": -25.4,
  "audioLevel": 58,
  "startTimeMs": 1200,
  "endTimeMs": 1800,
  "sendTimeMs": 1781510400000
}
```

`vadState` 取值：

| 值 | 说明 |
|---|---|
| `speaking` | 当前检测到语音 |
| `silence` | 当前处于静音 |
| `ended` | 当前语音分段已结束 |

`audioLevel` 范围为 `0-100`，适合直接渲染音量条。`volumeDb` 为 dBFS 值。

### 5.5 `call.ended`

```json
{
  "event": "call.ended",
  "callId": "call-001",
  "callfrom": "8015",
  "callto": "8014"
}
```

收到后可将对应 `callId` 标记为结束。若一通电话有两个方向的 `callId`，应等待两个 `callId` 都结束后，再将整通电话标记为结束。

## 6. 浏览器完整示例

```html
<!doctype html>
<html lang="zh-CN">
<body>
  <div id="status">未连接</div>
  <div id="segments"></div>

  <script>
    const ASR_ORIGIN = "https://192.168.173.167:8443";
    const MONITOR_URL = ASR_ORIGIN.replace(/^http/, "ws") + "/monitor";

    // key: callId:segmentId
    const segments = new Map();
    let ws;
    let reconnectTimer;

    function segmentKey(event) {
      return `${event.callId}:${event.segmentId}`;
    }

    function getSegment(event) {
      const key = segmentKey(event);
      if (!segments.has(key)) {
        segments.set(key, {
          callId: event.callId,
          segmentId: event.segmentId,
          speaker: event.speaker,
          text: "",
          audioUrl: "",
          audioDurationMs: 0
        });
      }
      return segments.get(key);
    }

    function handleEvent(event) {
      switch (event.event) {
        case "call.started":
          console.log("通话开始", event.callId);
          break;

        case "speech.final": {
          const segment = getSegment(event);
          segment.speaker = event.speaker;
          segment.text = event.text; // 覆盖，不追加
          segment.startTimeMs = event.startTimeMs;
          segment.endTimeMs = event.endTimeMs;
          render();
          break;
        }

        case "audio.segment": {
          const segment = getSegment(event);
          segment.speaker = event.speaker;
          segment.audioUrl = new URL(event.audioUrl, ASR_ORIGIN).href;
          segment.audioDurationMs = event.audioDurationMs;
          render();
          break;
        }

        case "speech.vad":
          console.log(
            "VAD",
            event.callId,
            event.speaker,
            event.vadState,
            event.volumeDb
          );
          break;

        case "call.ended":
          console.log("通话结束", event.callId);
          break;
      }
    }

    function connect() {
      clearTimeout(reconnectTimer);
      document.getElementById("status").textContent = "连接中";
      ws = new WebSocket(MONITOR_URL);

      ws.onopen = () => {
        document.getElementById("status").textContent = "已连接";
      };

      ws.onmessage = ({ data }) => {
        try {
          handleEvent(JSON.parse(data));
        } catch (error) {
          console.error("ASR 事件解析失败", error, data);
        }
      };

      ws.onerror = () => {
        document.getElementById("status").textContent = "连接异常";
      };

      ws.onclose = () => {
        document.getElementById("status").textContent = "已断开，5 秒后重连";
        reconnectTimer = setTimeout(connect, 5000);
      };
    }

    function escapeHtml(value) {
      const node = document.createElement("div");
      node.textContent = value || "";
      return node.innerHTML;
    }

    function render() {
      const html = Array.from(segments.values()).map(segment => {
        const role = segment.speaker === "agent" ? "接警员/Q" : "报警人/A";
        const audio = segment.audioUrl
          ? `<audio controls preload="metadata" src="${escapeHtml(segment.audioUrl)}"></audio>`
          : "录音生成中";

        return `
          <section>
            <strong>${role}</strong>
            <p>${escapeHtml(segment.text || "识别中")}</p>
            ${audio}
          </section>
        `;
      }).join("");

      document.getElementById("segments").innerHTML = html;
    }

    connect();
  </script>
</body>
</html>
```

## 7. Python 服务端消费示例

安装依赖：

```bash
pip install websockets
```

示例代码：

```python
import asyncio
import json
import ssl

import websockets

MONITOR_URL = "wss://192.168.173.167:8443/monitor"


async def consume():
    ssl_context = ssl.create_default_context()

    async with websockets.connect(
        MONITOR_URL,
        ssl=ssl_context,
        ping_interval=20,
        ping_timeout=20,
    ) as ws:
        async for message in ws:
            event = json.loads(message)

            if event["event"] == "speech.final":
                key = (event["callId"], event["segmentId"])
                print("文本", key, event["speaker"], event["text"])

            elif event["event"] == "audio.segment":
                key = (event["callId"], event["segmentId"])
                audio_url = (
                    "https://192.168.173.167:8443"
                    + event["audioUrl"]
                )
                print("录音", key, audio_url)


asyncio.run(consume())
```

生产环境应在连接断开后执行带退避的自动重连。

## 8. 推荐的客户端状态结构

```json
{
  "calls": {
    "call-001": {
      "status": "active",
      "callfrom": "8015",
      "callto": "8014",
      "segments": {
        "agent-0001": {
          "speaker": "agent",
          "text": "请问具体地址在哪里？",
          "audioUrl": "https://服务器:8443/recordings/...wav",
          "startTimeMs": 1200,
          "endTimeMs": 3500
        }
      }
    }
  }
}
```

## 9. 对接注意事项

1. `/monitor` 是只读接口，客户端不需要向其发送业务消息。
2. 当前 `/monitor` 会广播所有通话，客户端可按 `callId` 或电话号码过滤。
3. 当前没有鉴权和租户隔离，不应直接暴露到不可信网络。
4. 当前只提供实时广播，不提供历史补发。
5. 客户端在通话中途连接，只能收到连接之后发生的事件。
6. 页面刷新或客户端重连后，之前的文本状态不会由 `/monitor` 自动恢复。
7. `speech.final` 和 `audio.segment` 可能乱序到达，必须按 `callId + segmentId` 合并。
8. 同一 `segmentId` 的文本更新必须覆盖旧文本。
9. 录音 URL 是相对地址，需要和 ASR HTTPS 服务地址拼接。
10. 如果使用自签名证书，测试环境客户端需要信任证书；生产环境应使用受信任的 TLS 证书。

## 10. 联调步骤

1. 浏览器访问：

   ```text
   https://<ASR服务器>:8443/monitor.html
   ```

   确认网关可访问。

2. 业务系统连接：

   ```text
   wss://<ASR服务器>:8443/monitor
   ```

3. 发起测试电话。
4. 确认收到 `call.started`。
5. 双方说话，确认收到 `speech.vad` 和 `speech.final`。
6. 一段语音结束后，确认收到 `audio.segment`。
7. 使用 `callId + segmentId` 检查文本和录音一一对应。
8. 挂断后确认收到 `call.ended`。

## 11. 当前接口能力边界

当前接口适合实时监控页面和内部服务消费。若用于正式跨系统集成，建议后续补充：

- WebSocket 鉴权
- 按 `callId` 或电话号码订阅
- 历史通话 REST 查询接口
- 断线重连后的事件补发
- 服务端持久化 Q/A 结果
- 事件序号或游标

