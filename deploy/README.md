# 部署到阿里云（或其他云）的服务器

这份文档只解决一件事：**让这个项目在一个公网地址上长期活着**。
如果你的目的只是给身边几个人用，用 `run_lan.py` 或者发压缩包更省事，别上服务器。

---

## 一、先选机器，别选错档

| 档位 | 拿到什么 | 能不能绑域名 | 适合 |
|---|---|---|---|
| 个人免费试用（300 元额度） | 2核2G，**最长 3 个月** | **不能备案** | 短期演示、跑通流程 |
| 学生认证（学信网） | 300 元券 + 1核2G 最长 **12 个月** | 看实例类型 | 常年挂着 |
| 99 元/年 经济型 e | 2核2G 包年 | 可以 | 要放简历链接 |

**关键限制（官方规则里写着的）**：免费试用是**按量付费**实例，
**不支持 ICP 备案，不能用于内地公开访问的正式站点**。也就是说免费那三个月
你只能 `http://IP:8000` 这样访问——没有域名、没有 HTTPS，链接也没法放进简历。

所以：**免费 3 个月适合「先跑通」，不适合「当作品集链接长期挂着」。**

---

## 二、创建实例时只需要注意四件事

1. 镜像选 **Ubuntu 22.04 LTS**（别选 CentOS，apt 源更好处理）
2. 规格 **2核2G** 够用。重活都在模型 API 那边，这台机器只做解析和调度
3. **一定要分配公网 IP**，否则装完连不上
4. 安全组入方向只开 **22（SSH）**；走 Caddy 的话再开 **80 / 443**。
   **不要把 8000 直接开给公网**——那是绕过 HTTPS 的口子

---

## 三、部署（三条命令）

SSH 登上去之后：

```bash
git clone https://github.com/42-beep/course-assistant.git /tmp/ca
sudo bash /tmp/ca/deploy/setup.sh
```

脚本会做六件事：装 Python、拉代码到 `/opt/course-assistant`、建 venv 装依赖、
建数据目录、写 systemd 服务、打印后续指引。

**跑完之后一定要改口令**，别留着 `CHANGE_ME_BEFORE_PUBLIC`：

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(12))"   # 生成一个
sudo nano /etc/systemd/system/course-assistant.service          # 填进 ACCESS_CODE
sudo systemctl daemon-reload && sudo systemctl restart course-assistant
```

配模型 key（服务器上没有浏览器，得手写文件）：

```bash
sudo -u www-data nano /opt/course-assistant/config.json
```

```json
{ "base_url": "https://api.deepseek.com/v1", "api_key": "sk-xxx", "model": "deepseek-chat" }
```

---

## 四、有域名就上 HTTPS，没有就先用 IP

**有域名**（需先备案，1-2 周）：

```bash
sudo apt install -y caddy
sudo cp /opt/course-assistant/deploy/Caddyfile /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile        # 把 course.example.com 改成你的域名
sudo systemctl reload caddy
```

Caddy 会自己申请证书、自己续期，不用管。

**没域名**，临时用 IP 直连：

```bash
sudo sed -i 's/--host 127.0.0.1/--host 0.0.0.0/' /etc/systemd/system/course-assistant.service
sudo systemctl daemon-reload && sudo systemctl restart course-assistant
```

再去控制台安全组放行 8000，访问 `http://公网IP:8000`。
**这条路口令是明文传输的**，只适合自己测试，别发给别人。

---

## 五、免费额度的三个坑

1. **按小时扣钱**，300 元额度用完就按量计费。去「费用中心 → 预算管理」
   设一个 50 元告警，别等扣了才发现
2. **到期实例自动释放，数据只保留 72 小时**。`data/` 里是你传过的课件和
   所有分析结果，到期前拷走：
   `scp -r root@IP:/opt/course-assistant/data ./data-backup`
3. **同一实名主体只能领一次**。领之前想清楚这台机器要干什么

---

## 六、日常运维

```bash
sudo systemctl status course-assistant     # 活着没
sudo journalctl -u course-assistant -f     # 实时日志
sudo systemctl restart course-assistant    # 重启
```

更新代码：

```bash
sudo git -C /opt/course-assistant pull
sudo /opt/course-assistant/.venv/bin/pip install -r /opt/course-assistant/requirements.txt
sudo systemctl restart course-assistant
```

备份数据（建议挂个 cron 每天跑）：

```bash
tar czf /root/ca-data-$(date +%F).tgz -C /opt/course-assistant data
```

---

## 七、一句话总结

**免费 3 个月解决的是「能不能跑」，不解决「能不能当作品集」。**
想放简历就把域名 + 备案 + HTTPS 一起做掉，那才是能给人看的形态；
只想验证跑不跑得通，免费额度完全够了，做完了把机器释放掉就行。
