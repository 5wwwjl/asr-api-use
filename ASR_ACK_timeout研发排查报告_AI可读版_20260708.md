# ASR `/asr` ACK Timeout 研发排查报告（AI 可读版）

生成日期：2026-07-08  
问题模块：ASR 实时接入与分发模块  
接口：`wss://192.168.173.167:9443/asr`  
关联接口：`wss://192.168.173.167:9443/monitor`  
问题等级：P1，阻塞 20 并发以上正式压测验收  
当前结论：20 并发下 `/asr` 建连成功，但 ACK 返回链路不稳定，主要表现为 `ack_timeout`。

---

## 1. 给研发的最短结论

`/asr ACK timeout` 不是端口不通，也不是 WebSocket 建连失败。客户端已经成功连接并发送了 `call.started` 或 `audio.frame`，但在 10 秒 ACK 等待窗口内没有读到服务端 ACK。

目前最可疑的位置是：

```text
服务端收到 audio.frame 之后，到发送 ACK 之前，存在排队、阻塞或调度延迟。
```

也可能存在：

```text
服务端 ACK 已经发送，但客户端超时关闭连接后才到达，导致服务端后续写 ACK 时 transport 已 closing。
```

研发排查时不要先改超时时间，应该先补齐 ACK 链路打点，确认 ACK 卡在哪一段。

---

## 2. 问题现象

### 2.1 旧 JMeter rerun16d 结果

| 指标 | 结果 |
|---|---:|
| 并发 | 20 |
| planned calls | 100 |
| `/asr` open success | 100/100 |
| `/monitor` open success | 1/1 |
| full success call count | 60/100 |
| first error call count | 40 |
| first error type | `ack_timeout = 40` |
| first error event type | `audio.frame = 36`，`call.started = 4` |
| accepted=false | 0 |
| write_failed_after_first_error_count | 0 |
| final_active_sessions | 0 |

服务端 ACK 发送统计：

| event_type | ACK-SEND-ABOUT-TO | ACK-SEND-OK | ACK-SEND-FAILED |
|---|---:|---:|---:|
| call.started | 100 | 100 | 0 |
| audio.frame | 23217 | 23208 | 9 |
| call.ended | 60 | 60 | 0 |

服务端 `ACK-SEND-FAILED` 异常：

```text
ClientConnectionResetError: Cannot write to closing transport
```

关键证据：

```text
ACK-SEND-ABOUT-TO 时 transport_is_closing=true：9/9
ACK-SEND-FAILED 时 transport_is_closing=true：9/9
服务端先 SESSION-CLEANUP 再 ACK-SEND-FAILED：0/9
服务端先 SESSION-TIMEOUT 再 ACK-SEND-FAILED：0/9
```

解释：不是服务端先清理 session 再发 ACK，而是 ACK 发送时连接已经进入 closing 状态。

### 2.2 2026-07-08 Node 诊断复测结果

本轮使用 Node 24 原生 WebSocket 做等价诊断，非正式 JMeter 验收。

| 指标 | 结果 |
|---|---:|
| 并发 | 20 |
| 通话数 | 100 |
| 每通 audio.frame | 232 |
| `/asr` open success | 100/100 |
| full success call count | 87/100 |
| failed call count | 13 |
| ACK 成功 | 21284/21297 |
| 平均 ACK 延迟 | 140.36 ms |
| P95 ACK 延迟 | 593 ms |
| P99 ACK 延迟 | 1841 ms |
| 失败类型 | `ack_timeout = 13` |

失败阶段分布：

| phase | 数量 |
|---|---:|
| audio.frame | 11 |
| call.started | 2 |

失败明细：

```csv
call_id,phase,seq,elapsed_ms,error
ASR-NODE-20260708032504-003,audio.frame,78,10009,ack_timeout
ASR-NODE-20260708032504-018,audio.frame,46,10007,ack_timeout
ASR-NODE-20260708032504-021,audio.frame,162,10000,ack_timeout
ASR-NODE-20260708032504-023,call.started,1,10007,ack_timeout
ASR-NODE-20260708032504-024,call.started,1,10011,ack_timeout
ASR-NODE-20260708032504-026,audio.frame,53,10001,ack_timeout
ASR-NODE-20260708032504-030,audio.frame,100,10000,ack_timeout
ASR-NODE-20260708032504-042,audio.frame,200,10016,ack_timeout
ASR-NODE-20260708032504-054,audio.frame,26,10003,ack_timeout
ASR-NODE-20260708032504-063,audio.frame,81,10019,ack_timeout
ASR-NODE-20260708032504-070,audio.frame,41,10016,ack_timeout
ASR-NODE-20260708032504-078,audio.frame,136,10004,ack_timeout
ASR-NODE-20260708032504-084,audio.frame,14,10005,ack_timeout
```

解释：当前复测下问题比旧 JMeter rerun16d 轻，但方向一致：20 并发仍然存在 ACK timeout。

---

## 3. 已经可以排除的方向

| 排除项 | 证据 |
|---|---|
| 端口不可达 | `192.168.173.167:9443` TCP 可连接。 |
| `/asr` 建连失败 | JMeter rerun16d 和 Node 复测均为 `/asr open success = 100/100`。 |
| 全部 ACK 都失败 | 大量 ACK 成功，Node 复测 ACK 成功 21284/21297。 |
| 服务端业务拒绝 | 旧报告 `accepted=false = 0`。 |
| 压测端继续写坏连接导致错误雪崩 | 旧报告 ackskip 后 `write_failed_after_first_error_count = 0`。 |
| 单纯文本聚合问题 | 失败点发生在 ACK 阶段，早于最终文本聚合。 |
| 单纯内存不足 | Node 复测后 MemAvailable 约 75.8 GiB，无明显内存耗尽。 |

---

## 4. ACK 链路拆解

一个 `audio.frame` 的理想链路应该是：

```text
T1 客户端发送 audio.frame
T2 服务端 WebSocket 收到 frame
T3 服务端完成基础校验
T4 服务端准备发送 ACK
T5 服务端 ACK send 完成
T6 客户端收到 ACK
T7 音频帧异步进入 VAD/ASR/分发队列
```

如果 ACK 语义只是“网关已接收”，推荐顺序是：

```text
收到 frame -> 校验 callId/seq/payload -> 入队 -> 立即 ACK -> 后续异步处理
```

不建议：

```text
收到 frame -> 等 VAD/ASR/monitor/落库/日志处理 -> 再 ACK
```

20 并发时后者很容易让 ACK 被业务处理拖慢。

---

## 5. 根因假设优先级

### H1：ACK 前存在业务处理阻塞或排队

优先级：最高  
可能性：高

表现：

```text
server_ws_recv_at 正常
但 ack_about_to_send_at 明显晚
```

可能原因：

- `audio.frame` ACK 前等待 VAD 或 ASR upstream；
- ACK 前等待 monitor 广播；
- ACK 前写文件、落库或同步日志；
- ACK 前进入阻塞队列；
- 单连接或全局锁导致排队。

### H2：Python asyncio event loop 被阻塞

优先级：高  
可能性：高

可能原因：

- 同步 CPU 计算；
- 同步 IO；
- 每帧大量日志输出；
- 阻塞式队列 put/get；
- 在 async handler 内调用阻塞函数；
- 单线程 event loop 同时处理 WebSocket、ASR upstream、monitor 分发。

### H3：WebSocket send 出现背压或连接进入 closing

优先级：高  
可能性：中高

证据：旧报告中：

```text
ACK-SEND-FAILED: Cannot write to closing transport
transport_is_closing=true
```

需要确认：

- 是客户端先超时 close，服务端后发 ACK；
- 还是服务端 transport 先进入 closing；
- 是否存在 TCP zero window / retransmission；
- send ACK 时耗时是否异常。

### H4：客户端/JMeter 读帧时序问题

优先级：中  
可能性：中

证据：旧报告中 close 阶段读到迟到 ACK Text frame，说明部分 ACK 可能已经到达但错过了 ACK read sampler 的消费窗口。

但 Node 复测不用 JMeter 也出现 ACK timeout，所以不能只归因于 JMeter 插件。

### H5：网络/TLS 层问题

优先级：中低  
可能性：待证据确认

需要抓包确认：

- client FIN/RST 和 server FIN/RST 谁先发生；
- 是否存在重传、zero window、MTU/TLS record 堵塞；
- 关键失败时间点 pcap 是否覆盖。

旧 pcap 没覆盖关键 ACK-SEND-FAILED 时间点，所以不能定责。

---

## 6. 研发需要补充的日志字段

请在服务端 `/asr` WebSocket handler 中按每条上行事件补齐如下字段。

### 6.1 每条上行消息基础字段

```json
{
  "trace_id": "callId + seq",
  "call_id": "...",
  "seq": 78,
  "event_type": "audio.frame",
  "connection_id": "...",
  "payload_bytes": 10240,
  "active_asr_connections": 20,
  "active_sessions": 20
}
```

### 6.2 ACK 时序字段

```json
{
  "client_send_at": "如果客户端传了 timestamp 可记录",
  "server_ws_recv_at": "服务端收到 WebSocket 消息时间",
  "json_parse_done_at": "JSON 解析完成时间",
  "basic_validate_done_at": "基础校验完成时间",
  "queue_put_start_at": "入队开始时间",
  "queue_put_done_at": "入队完成时间",
  "ack_about_to_send_at": "准备发送 ACK 时间",
  "ack_send_done_at": "ACK 发送完成时间",
  "ack_send_elapsed_ms": 0.0,
  "ack_total_elapsed_ms": 0.0
}
```

### 6.3 transport 状态字段

```json
{
  "transport_is_closing_before_ack": false,
  "transport_is_closing_after_ack": false,
  "websocket_closed": false,
  "close_code": null,
  "close_reason": null,
  "peername": "client_ip:port"
}
```

### 6.4 队列与事件循环字段

```json
{
  "audio_frame_queue_depth": 0,
  "asr_upstream_queue_depth": 0,
  "monitor_dispatch_queue_depth": 0,
  "event_loop_lag_ms": 0.0,
  "ack_queue_depth": 0
}
```

### 6.5 异常日志字段

```json
{
  "error_stage": "ack_send | queue_put | parse | validate | upstream | monitor_dispatch",
  "error_type": "ClientConnectionResetError",
  "error_message": "Cannot write to closing transport",
  "trace_id": "callId + seq",
  "last_success_seq": 77
}
```

---

## 7. 判定规则：如何定位卡在哪一层

| 证据模式 | 说明 | 下一步 |
|---|---|---|
| `server_ws_recv_at - client_send_at` 很大 | 网络、TLS、服务端接收层排队 | 抓包、看 TCP 重传和服务端 accept/read 压力。 |
| `ack_about_to_send_at - server_ws_recv_at` 很大 | ACK 前业务处理阻塞 | 查 ACK 前是否做 VAD/ASR/落库/日志/monitor。 |
| `ack_send_done_at - ack_about_to_send_at` 很大 | WebSocket send 背压 | 查 transport、客户端窗口、网络、send buffer。 |
| `ack_send_done_at` 正常但客户端没收到 | 客户端读帧、网络或 TLS 问题 | 客户端抓包、服务端抓包对齐。 |
| 客户端 timeout 后服务端才 ACK | ACK 迟到 | 解耦 ACK 或缩短 ACK 前路径。 |
| `transport_is_closing_before_ack=true` | 连接在 ACK 前已关闭 | 查谁先 close：client timeout 还是 server close。 |

---

## 8. 建议做的最小验证实验

### 实验 A：立即 ACK 模式

目的：验证 ACK timeout 是否由 ACK 前业务链路导致。

实验方式：临时加一个开关，例如：

```text
ASR_ACK_MODE=immediate
```

逻辑：

```text
收到 audio.frame
-> 只做 JSON 解析和基础字段校验
-> 立即 ACK accepted=true
-> 后续 VAD/ASR/monitor 全部异步处理或直接跳过
```

判定：

| 结果 | 结论 |
|---|---|
| 20 并发 100/100 通过 | 根因在 ACK 前业务处理或队列阻塞。 |
| 仍出现 ACK timeout | 继续查 WebSocket send、event loop、网络或客户端读取。 |

### 实验 B：禁用 ASR upstream，仅保留 ACK

目的：判断 ASR upstream 是否拖慢 ACK。

逻辑：

```text
/asr 接收 frame -> ACK -> 不转发 FunASR，不做识别
```

如果通过，说明 upstream 或其队列对 ACK 路径有影响。

### 实验 C：关闭每帧日志

目的：判断日志 IO 是否拖慢 event loop。

要求：

```text
20 并发下禁止每个 audio.frame 打 info 日志
只保留每通 summary 和异常日志
```

如果 timeout 明显减少，说明日志输出是关键压力点。

### 实验 D：固定帧数阶梯

目的：判断 ACK timeout 是否随帧数累计放大。

测试矩阵：

| 并发 | 通话数 | 每通 frame |
|---:|---:|---:|
| 20 | 100 | 10 |
| 20 | 100 | 60 |
| 20 | 100 | 232 |
| 20 | 100 | 500 |

观察：

```text
failed_call_count 是否随 frame 数上升
P95/P99 ACK latency 是否随 frame 数上升
audio.frame queue depth 是否持续上涨
```

### 实验 E：抓包覆盖关键失败窗口

目的：确认谁先关闭连接。

要求：

```text
pcap 必须覆盖 ACK timeout 发生前后至少 60 秒
服务端日志时间与 pcap 时间同步
记录 callId/seq 与 client ip:port 映射
```

看：

```text
client FIN/RST 先出现？
server FIN/RST 先出现？
是否有 retransmission / zero window？
```

---

## 9. 推荐修复方向

在根因确认前，不建议直接把 ACK timeout 从 10 秒加到 30 秒。那只是掩盖问题。

优先考虑：

1. **ACK 与识别处理解耦**

```text
收到 frame -> 基础校验 -> 入队 -> 立即 ACK
```

2. **ACK 独立优先级**

ACK 发送不要排在 VAD、ASR、monitor 广播、落库后面。

3. **异步队列限流和背压**

如果内部队列满，应快速返回：

```json
{"type":"ack","accepted":false,"message":"queue_full"}
```

不要让客户端一直等到 timeout。

4. **降低每帧日志**

每帧日志只在 debug 模式打开；压测时保留计数器和异常日志。

5. **增加 ACK latency histogram**

至少按事件类型区分：

```text
ack_latency_ms{event_type="call.started"}
ack_latency_ms{event_type="audio.frame"}
ack_latency_ms{event_type="call.ended"}
```

6. **增加 event loop lag 监控**

如果是 Python asyncio，建议暴露：

```text
event_loop_lag_ms
slow_callback_count
pending_task_count
```

---

## 10. 研发排查 checklist

### 第一优先级

- [ ] 确认 ACK 语义：ACK 是否只表示网关接收成功？是否需要等待 ASR/VAD？
- [ ] 给每条 `call.started` / `audio.frame` / `call.ended` 加 ACK 时序日志。
- [ ] 统计 `server_ws_recv_at -> ack_about_to_send_at` 的 P95/P99。
- [ ] 统计 `ack_about_to_send_at -> ack_send_done_at` 的 P95/P99。
- [ ] 确认 ACK 前是否有同步阻塞操作。
- [ ] 确认每帧是否有大量 info 日志。

### 第二优先级

- [ ] 增加 event loop lag 指标。
- [ ] 增加内部队列长度指标。
- [ ] 增加 transport closing 前后日志。
- [ ] 抓包覆盖完整失败窗口。
- [ ] 对齐客户端 callId/seq 和服务端 trace_id。

### 第三优先级

- [ ] 恢复旧 `C:\asr-jmeter-test` 工程，按 rerun16d 同口径复测。
- [ ] 增加 Node 客户端与 JMeter 客户端的对比压测。
- [ ] 增加 `/monitor` 长连接稳定性单独压测。

---

## 11. AI 排查提示词

如果研发把服务端日志发回来，可以用下面提示词让 AI 继续分析：

```text
你是后端性能排查助手。下面是 ASR /asr WebSocket 20 并发 ACK timeout 的服务端日志。
请按 callId+seq 重建链路，重点分析：
1. server_ws_recv_at 到 ack_about_to_send_at 是否异常；
2. ack_about_to_send_at 到 ack_send_done_at 是否异常；
3. timeout 样本是否集中在 audio.frame；
4. 是否存在 event loop lag、queue depth 上涨、transport_is_closing；
5. 谁先关闭连接，client 还是 server；
6. 给出最可能根因和下一步最小验证实验。
不要直接建议加大 timeout，除非证据证明只是客户端窗口太短。
```

---

## 12. 关联文件

| 文件 | 路径 |
|---|---|
| 20 并发复测报告 | `D:\TW\tw_v2\asr测试\ASR_20并发复测问题报告_20260708.md` |
| 旧 JMeter 20 并发报告 | `D:\TW\tw_v2\asr测试\ASR_20u_test_result_report_rerun16d.md` |
| 压测踩坑复盘 | `D:\TW\tw_v2\asr测试\asr_pressure_test_pitfalls_summary.md` |
| Node 诊断脚本 | `D:\TW\tw_v2\asr测试\asr_ws_20u_diag.mjs` |
| Node 20 并发 232 帧结果 | `D:\TW\tw_v2\asr测试\results\asr_20u_node_diag_20260708032504.csv` |
| Node 20 并发 232 帧汇总 | `D:\TW\tw_v2\asr测试\results\asr_20u_node_diag_summary_20260708032504.json` |
