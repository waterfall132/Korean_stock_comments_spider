# Naver Finance 股票论坛爬虫与翻译 API

这是一个基于 **FastAPI** 编写的高性能异步后端服务。其主要功能是抓取韩国 Naver Finance (네이버 금융) 股票讨论区的帖子列表及正文内容，支持通过外部 API 进行韩文到中文/英文的实时翻译，并将抓取的数据持久化到 SQLite 数据库中。

## 🚀 核心特性

1. **API 接口化**：提供标准的 RESTful API 获取单页列表、多页区间以及帖子详情。
2. **智能翻译引擎**：集成第三方翻译接口，支持长文本自动分片（Chunking）、限流控制及错误回退（失败时返回原文）。
3. **强大的爬虫容错**：
   * 针对 Naver 详情页的新旧两套架构进行了兼容（包括传统的 DOM 树以及基于 Next.js SSR 的 `__NEXT_DATA__` JSON 数据提取）。
   * 针对网络波动配置了完备的重试机制（Retry Adapter），支持自动拦截并重试 502/504 等错误。
   * 支持通过系统环境变量动态配置 Clash 等代理。
4. **多级缓存机制**：使用 `cachetools` 在内存中对列表页（60秒）、帖子详情（10分钟）、翻译结果（24小时）进行缓存，极大降低目标网站及翻译接口的压力。
5. **本地数据持久化**：内置 SQLite 数据库支持，自动建表与表结构迁移（Auto-Migration），并通过自定义 `dedupe_key` (MD5 Hash) 实现严格的数据去重。

---

## 🛠️ 技术栈

* **Web 框架**: FastAPI, Pydantic, Uvicorn
* **爬虫与解析**: Requests, BeautifulSoup4
* **缓存组件**: Cachetools (TTLCache)
* **数据库**: SQLite3 (内置)

---

## ⚙️ 环境变量配置

系统启动时会自动读取以下环境变量以调整行为：

| 变量名 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `DB_PATH` | `naver_forum.db` | SQLite 数据库文件保存路径 |
| `TRANSLATE_API_URL` | `https://60s.viki.moe/v2/fanyi` | 翻译 API 地址 |
| `TRANSLATE_TIMEOUT` | `15` | 翻译接口的超时时间（秒） |
| `CLASH_HTTP_PROXY` | `(空)` | HTTP/HTTPS 请求代理地址（如 `http://127.0.0.1:7890`） |

---

## 📡 API 接口文档

API 允许跨域（CORS），前端可直接调用。所有请求默认均会自动触发数据库落库（`save_db=True`）。

### 1. 健康检查与配置状态
* **GET** `/api/health`
* **响应**: 返回当前服务的存活状态、数据库路径及代理配置。

### 2. 获取单页帖子列表
* **GET** `/api/posts`
* **参数**:
  * `code` (str): 股票代码，必填（默认 `000660`，即 SK Hynix）。
  * `page` (int): 页码，必填（默认 `1`）。
  * `lang` (str): 翻译目标语言，可选 `ko` (原韩文), `zh` (中文), `en` (英文)。
  * `save_db` (bool): 是否将抓取到的数据写入数据库（默认 `True`）。
* **返回格式**:
  ```json
  {
    "code": "000660",
    "page": 1,
    "lang": "zh",
    "available_pages": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
    "next_group_page": 11,
    "count": 20,
    "posts": [
      {
        "dedupe_key": "hash:xxxxxxxx...",
        "date": "2023.10.25 14:30",
        "title_ko": "원문 제목",
        "title": "翻译后的中文标题",
        "author": "用户名",
        "views": 150,
        "likes": 10,
        "dislikes": 1,
        "comments": 5,
        "nid": "12345678",
        "post_url": "https://..."
      }
    ]
  }
  ```

### 3. 获取多页帖子列表 (区间抓取)
* **GET** `/api/posts/range`
* **参数**:
  * `code` (str): 股票代码。
  * `start_page` (int): 起始页。
  * `end_page` (int): 结束页（单次最多跨度 20 页）。
  * `lang` (str): 语言选择 (`ko`, `zh`, `en`)。
* **说明**: 会对区间内抓取到的所有帖子进行**跨页去重**处理，防止因为翻页瞬间有新帖顶上导致数据重复。

### 4. 获取帖子详情 (正文解析)
* **GET** `/api/post/detail`
* **参数**:
  * `code` (str): 股票代码。
  * `nid` (str): 帖子的唯一标识 ID（可选，与 `post_url` 二选一）。
  * `post_url` (str): 帖子的完整链接（可选）。
  * `lang` (str): 语言选择。
  * `force_refresh` (bool): 是否跳过本地数据库直接重新抓取（默认 `False`）。
* **响应包含**:
  返回帖子的原文内容 (`content_ko`) 与翻译后内容 (`content`)。

---

## 🗄️ 数据库设计 (SQLite)

系统启动时会自动执行 `init_db_and_migrate()`，如果遇到旧版本库，会自动 `ALTER TABLE` 补充字段。

### 1. 列表表 (`forum_posts`)
记录帖子摘要属性。核心字段：
* `code`: 股票代码
* `dedupe_key`: 去重哈希键（由 `code`+`title_ko`+`date` 或 `nid` 组合 MD5 生成）
* `views`, `likes`, `dislikes`, `comments`: 统计数据
* `crawled_at`: 最后爬取时间

### 2. 详情表 (`forum_post_details`)
记录帖子长正文。核心字段：
* `code`, `dedupe_key`, `nid`, `post_url`: 基础关联信息
* `title_ko`: 韩文标题
* `content_ko`: 韩文纯文本正文

*注: 数据库设计上使用 `ON CONFLICT DO UPDATE`（Upsert）策略，保证重复抓取只会更新浏览量等统计数据，不会产生冗余脏数据。*

---

## 💡 核心实现原理解析

### Next.js SSR 深度抓取
现代的 Naver 讨论区将帖子正文嵌入到了由 `m.stock.naver.com` 提供支持的 `iframe` 中，且页面采用了 Next.js 服务器端渲染（SSR）。
本爬虫的 `_fetch_iframe_content` 函数采用降维打击的方式：
1. 直接正则提取 HTML 中的 `<script id="__NEXT_DATA__">` 数据块。
2. 将其转化为 JSON，遍历内部嵌套结构（`dehydratedState.queries...`），直接提取底层纯净的文本数据。
3. 相比于传统的 BeautifulSoup 解析，该方案速度极快且完全免疫前端 DOM 结构的样式变动。

### 长文本翻译分片处理
由于第三方翻译接口使用 `GET` 请求，对 URL 长度有硬性限制。代码中 `_split_text_for_get` 会根据换行符 `\n` 对超过 700 字符的长文本进行智能拆分，循环翻译后再重新拼接，确保不丢失任何上下文。

---

## 🚀 部署与运行

1. 安装依赖：
   ```bash
   pip install fastapi uvicorn requests beautifulsoup4 cachetools pydantic
   ```
2. 运行服务：
   ```bash
   # 默认运行在 127.0.0.1:8000
   uvicorn main:app --reload
   ```
3. 访问交互式接口文档 (Swagger UI):
   打开浏览器访问：`http://127.0.0.1:8000/docs`

> **前端集成提示**：
> 如果在项目根目录下创建一个 `frontend` 文件夹并放入 `index.html`，FastAPI 会自动将其作为静态站点挂载到根路径 `/`。
