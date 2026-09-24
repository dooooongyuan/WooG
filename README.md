# WoG 宝玉助手

这是 WoG 宝玉助手的开源代码，包含游戏连接、合成与仓库自动化、经济统计、Steam 市场查价和可选的共享价格缓存服务。

公开仓库没有包含个人邮箱授权码、服务器地址、服务器 token、Steam Cookie、价格数据库、本地运行记录或打包产物。共享价格服务地址通过 `WOG_PRICE_SERVICE_URL` 配置；留空时助手只使用本机 Steam 查价和本地缓存。

## 目录

| 文件或目录 | 用途 |
| --- | --- |
| `src/宝玉面板.py` | Tk 面板、连接状态、统计/经济页、合成调度、仓库调度、价格缓存和邮件设置。 |
| `src/宝玉助手.py` | 游戏 WebSocket/Inspector 连接、Puerts JavaScript 查询、合成、入库和仓库整理流程。 |
| `tools/修复宝玉助手连接.py` | 检查、备份、应用和恢复 10998 端口补丁；带哈希校验的备份恢复机制。 |
| `tools/10998端口补丁窗口.py` | 10998 端口补丁的图形化启动窗口。 |
| `server/wog_price_service.py` | 可选的共享价格缓存服务；服务端只处理客户端上传和缓存读取，凭据通过环境变量提供。 |
| `config/宝玉助手配置.json.example` | 新用户默认配置示例，自动合成、自动入库、自动整理和邮件推送默认关闭。 |
| `config/宝玉助手价格服务.env.example` | 共享价格服务地址和 token 文件的配置说明。 |
| `tests/test_panel_runtime.py` | 面板运行逻辑、统计归类、合成/掉落隔离和价格刷新回归测试。 |
| `tests/test_price_service.py` | 价格服务请求校验、缓存和限流逻辑测试。 |
| `tests/test_port_patch.py` | 10998 补丁的备份、恢复和文件校验测试。 |
| `requirements.txt` | Python 运行依赖。 |
| `.gitignore` | 排除 token、Cookie、数据库、本地记录和构建产物。 |

## 本地运行

```bash
python -m pip install -r requirements.txt
python src/宝玉面板.py
```

将 `config/宝玉助手配置.json.example` 复制为运行目录中的 `宝玉助手配置.json` 后再按需修改。不要把 QQ 授权码写入 Git。

共享价格缓存是可选项。需要启用时，在运行环境设置：

```text
WOG_PRICE_SERVICE_URL=https://your-price-service.example/wog-prices
WOG_PRICE_SERVICE_TOKEN=your-token-from-the-service
```

token 通过 `WOG_PRICE_SERVICE_TOKEN` 环境变量或运行目录中被 `.gitignore` 排除的 `宝玉助手价格服务.token` 文件提供，不要提交到仓库。没有共享服务时，助手会使用本机 Steam 市场 JSON 搜索接口，并保留本地价格缓存和请求间隔。

## 服务端运行

```bash
WOG_PRICE_DATA_DIR=./wog-price-data python server/wog_price_service.py
```

生产环境请通过环境变量设置 token、数据库目录和 Cookie；不要把这些值写进源码或提交记录。服务端应放在反向代理后，并使用 HTTPS、访问认证和防火墙限制。

## 测试

```bash
python -m unittest discover -s tests -p 'test_*.py'
```

## 安全说明

- 不要提交 `*.token`、`*.token.enc`、Steam Cookie、QQ 授权码、SQLite 数据库或本地统计文件。
- 不要把生产服务器域名、IP、SSH 密钥或反向代理配置写进源码。
- 如果凭据曾经出现在公开 Git 历史中，应立即撤销并重新生成；仅删除工作区文件不能清除 Git 历史。
- 10998 补丁只应对自己的游戏文件使用，并保留工具生成的备份。
