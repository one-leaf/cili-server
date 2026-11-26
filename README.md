# cili-server

组织管理服务端。当前已落地短信切片：配置加载、腾讯云 SendSms provider、mock provider 与测试。

## 短信配置

仓库只提交 `config.example.json`，真实 `config.json` 不进 Git。

```bash
cp config.example.json config.json
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
cp config.example.json config.json   # 填腾讯云配置；本地可把 provider 改成 mock
uvicorn app.main:app --host 127.0.0.1 --port 7681
```

接口：

- `GET /api/v1/health`
- `POST /api/v1/auth/sms/send`：body `{org_id, phone}`，返回 `{ok, cooldown_seconds}`；组织不存在、手机号未登记/停用也返回同样的 ok，且不实际发送。
- `POST /api/v1/auth/sms/verify`：body `{org_id, phone, code, device_name}`；校验通过后签发 30 天滑动设备令牌，返回 `{token, expires_at, org, user, config_version}`。
- `GET /api/v1/auth/me`：`Authorization: Bearer <token>`，校验令牌并滑动续期。
- `POST /api/v1/auth/logout`：`Authorization: Bearer <token>`，吊销令牌。
- `GET /api/v1/config`：`Authorization: Bearer <token>`；支持 `If-None-Match`（ETag = 组织 config_version + 人员 updated_at），命中返回 304。

设备令牌只在 verify 响应中出现一次；数据库只存 SHA-256 哈希，30 天有效，每次 `/me` 或配置拉取时续期。

配置包解析（`app/services/bundles.py`）：

- 模型：个人包 > 群组包（按绑定/包优先级）> 默认包，第一个有 `model_json` 的胜出；
- 工作区：只来自群组包，按 `key` 并集，同 key 高优先级覆盖，再按 `group_id` 排序；
- 群组成员变化、Bundle 创建/更新/绑定都会推进组织 `config_version`。

管理端页面：`http://<host>:7681/admin`（登录后可管理人员/群组/配置包/仓库/设备/审计）。

管理端 API（`/api/v1/admin/*`，Cookie 会话，12 小时过期）：

- `POST /login` / `POST /logout` / `GET /me`
- `GET /orgs`
- `GET/POST /people`、`PATCH/DELETE /people/{id}`
- `GET/POST /groups`、`PATCH/DELETE /groups/{id}`、成员增删
- `GET/POST /bundles`、`PATCH/DELETE /bundles/{id}`、`POST /bundles/{id}/bind`、`GET /bindings`、`DELETE /bindings/{id}`
- `GET /devices`、`POST /devices/{id}/revoke`
- `GET /audit?org_id=&limit=&before_id=`：管理端审计日志（登录、人员/群组/配置包/设备/仓库变更）
- `GET/POST /repos`、`DELETE /repos/{id}`、仓库群组 ACL 增删

## Git 网关

本地裸仓库在 `git.root`（默认 `data/git/`），每个仓库 `data/git/<slug>.git`。

客户端地址：`http://<host>/git/<slug>.git`，用户名任意，密码 = 设备令牌。

支持的 Smart HTTP 入口：

- `GET /git/<slug>.git/info/refs?service=git-upload-pack|git-receive-pack`
- `POST /git/<slug>.git/git-upload-pack`
- `POST /git/<slug>.git/git-receive-pack`

校验链：Basic 令牌 → 设备未吊销/未过期 → 人员启用 → 仓库群组 ACL（读/写）→ `git http-backend`。只读 ACL 允许 clone/fetch，拒绝 push。

仓库创建时会 `git init --bare` 并写入初始提交（`.gitignore` 含 `.cili/`），同时启用 `http.receivepack`。

也支持 `backend=forward`：在 `POST /api/v1/admin/repos` 或 `PATCH /repos/{id}` 里提供 `upstream_url`、`upstream_username`、`upstream_password`，网关会把请求（含 pack）流式转发到 GitLab/Gitea，并用服务账号凭证替换 Authorization。管理端返回 `has_upstream_password`，不回传密码。

管理端变更需要同源（`Origin`/`Referer` 与 Host 一致）；没有 Origin 的脚本/curl 请求放行。

本地还没有管理端页面；测试/联调可直接用上面的 API 或 `app.services.*` 写入数据。

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

## 安全约定

- SecretId / SecretKey 只来自 `config.json` 或环境变量，不进数据库、不进管理端、不出现在 API 响应。
- `config.json` 已在 `.gitignore`；不要把它提交到 Git。
- 日志不打印验证码（`mock` provider 例外，仅本地）。
- `SendSms` 的 `retries` 默认 0，避免网络超时重试导致重复短信。
