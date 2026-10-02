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
   https://github.com/你的用户名/VpnShare.git
   ```
   **把这行复制下来**，下一步要用。

### 第 2 处：命令行登录 —— 第一次 push 时的身份认证

GitHub **早在 2021 年就停用了「账号密码」认证**，所以 push 时密码框里
**填你的登录密码一定会失败**。必须用 **Personal Access Token（PAT）**。

**生成 Token：**

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

**最简单的一次性做法 —— 把 Token 塞进远程地址（不用记，也不用配凭据管理器）：**

```bash
git remote add origin https://你的用户名:ghp_你的Token@github.com/你的用户名/VpnShare.git
git branch -M main
git push -u origin main
```

这样 push 直接成功，不会再弹登录框。
（代价：Token 明文存在 `.git/config` 里，自用机器无所谓；介意的话看下面「备选」。）

**备选 —— 让 Windows 记住凭据：**

```bash
git remote add origin https://github.com/你的用户名/VpnShare.git
git branch -M main
git push -u origin main
```

第一次 push 会弹 Windows 凭据窗口 / 浏览器授权页：
- 用户名：你的 GitHub 用户名
- **密码：粘贴上面那个 Token**（不是登录密码）

之后 Windows 凭据管理器会记住，再 push 就不用输了。

> 如果报错 `remote: Support for password authentication was removed...`，
> 说明你填的是登录密码而不是 Token，重来一遍即可。

---

## 完整命令（复制粘贴版）

把 `你的用户名` 和 `ghp_你的Token` 换成实际值：

```bash
cd C:/Users/ycylg/WorkBuddy/2026-09-30-18-49-11/vpn-share

git remote add origin https://你的用户名:ghp_你的Token@github.com/你的用户名/VpnShare.git
git branch -M main
git push -u origin main
```

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

## 提交身份（可选）

首次 commit 用的是仓库级占位身份。想换成你自己的名字：

```bash
git config user.name  "你的名字"
git config user.email "你的邮箱"
git commit --amend --reset-author --no-edit
git push -f origin main
```

> `--amend` 会改写历史，只在**还没人 clone 你的仓库**时这么做才安全。

---

## 常见问题

**Q：push 报 `Authentication failed`**
A：密码处填的是登录密码。必须用 PAT（见「第 2 处」）。

**Q：push 报 `failed to push some refs`**
A：建仓库时勾了 README / .gitignore / LICENSE。去网页把仓库删了重建，三个勾都不打。

**Q：push 太慢 / 卡住**
A：总大小约 16.5 MB（exe 11.5 MB + apk 4.6 MB），正常应该几十秒。
一直卡就换 SSH 或挂代理。

**Q：APK 装不上，提示「未知来源」**
A：手机设置里允许「来自此来源的应用」，见 `docs/使用手册.md`。
