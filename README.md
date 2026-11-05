# Clash Subscription Panel

自托管的 Clash YAML 订阅分发和管理面板。每个订阅者拥有独立令牌和 UUID，链接采用以下格式。

```text
https://subscriptions.example.com/<token>/<email-local-part>.yaml
```

## 功能

- 新增、编辑、启用和停用订阅，导出包含邮箱、UUID、链接和状态的 TSV。
- 在线编辑 `{{UUID}}` 模板，校验后原子切换整批配置。
- 自定义令牌支持 32～255 个 URL 安全字符，区分大小写，默认生成 64 位随机十六进制令牌。
- 每页最多 100 条订阅，搜索覆盖全部订阅。
- 记录最近访问时间，按完整 User-Agent 去重，支持弹窗分页、搜索和逐条删除。
- 更换令牌后保留该订阅的请求历史。
- Nginx 提供 HTTPS、管理员 Basic Auth 和可直接在浏览器查看的 YAML。
- 应用通过 Unix socket 通信。数据和运行目录使用绑定挂载。

## 镜像与更新

`ghcr.io/shigalin/clash-subscription-panel:latest`，当前构建平台为 `linux/amd64`。

推送到 `main` 或 `v*` 标签时，GitHub Actions 构建镜像，发布 `latest`、提交 SHA 和分支或版本标签。

在 VPS 的实例目录执行以下命令。

```sh
docker compose pull
docker compose up -d --wait --wait-timeout 60
```

需要锁定版本时，把 `.env` 中的 `IMAGE` 指向版本标签或 `ghcr.io/...@sha256:...`。更新前备份数据目录，回滚时切换回原镜像引用并重新执行 `docker compose up -d --wait`。

## 首次部署

需要已有的 Docker Compose、Nginx 和域名证书。

1. 克隆项目并准备实例配置。

   ```sh
   git clone https://github.com/shigalin/clash-subscription-panel.git
   cd clash-subscription-panel
   cp .env.example .env
   chmod 600 .env
   ```

2. 修改 `.env`，设置域名、绝对数据路径和运行路径。新实例可使用 UID/GID 10001，宿主机的 Nginx 工作组通常为 33，先用 `id`、`getent group www-data` 核对。

   按实际路径创建目录。

   ```sh
   install -d -o 10001 -g 33 -m 750 /opt/clash-subscriptions/data
   install -d -o 10001 -g 33 -m 750 /opt/clash-subscriptions/runtime
   ```

3. 将带 `{{UUID}}` 的模板保存为私密的 `imports/template.yaml`，将名单保存为 `imports/users.csv`。CSV 表头为 `email,uuid` 或 `email,uuid,token`，目录名使用邮箱 `@` 前的部分加 `.yaml`，令牌必须唯一，同名文件和相同 UUID 可以拥有多条独立订阅。

   `imports` 和文件应由实例运行用户拥有，目录权限 700，文件权限 600。

   ```sh
   docker compose pull
   docker compose run --rm -v "$PWD/imports:/imports:ro" panel python /app/manage.py      --users /imports/users.csv --template /imports/template.yaml
   docker compose up -d --wait --wait-timeout 60
   ```

4. 在宿主机创建独立的管理员口令文件。目录应允许 Nginx 工作组遍历，口令文件为 root:www-data、640，证书私钥为 600。例如使用 `openssl passwd -6` 交互生成口令哈希，并保存成 `admin:<hash>` 格式。口令文件应位于版本库和镜像之外。

5. 生成 Nginx 站点配置，参数使用该实例的实际路径。

   ```sh
   python3 deploy/render-nginx.py      --domain subscriptions.example.com      --data-dir /opt/clash-subscriptions/data      --runtime-dir /opt/clash-subscriptions/runtime      --cert-file /etc/nginx/subscriptions/fullchain.pem      --key-file /etc/nginx/subscriptions/privkey.pem      --auth-file /etc/nginx/subscriptions/admin.htpasswd      > /etc/nginx/conf.d/subscriptions.conf
   nginx -t
   systemctl reload nginx
   ```

管理入口为 `https://subscriptions.example.com/panel/`。证书续期由宿主机的证书工具负责，续期后重载 Nginx。

## 多个域名

每个域名使用独立的 Compose 项目名、`.env`、数据目录、运行目录、证书和管理员口令文件。同一镜像可以运行多个实例，各自维护模板、邮箱、UUID、令牌和 UA 数据。

为第二套实例复制 `compose.yaml` 和 `.env.example` 到新目录，设置不同的 `COMPOSE_PROJECT_NAME`，再按首次部署步骤初始化。第二个站点的 Nginx 配置必须指向第二套数据与 socket 路径。

## 旧 Gist 订阅迁移

初始化工具同时支持表头为 `Gist ID,UUID,备注,哈希值` 的 CSV。此格式将 Gist ID 原样作为订阅令牌，`备注` 必须填写原来的 YAML 文件名。相同文件名和 UUID 的多条记录按独立订阅 ID 管理。

渲染该站点的 Nginx 配置时增加 `--legacy-owner YOUR_GITHUB_USERNAME`，即可同时接受 `/<token>/raw/<filename>` 和 `/<owner>/<token>/raw/<filename>`。面板生成的标准地址仍为 `/<token>/<filename>`。

## 现有数据迁移

数据格式与原 Python/systemd 版本兼容。停止旧应用后，直接把旧的 `store` 目录作为 `DATA_DIR` 挂载，使用原运行用户的数字 UID/GID，Nginx 的静态文件路径继续指向该目录。

切换步骤为备份数据、启动容器并通过健康检查、更新 Nginx 的 socket 路径、验证订阅和面板，最后停用旧应用的自启动。旧程序与数据备份可用于迁移回退。

## 数据与访问

数据目录包含模板、用户凭据、生成的 YAML 和 SQLite UA 记录。请将其作为私密数据备份。镜像仅打包应用程序和依赖。

删除 UA 记录后，相同 UA 后续再次请求时会重新记录。订阅链接本身是访问凭据，完整链接应只分发给对应用户。
