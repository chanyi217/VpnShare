# 发布到 GitHub —— 操作步骤

本地仓库已经建好、首次 commit 已经做完。剩下只有**两步需要你亲自登录 GitHub**，
其余都是复制粘贴命令。

---

## 需要登录 GitHub 的地方（一共 2 处）

### 第 1 处：网页登录 —— 创建空仓库

1. 浏览器打开 <https://github.com>，登录你的账号（没有账号就先注册）。
2. 右上角 `+` → **New repository**。
3. 填写：
   - **Repository name**：`VpnShare`（或你喜欢的名字）
   - **Description**：`把手机 VPN 共享给局域网 / USB 直连的电脑`
   - **Public / Private**：建议 **Public**（方便别人下载 APK）；选 Private 也行，只是别人看不到。
   - ⚠️ **下面三个勾一个都不要打**：
     - `Add a README file`
     - `Add .gitignore`
     - `Choose a license`

   因为本地已经有 README / .gitignore / LICENSE 了，勾了会冲突，push 会被拒。
4. 点 **Create repository**。
5. 创建完页面会显示一个地址，形如：
```
https://github.com/chanyi217/VpnShare.git
```
   **把这行复制下来**，下一步要用。

### 第 2 处：第一次 push —— 弹浏览器登录

这台机器装了 **Git Credential Manager 2.9.0**（随 PortableGit 带的），所以不需要手动生成 Token：
push 的时候会自动**弹一个 GitHub 授权网页**，在网页里用 `chanyi217` + 密码登录、点 Authorize 就行。
登录一次之后 Windows 会记住，以后 push 不再问。

```bash
git remote add origin https://github.com/chanyi217/VpnShare.git
git branch -M main
git push -u origin main
```

执行后会发生什么：

```
> git push -u origin main
# 弹出浏览器 → GitHub 登录页
#   用户名: chanyi217
#   密码:   你的 GitHub 登录密码
# 登录后点绿色的 "Authorize GitCredentialManager"
# 终端显示 Writing objects: 100% ... done
```

> ⚠️ **别在终端的黑框里输密码。** 真正的 Git push **不支持**账号密码直连
> （GitHub 2021 年就停用了），在命令行 paste 密码会报：
> `remote: Support for password authentication was removed`。
> 只有**浏览器那个网页**能用密码，或者用下面的 Token 兜底。

---

## 兜底方案 B：手动生成 Token

只在**浏览器没弹出来**时才用这条路（比如远程桌面、无图形会话）。

### 生成 Token

1. 登录 github.com → 右上角头像 → **Settings**
2. 左侧最下面 → **Developer settings**
3. → **Personal access tokens** → **Tokens (classic)**
4. → **Generate new token (classic)**
5. 填写：
   - **Note**：`VpnShare push`
   - **Expiration**：`90 days`（或 No expiration）
   - **勾选 `repo`**（这一项就够，它包含 push 权限）
6. 点 **Generate token**
7. ⚠️ **Token 只显示这一次**，立刻复制保存（形如 `ghp_xxxxxxxxxxxxxxxxxxxx`）

### 用法一：塞进远程地址（免交互，推荐自用）

```bash
git remote add origin https://chanyi217:ghp_你的Token@github.com/chanyi217/VpnShare.git
git branch -M main
git push -u origin main
```

push 直接成功，不再弹任何框。
代价：Token 明文存在 `.git/config` 里 —— 自用机器无所谓，但**这个文件绝不能外传**。

### 用法二：让凭据管理器记住

```bash
git remote add origin https://github.com/chanyi217/VpnShare.git
git branch -M main
git push -u origin main
```

弹凭据窗口时：用户名填 `chanyi217`，**密码栏粘贴 Token**（不是登录密码）。
之后 Windows 凭据管理器会记住，再 push 就不用输了。

---

## 完整命令（复制粘贴版）

```bash
cd <你的项目路径>/vpn-share

git remote add origin https://github.com/chanyi217/VpnShare.git
git branch -M main
git push -u origin main
```

终端跑完这三行 → 浏览器自动弹出 → 用 `chanyi217` + 密码登录 → 点 Authorize → 完事。

push 完刷新仓库页面，应该能看到 README 渲染出来的首页。

---

## 可选：发一个 Release（让别人好下载）

网页上：仓库首页右侧 **Releases** → **Create a new release**
- **Tag version**：`v1.2`
- **Release title**：`VpnShare v1.2 / PC v3.4`
- **Attach binaries**：把这两个拖进去
  - `apk/VpnShare-v1.2.apk`
  - `pc-gui/release/VpnShare.exe`
- **描述**用下面这段：

```markdown
## 下载

| 文件 | 说明 | 大小 |
|------|------|------|
| `VpnShare-v1.2.apk` | 手机端（Android 8.0+），装到开 VPN 的那台手机 | 4.6 MB |
| `VpnShare.exe` | 电脑端（Windows 10/11），免安装双击即用 | 11.5 MB |

## 这次更新了什么

- **两端都加了实时流量折线图**：手机端和电脑端都能看到上/下行速率曲线
  （60 秒滑动窗口、Y 轴自动量程、绿色下载 / 蓝色上传）
- 电脑端 v3.4：本机中转链路加上了双向字节统计

## 它是干嘛的

手机开 VPN，电脑没梯子。这个方案让电脑通过 Wi-Fi 或 USB 走手机的网络出口。

- **手机端**：在手机上起 SOCKS5 / HTTP 代理（前台服务保活）
- **电脑端**：把 Windows 系统代理指到手机，或者 USB 直连

三种连法：Wi-Fi 热点 / 同一局域网 / USB 直连（安卓 8.0+ 手机可以直接开 USB 反向共享网络）。

## ⚠️ 一个关键前提：fake-IP

很多 VPN App 用 **fake-IP 模式**（DNS 一律返回 `198.18.x.x` 假地址）。
假地址只在手机 VPN 内部有意义，电脑拿到它连不出去，表现是：
**浏览器 `ERR_CONNECTION_CLOSED`，但直接 ping 真实 IP 却是通的。**

本项目的手机端内置了 **DoH（DNS over HTTPS，走 443 端口）** 来拿真实 IP，
绕过被劫持的 53 端口 DNS，所以能正常用。

判据：
```bash
curl https://1.1.1.1          # 通   → 链路没问题，是 DNS 被劫持
curl https://www.google.com   # 不通 → 确认是 fake-IP
```

## 依赖与参考

见仓库内 `THIRD-PARTY-NOTICES.md`。
一句话总结：**运行期零第三方依赖**（手机端只用 Android SDK，电脑端只用 Python 标准库），
第三方库只出现在构建阶段（Gradle / PyInstaller）。
```

---

## 提交身份

已经配好了，不用管：

```
user.name  = chanyi217
user.email = chanyi217@users.noreply.github.com
```

> **为什么用 noreply 邮箱**：仓库一旦 Public，commit 里的邮箱会被**永久公开**
> （GitHub 网页、git log、clone 都能看到），爬虫专门抓这个发垃圾邮件。
> noreply 地址同样能关联到 GitHub 账号（头像、主页跳转都在），但不暴露真实邮箱。
>
> 配套设置：GitHub → Settings → **Emails** → 勾上 `Keep my email addresses private`。
>
> 注意：这个身份保存在**本地 Git 配置**里，不会随仓库上传；
> 但**写进 commit 的邮箱会**公开，所以要在 push 之前定好用哪个。

## 想加协作者 / 换电脑

```bash
git clone https://github.com/chanyi217/VpnShare.git
```

仓库里**没有** keystore 和构建缓存，clone 下来直接 `./gradlew assembleRelease` 会退回 debug 签名，
自用没问题。要正式签名参见 `android-app/README.md`。

---

## 常见问题

**Q：push 报 `Support for password authentication was removed`**
A：说明密码是被敲进**终端**的，而不是在浏览器网页里输的。Git 协议不支持密码，
只在网页 OAuth 登录时用。先清掉缓存的凭据再重试：

```bash
git credential-manager github logout   # 或 cmdkey /delete:git:https://github.com
git push -u origin main                # 重来，这次在弹出来的网页里登
```

**Q：push 报 `Authentication failed`**
A：浏览器弹窗里登录错了账号，或者没点 Authorize。按上面 logout 后重来。
实在不行走「兜底方案 B」手动生成 Token。

**Q：push 报 `failed to push some refs`**
A：建仓库时勾了 README / .gitignore / LICENSE。去网页把仓库删了重建，三个勾都不打。

**Q：push 太慢 / 卡住**
A：总大小约 16.5 MB（exe 11.5 MB + apk 4.6 MB），正常应该几十秒。
一直卡就换 SSH 或挂代理。

**Q：APK 装不上，提示「未知来源」**
A：手机设置里允许「来自此来源的应用」，见 `docs/使用手册.md`。
