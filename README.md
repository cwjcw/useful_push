# useful_push

每天早上 7 点，通过方糖（Server酱）分别推送多条通知，覆盖：

- AI 新闻、机器人新闻、财经新闻、科技新闻（每类独立推送，自动翻译 + 摘要，保留原文链接）
- 未来 3 天（默认厦门市 / 南平市浦城县，可通过 `WEATHER_CITY_IDS` 配置）的天气（包含体感温度、风速、降水、日出日落等）
- 服务器健康状态（CPU / 负载 / 内存 / 磁盘 / 运行时长）
- Google Calendar 今日日程（全天 + 精确到分钟的事件）

## 快速开始

1. **安装依赖**

   ```bash
   pip install -r requirements.txt
   ```

2. **配置环境变量**

   - 将敏感信息直接注入系统环境（示例）：

     ```bash
     export SERVERCHAN_KEY="SCTxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
     export OPENROUTER_KEY="sk-or-v1-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
     export GOOGLE_SERVICE_ACCOUNT_JSON="/absolute/path/to/service-account.json"
     export GOOGLE_CALENDAR_ID="your_calendar_id@group.calendar.google.com"
     export WEATHER_API_KEY="聚合天气提供的apikey"
     export WEATHER_CITY_IDS='{"厦门市": "3105", "南平市浦城县": "1743"}'
     ```

   - 非敏感调优项已经写入仓库根目录的 `.env`，根据需要直接编辑即可（例如 `OPENROUTER_MAX_CHARS`、`NEWS_SOURCES_FILE` 等）。

   > 脚本会自动读取 `.env`（或 `USEFUL_PUSH_ENV_FILE` 指定的路径）中的非敏感配置，但 `SERVERCHAN_KEY` / `OPENROUTER_KEY` / `GOOGLE_*` 始终以系统环境变量为准。

3. **运行一次，确认推送成功**

   ```bash
   python push_digest.py
   ```

## 定时推送

使用 cron 在每天 7:00 运行（示例为系统 Python3 路径 `/usr/bin/python3`，请根据自己的环境调整）：

```
0 7 * * * cd /home/cwj/code/useful_push && /usr/bin/python3 push_digest.py >> /var/log/useful_push.log 2>&1
```

> 提示：首次运行建议手动执行并观察日志；若需要不同频率/时间，只需调整 cron 表达式。

## 新闻源配置

- 编辑 `news_sources.json` 来增删 RSS / Google News / 站点提要，每条需包含：

  ```json
  { "category": "ai", "label": "Google News - AI 热点", "url": "..." }
  ```

  支持的 `category` 默认为 `ai`、`robotics`、`finance`、`tech`，可自行新增并在 `push_digest.py` 的 `NEWS_CATEGORY_META` 中配置推送标题。
- 选取逻辑：脚本会读取同一分类下的全部源，抓取最近 24 小时内的内容，按发布时间逆序合并、去重后，取最新的 20 条（可在 `NEWS_CATEGORY_META[*].max_items` 中调整），再调用 OpenRouter 翻译 + 摘要。

## 其它说明

- 天气数据改为调用[聚合数据 · 天气预报](https://www.juhe.cn/docs/api/id/73)，需要提前配置 `WEATHER_API_KEY`。城市列表默认包含厦门市（3105）和南平市浦城县（1743），也可通过设置 `WEATHER_CITY_IDS`（JSON 字典形式，如 `{"上海市":"101020100"}`）自定义多个城市。
- 服务器健康数据由 `psutil` 生成；如需额外指标，可在 `push_digest.py` 中扩展。
- 若未配置某项（如 Google Calendar），对应板块会提示“未配置”而不会导致脚本失败。
- 日志使用 `logging` 模块输出到控制台，可通过 `cron` 重定向到文件便于排查。
- OpenRouter 调用默认控制在约 12 次/分钟（更保守以减少 429），收到 429 会自动指数退避重试，最大 5 次。若需要更长原文，可设置 `OPENROUTER_MAX_CHARS`（默认 6000）控制发送给模型的最大字符数。
- 所有 HTTP 请求统一复用带自动重试的会话，遇到网络波动（含 SSL 断开、429、50x 等）会自动重试 3 次。
- 若新闻本身为中文，则跳过 OpenRouter 调用，直接取原文 + 本地摘要；仅对非中文条目调用翻译/摘要，可显著降低 API 次数。若希望强制全部走 OpenRouter，可设置 `OPENROUTER_ALWAYS=1`。

欢迎根据自己的需求扩展更多板块，或修改推送格式。
