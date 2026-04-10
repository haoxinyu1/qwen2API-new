# qwen2API

将通义千问（chat.qwen.ai）网页版能力转换为 OpenAI、Claude 与 Gemini 兼容 API。

---

## 一、最快部署方式（服务器 / Linux / Windows 通用）

**只需三步，无需编译，直接拉取预构建镜像运行。**

### 第一步：下载 docker-compose.yml

```bash
curl -O https://raw.githubusercontent.com/YuJunZhiXue/qwen2API/main/docker-compose.yml
```

或者手动创建 `docker-compose.yml`，内容如下：

```yaml
services:
  qwen2api:
    image: yujunzhixue/qwen2api:latest
    container_name: qwen2api
    restart: unless-stopped
    env_file:
      - path: .env
        required: false
    ports:
      - "7860:7860"
    volumes:
      - ./data:/workspace/data
      - ./logs:/workspace/logs
    shm_size: '256m'
    environment:
      PYTHONIOENCODING: utf-8
      PORT: "7860"
      ENGINE_MODE: "hybrid"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:7860/healthz"]
      interval: 30s
      timeout: 10s
      start_period: 120s
      retries: 3
```

### 第二步：（可选）创建 .env 配置文件

不创建也能启动，会使用默认配置。**建议至少改掉 `ADMIN_KEY`**：

```bash
cat > .env << 'EOF'
ADMIN_KEY=your-strong-password
PORT=7860
ENGINE_MODE=hybrid
BROWSER_POOL_SIZE=2
MAX_INFLIGHT=1
EOF
```

### 第三步：启动

```bash
docker compose up -d
```

首次运行会自动拉取镜像（约 1~2 GB），之后直接启动。

**访问**：`http://服务器IP:7860`

---

## 二、常用命令

```bash
# 查看运行状态
docker compose ps

# 查看实时日志
docker compose logs -f

# 停止
docker compose down

# 更新到最新镜像
docker compose pull && docker compose up -d

# 重启
docker compose restart
```

---

## 三、添加千问账号

1. 打开 `http://服务器IP:7860`
2. 左侧菜单 → **账号管理** → **添加账号**
3. 输入千问邮箱和密码，系统自动验证并获取 Token

---

## 四、API 使用

Base URL：`http://服务器IP:7860`

鉴权：请求头加 `Authorization: Bearer YOUR_KEY`（没有配置 API Key 时用 `ADMIN_KEY`）

### OpenAI 格式

```bash
curl http://localhost:7860/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_KEY" \
  -d '{
    "model": "qwen3.6-plus",
    "stream": true,
    "messages": [{"role": "user", "content": "你好"}]
  }'
```

### Claude 格式（Claude Code / Anthropic SDK）

```bash
curl http://localhost:7860/anthropic/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: YOUR_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "claude-sonnet-4-6",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "你好"}]
  }'
```

### Claude Code 配置

```bash
export ANTHROPIC_BASE_URL=http://localhost:7860/anthropic
export ANTHROPIC_API_KEY=YOUR_KEY
```

### 图片生成

```bash
curl http://localhost:7860/v1/images/generations \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_KEY" \
  -d '{
    "model": "dall-e-3",
    "prompt": "一只赛博朋克风格的猫，霓虹灯背景",
    "n": 1,
    "size": "1024x1024"
  }'
```

---

## 五、配置参数

`.env` 文件支持的参数：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `ADMIN_KEY` | `admin` | 管理员密钥，**务必修改** |
| `PORT` | `7860` | 服务端口 |
| `ENGINE_MODE` | `hybrid` | `hybrid` / `browser` / `httpx` |
| `BROWSER_POOL_SIZE` | `2` | 浏览器并发页面数 |
| `MAX_INFLIGHT` | `1` | 每账号最大并发请求数 |
| `ACCOUNT_MIN_INTERVAL_MS` | `1200` | 同账号请求最小间隔（毫秒） |
| `MAX_RETRIES` | `2` | 失败最大重试次数 |
| `REGISTER_SECRET` | `""` | 注册码（空=任何人可注册 Key） |

完整参数参见 `.env.example`。

---

## 六、数据持久化

| 宿主机目录 | 说明 |
|-----------|------|
| `./data/` | 账号凭证、API Key、配置（重要，请备份） |
| `./logs/` | 运行日志 |

---

## 七、模型映射

所有主流模型名自动映射到 `qwen3.6-plus`：

`gpt-4o` / `gpt-4.1` / `o1` / `o3` / `claude-sonnet-4-6` / `claude-opus-4-6` / `gemini-2.5-pro` / `deepseek-chat` → **`qwen3.6-plus`**

---

## 八、从源码构建（开发者）

```bash
git clone https://github.com/YuJunZhiXue/qwen2API.git
cd qwen2API

# 本地一键启动（自动安装依赖 + 下载浏览器 + 构建前端）
python start.py
```

或者 Docker 本地构建：

```bash
# 使用本地 Dockerfile 构建（不拉取预构建镜像）
docker compose -f docker-compose.yml up -d --build
```

> 本地构建需要下载 Camoufox 浏览器内核（约 100MB），需要网络能访问 GitHub，国内服务器建议使用代理。

---

## 九、常见问题

**Q：docker compose up 报 `.env not found`**  
A：创建一个空文件即可：`touch .env`，或升级 Docker Compose 到 v2.24+。

**Q：端口 7860 被占用**  
A：修改 `.env` 里的 `PORT=8080`，同时修改 `docker-compose.yml` 的 ports 为 `"8080:8080"`。

**Q：浏览器崩溃 / 内存不足**  
A：修改 `docker-compose.yml` 中 `shm_size: '512m'`，然后 `docker compose restart`。

**Q：图片生成返回错误**  
A：查看日志 `docker compose logs -f`，确认账号状态正常（账号管理页面显示「正常」）。

---

## 许可证

MIT License — 仅供个人学习研究使用，严禁用于商业灰产，违者自负。
