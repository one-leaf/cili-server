# cili-server

组织管理服务端。当前已落地短信切片：配置加载、腾讯云 SendSms provider、mock provider 与测试。

## 短信配置

仓库只提交 `config.example.json`，真实 `config.json` 放在 `data/config/` 下（整个 `data/` 不进 Git，部署/容器化时整体挂载）。

`data/` 结构：

```text
data/
├─ config/config.json      # 服务配置
├─ db/cili-server.db       # SQLite（WAL 还有 -shm/-wal 两个文件）
├─ logs/access.log         # 访问日志（10MB 轮转，保留 5 份）
└─ workspaces/<slug>.git   # 共享仓库裸库
```

```bash
mkdir -p data/config
cp config.example.json data/config/config.json
```

腾讯云配置：

```json
{
  "sms": {
    "provider": "tencent",
    "tencent": {
      "secret_id": "你的 SecretId",
      "secret_key": "你的 SecretKey",
      "sdk_app_id": "短信应用 SdkAppId",
      "sign_name": "短信签名",
      "template_id": "模板 ID",
      "region": "ap-guangzhou",
      "timeout_seconds": 10,
      "retries": 0
    }
  }
}
```

本地联调可把 `provider` 改成 `mock`，验证码会打印到日志，不实际发送。

密钥也可以走环境变量（优先于配置文件）：

- `CILI_SMS_PROVIDER`
- `CILI_SMS_TENCENT_SECRET_ID`
- `CILI_SMS_TENCENT_SECRET_KEY`
- `CILI_SMS_TENCENT_SDK_APP_ID`
- `CILI_SMS_TENCENT_SIGN_NAME`
- `CILI_SMS_TENCENT_TEMPLATE_ID`
- `CILI_SMS_TENCENT_REGION`
- `CILI_SMS_CODE_PEPPER`（验证码 HMAC 密钥；不填时 tencent 模式回退用 SecretKey）
- `CILI_SERVER_DB`（SQLite 路径，覆盖 `database.path`）
- `CILI_TRUST_FORWARDED_FOR=1`（仅反代部署时开启，取 `X-Forwarded-For` 第一段作为客户端 IP）
- `CILI_ADMIN_USERNAME` / `CILI_ADMIN_PASSWORD`（首次启动创建管理员；密码至少 8 位）
- `CILI_ADMIN_COOKIE_SECURE=1`（HTTPS 部署时开启管理端 Cookie Secure）
- `CILI_GIT_ROOT`（本地裸仓库目录，覆盖 `git.root`）

`config.json` 路径可用 `CILI_SERVER_CONFIG` 覆盖。

## 使用

```python
from app.settings import load_settings
from app.sms import build_provider

settings = load_settings()
provider = build_provider(settings)
provider.send("13800000000", "123456")
```

- `tencent`：POST `https://sms.tencentcloudapi.com/`，TC3-HMAC-SHA256 签名，逐号码检查 `SendStatusSet[*].Code == "Ok"`。
- `mock`：只记录到 `provider.sent` 并打印日志，仅供开发。

## 验证码服务

`app/sms/verification.py` 提供 `SmsVerificationService`：

- 6 位数字验证码，使用 `secrets` 生成；
- 只存 HMAC 哈希，不存明文；
- 5 分钟过期、最多 5 次尝试、成功即删除；
- 同一手机号 60 秒冷却、每小时 5 次、每天 10 次；同一 IP 每小时 30 次；
- 限流与验证码状态落 SQLite（`app/db.py` 的 `sms_codes`、`sms_send_events`）；
- provider 网络调用在数据库事务之外，发送失败会清理本次验证码与发送记录。

初始化：

```python
from app.db import connect, init_schema
from app.settings import load_settings
from app.sms import build_provider
from app.sms.verification import SmsVerificationService

conn = connect("data/cili-server.db")
init_schema(conn)
settings = load_settings()
service = SmsVerificationService(conn, build_provider(settings), settings.verification)

service.send_code("13800000000", ip="127.0.0.1")
service.verify_code("13800000000", "123456")
```

## 启动

```bash
pip install -r requirements.txt
mkdir -p data/config
cp config.example.json data/config/config.json   # 填腾讯云配置；本地可把 provider 改成 mock
uvicorn app.main:app --host 127.0.0.1 --port 7681
```

接口：

- `GET /api/v1/health`
- `GET /api/v1/public/orgs`：无需认证，返回 `{orgs: [{id, name}]}`；`id` 是组织 slug（客户端用 `org-<slug>` 命名模型 provider）。
- `POST /api/v1/auth/sms/send`：body `{org_id, phone}`，`org_id` 取 public/orgs 返回的 slug（也兼容内部 id），成功返回 `{ok, cooldown_seconds}`；组织不存在返回 404 `org_not_found`，手机号未登记返回 404 `phone_not_registered`（detail：找不到用户，请输入正确的手机号），人员已停用返回 403 `person_disabled`。
- `POST /api/v1/auth/sms/verify`：body `{org_id, phone, code, device_name}`；校验通过后签发 30 天滑动设备令牌，返回 `{token, expires_at, org, user, config_version}`，其中 `org.id` 为 slug，`user.groups` 为群组名列表。
- `GET /api/v1/auth/me`：`Authorization: Bearer <token>`，校验令牌并滑动续期。
- `POST /api/v1/auth/logout`：`Authorization: Bearer <token>`，吊销令牌。
- `GET /api/v1/config`：`Authorization: Bearer <token>`；`org` 含 `id`（slug）、`name`、`web_url`，`user` 含本人的 `groups`；`model` 来自组织级模型设置，注入 `provider_key`（直接使用企业名，空白和 `/` 替换为 `-`，企业名为空时回退 slug）和该人员的 `api_key`；`workspaces` 是人员所在群组绑定的仓库，每条带 `slug`（= 仓库 slug，客户端用作目录/工作区 slug），`git` 块补齐 `remote_url`（`web_url` + `/git/<slug>.git`）、`branch`、`username`（人员中文名）、`password`（当前设备令牌）、`sync_mode`（`remote_wins`）。支持 `If-None-Match`（ETag = 组织 config_version + 人员 updated_at），命中返回 304。组织未配置模型时返回 409 `org_model_not_configured`。
- 错误统一为 `{"error": "<code>", "detail": "<说明>"}`；限流额外带 `retry_after`。

设备令牌只在 verify 响应中出现一次；数据库只存 SHA-256 哈希，30 天有效，每次 `/me` 或配置拉取时续期。

配置下发规则（`app/services/delivery.py`）：

- 模型：组织级一套，所有人员共用；API Key 按人员（`people.model_api_key`）注入；
- 工作区：每个仓库绑定一个群组；人员属于哪个群组，就下发该群组绑定的仓库。需要跨群访问时，把人员加入对应群组即可；
- 仓库创建/改名/删除、仓库群组变更、人员与群组变更都会推进组织 `config_version`。

管理端页面：`http://<host>:7681/admin`（登录后可管理人员/群组/模型设置/仓库/设备/审计）。

「设置 → 模型设置」维护组织共享模型：提供商名称、协议（anthropic / openai / openai-responses）、认证方式、服务商预设、Base URL、API 路径、Headers，以及多个模型条目（别名、API 模型 ID、上下文、最大输出、类型、输入模态、思考档位、缓存策略）；服务端校验字段（至少一个模型、上下文/最大输出为正整数、模态必须含 text），不下发角色绑定。`api_key` 仍按人员下发，不在模型设置里填。

管理端 API（`/api/v1/admin/*`，Cookie 会话，12 小时过期）：

- `POST /login` / `POST /logout` / `GET /me`
- `POST /account`：修改管理员账号名/密码（需提供当前密码；新密码至少 8 位，账号名不能重复；成功后该管理员的全部会话失效）
- `GET /orgs`、`PATCH /orgs/{id}`：修改企业名称和对外 Web 地址（两者都会随 `/api/v1/config` 下发并推进 config_version）
- `GET/PUT/DELETE /orgs/{id}/sms`：组织级腾讯云短信配置（SecretId/SecretKey/SdkAppId/签名/模板/Region），保存后即时生效；响应只返回 SecretId 掩码与 `has_secret_key`，不返回密钥明文
- `GET /backup`：下载 SQLite 在线快照（`.db`，含组织/人员/仓库授权/设备/审计与短信密钥，请妥善保管）；`POST /restore`：上传 `.db` 覆盖当前数据库，恢复前自动备份到 `data/db/backups/`，即时生效（设备与会话以备份为准，恢复后需重新登录）
- `GET/POST /people`、`PATCH/DELETE /people/{id}`
- `GET/POST /groups`、`PATCH/DELETE /groups/{id}`、成员增删
- `GET/PUT /orgs/{id}/model`：组织级共享模型设置（所有人共用；不含人员 Key）
- `GET /devices`、`POST /devices/{id}/revoke`
- `GET /audit?org_id=&limit=&before_id=`：管理端审计日志（登录、人员/群组/模型设置/设备/仓库变更）
- `GET/POST /repos`、`PUT /repos/{id}/group`（设置所属群组，一对一）、`DELETE /repos/{id}`；仓库所属群组成员获得读写权限并下发对应工作区。

## Git 网关

只支持本地裸仓库，位于 `git.root`（默认 `data/workspaces/`），每个仓库 `data/workspaces/<slug>.git`。

客户端地址：`http://<host>/git/<slug>.git`，用户名任意，密码 = 设备令牌。

支持的 Smart HTTP 入口：

- `GET /git/<slug>.git/info/refs?service=git-upload-pack|git-receive-pack`
- `POST /git/<slug>.git/git-upload-pack`
- `POST /git/<slug>.git/git-receive-pack`

校验链：Basic 令牌 → 设备未吊销/未过期 → 人员启用 → 仓库群组授权（人员属于仓库所属群组）→ `git http-backend`。授权即读写，没有只读模式。

仓库创建时会 `git init --bare` 并写入初始提交（`.gitignore` 为空，即 **`.cili/` 也会参与同步**），同时启用 `http.receivepack`。

共享工作区的 `.cili/`（会话、记忆、MCP、workflow、cron、tools、敏感词规则）默认全部纳入 Git，仅 `.cili/tmp/`、`.cili/cache/`、`.cili/runtime/` 保留本机；服务启动时会把已有仓库里的 `.cili` 忽略规则移除并提交一次。注意远程权威模式下多人同时写会话/记忆会被远程覆盖，且这些数据对同群组成员可见。

管理端变更需要同源（`Origin`/`Referer` 与 Host 一致）；没有 Origin 的脚本/curl 请求放行。

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

## 安全约定

- 腾讯云 SecretId / SecretKey 可在管理端「设置 → 短信（腾讯云）」按组织配置（存数据库，保存后即时生效；接口只返回掩码，审计与访问日志不记录）；未配置时回退 `config.json` 或环境变量。组织配置优先于配置文件/环境变量。
- 验证码模板需在腾讯云控制台创建并审核通过；服务端固定把 6 位验证码作为模板参数 `{1}`（`TemplateParamSet.0`）发送，`SignName` 必须与模板签名一致。`SdkAppId` 兼容 aiway 配置里的 `sms_sdk_app_id` 写法。
- `config.json` 位于 `data/config/`（整个 `data/` 已 gitignore）；不要把它提交到 Git。
- 人员模型 API Key 随 `/api/v1/config` 下发给本人设备；管理端（需管理员登录）会明文返回以便核对，审计与访问日志不记录 Key。
- 日志不打印验证码（`mock` provider 例外，仅本地）。
- `SendSms` 的 `retries` 默认 0，避免网络超时重试导致重复短信。
