# ASR Docker 部署

本目录的 `compose.yaml` 会构建并启动完整的两层 ASR 服务：

- `funasr`：CPU Paraformer 推理，容器内端口 `10095`。
- `gateway`：HTTPS/WebSocket 网关、VAD、热词、纠错、数据库与 RabbitMQ 对接，容器内端口 `8443`。

代码和 Python 环境会进入镜像。模型、环境变量、证书、日志和录音作为运行数据保留在镜像外，避免泄漏密钥，也便于升级代码。

## 1. 准备配置

```bash
cd /home/twai/huilong/full_question_v6_strata/asr_api_use
cp docker.env.example .env
```

编辑 `.env`，至少把 `FUNASR_MODEL_DIR` 改为宿主机上的 Paraformer 模型绝对路径。当前机器可使用：

```text
/home/twai/xhw/FunASR/server/funasr-runtime-resources/models/iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch
```

如需数据库、RabbitMQ 或 LLM 后处理，再填写对应配置。不要把 `.env`、证书私钥或录音提交到 Git。

## 2. 构建

```bash
docker compose build
```

首次构建需要拉取约 3.2GB 的 FunASR CPU 基础镜像，并在模型服务镜像中缓存 VAD 模型。业务 Paraformer 模型不会被复制进镜像，而是在运行时只读挂载。

## 3. 启动

如果旧的 `funasr-paraformer-large` 容器和宿主机 Gateway 仍占用 `10097`、`8443`，先按原运维方式停止它们，再执行：

```bash
docker compose up -d
docker compose ps
docker compose logs -f --tail=100
```

访问地址：

```text
https://<服务器IP>:8443/
wss://<服务器IP>:8443/asr
wss://<服务器IP>:8443/asr-cpu-test
```

首次启动会在 `./certs` 自动生成开发自签证书。生产环境请把正式证书放到：

```text
./certs/cert.pem
./certs/key.pem
```

## 4. 停止与升级

```bash
docker compose down
docker compose build --pull
docker compose up -d
```

`docker compose down` 不会删除 `./recordings`、`./logs`、`./certs` 和宿主机模型。

## 5. 离线迁移镜像

联网机器构建完成后可导出两张镜像：

```bash
docker save \
  full-question-funasr:cpu \
  full-question-asr-gateway:latest \
  | gzip > full-question-asr-images.tar.gz
```

目标机器导入：

```bash
gzip -dc full-question-asr-images.tar.gz | docker load
```

模型目录仍需单独复制到目标机器，并通过 `.env` 的 `FUNASR_MODEL_DIR` 指向它。然后执行：

```bash
docker compose up -d --no-build
```

