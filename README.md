# 小红书商家客服 Agent 中台 MVP

这是基于 [ARCHITECTURE.md](D:\study\agent\codex\reply-agent\ARCHITECTURE.md) 实现的一版可运行 MVP，覆盖以下能力：

- 消息分类：售前咨询、催发货、售后、退换货、价格咨询
- 知识库检索：优先检索商品 FAQ、物流规则、售后政策
- 自动回复生成：按意图路由不同 Prompt 模板
- 回复质检：限制承诺性表述，拦截售后/赔付/时效乱答
- 会话标签与待跟进队列：高风险、低置信度、知识未命中自动入队
- 会话与审计记录：消息、意图、知识命中、回复、质检全链路留痕

## 项目结构

```text
app/
  core/
  models/
  services/
  main.py
data/
  knowledge_base.json
  agent.db         # 启动后自动生成
```

## 快速启动

```powershell
pip install -r requirements.txt
python -m uvicorn app.main:app --reload
```

启动后访问：

- Swagger UI: `http://127.0.0.1:8000/docs`
- 健康检查: `http://127.0.0.1:8000/health`

## 主要接口

- `POST /api/channel/xiaohongshu/events`：模拟小红书消息接入并触发完整处理链路
- `POST /api/demo/seed`：一键灌入演示数据
- `POST /api/demo/run`：一键触发一个演示场景
- `POST /internal/intent/recognize`：意图识别
- `POST /internal/kb/search`：知识检索
- `POST /internal/reply/generate`：回复生成
- `POST /internal/reply/check`：回复质检
- `GET /api/conversations/{conversation_id}`：查看会话详情
- `GET /api/follow-up/tasks`：查看待跟进队列
- `POST /api/follow-up/tasks/{task_id}/claim`：领取待跟进任务
- `POST /api/follow-up/tasks/{task_id}/resolve`：完成待跟进任务

## 示例请求

```powershell
$body = @{
  shop_id = "shop-demo"
  user_id = "user-001"
  content = "怎么还没发货，明天能到吗？"
  product_id = "sku-scarf"
  order_context = @{
    status = "paid"
    is_presale = $false
  }
} | ConvertTo-Json -Depth 5

Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/api/channel/xiaohongshu/events" `
  -Method Post `
  -ContentType "application/json" `
  -Body $body
```

## 当前实现说明

- 这是一版本地可运行 MVP，使用规则分类、关键词/BM25 风格简化检索和模板式回复生成。
- 回复生成服务已支持可选接入真实 LLM；若未配置 `LLM_API_KEY`，则自动回退到模板模式。
- 数据持久化使用 SQLite，适合本地验证和单机演示。
- 已内置可视化演示模式，打开首页即可配合演示数据查看完整中台流程。
- 已支持手动输入模拟客户消息，页面会直接展示 Agent 的意图识别、知识命中、回复生成和质检结果。
- 已支持通过页面上传 CSV 导入知识库，依赖 `python-multipart`，请先执行 `pip install -r requirements.txt`。

## 启用真实大模型

先编辑项目根目录下的 [.env](D:\study\agent\codex\reply-agent\.env)：

```env
LLM_API_KEY=your_api_key
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4.1-mini
```

也提供了模板文件 [.env.example](D:\study\agent\codex\reply-agent\.env.example)。

保存后重新启动服务：

```powershell
python -m uvicorn app.main:app --reload
```

然后在页面“运行策略”中开启“启用大模型回复”。

## 下一步建议

- 接入真实小红书回调和订单/物流上下文
- 将 `reply-service` 替换为真实 LLM Gateway
- 将知识库导入、向量化、重排拆成独立服务
- 增加多租户店铺配置中心和运营后台
