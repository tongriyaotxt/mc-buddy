# COMPLIANCE —— 分发合规（2026-10-29，operator：合规你弄）

> 目标：**别人装得上、我们发得合法**。这份文件写死"怎么发、发什么、不发什么、源码在哪"。
> 技术清单见 [`THIRD-PARTY.md`](../THIRD-PARTY.md)；版权声明见 [`NOTICE`](../NOTICE)。

---

## 1. 我们自己的东西：Apache-2.0

- 代码：`companion/`、`launcher/`、`tools/`、`orchestrator/` 等全部按 **Apache-2.0**（见 `LICENSE`）。
- 数据与台账：另按 `docs/DATA-LICENSE.md`（CC BY 4.0）。
- 商业使用：允许；但**不得使用 Mojang/Microsoft 的商标与资源**暗示官方关联。

## 2. 改过的 mod：GPL-3.0 合规三条

`_refs/unionclef-0.95.63` 的 `LICENSE` 是 **GPL-3.0**（`fabric.mod.json` 却写 MIT —— **待定案**）。
在定案之前，我们按**更严格**的一边执行：

1. **随包提供源码获取方式**：安装包内放 `SOURCE-OFFER.txt`，指向
   `https://github.com/<org>/for-xiaorun`（或发布页）与构建脚本 `tools/_make_hybrid_jar.py`；
2. **补丁与构建可复现**：`tools/patch_*.py` + `HANDOFF §6` 的 gradle 配方就是源码级说明；
3. **不改许可声明**：jar 内保留原始 `LICENSE` / `NOTICE` / `META-INF`，我们不覆盖。

> 若定案为 MIT（上游 AltoClef 是 MIT），上述做法依然成立（多余但不违规）。

## 3. 不分发清单（硬红线）

| 东西 | 原因 | 我们的做法 |
|---|---|---|
| Minecraft 客户端/服务端文件、材质、音效 | Mojang EULA | **一个字节都不进安装包**；用户用官方/PCL 启动器自备 |
| Paper 服务端 jar | GPL-3.0 且体积大 | 文档给链接，用户自下 |
| Java 运行时 | 各自许可 | 用系统/JBR |
| SenseVoice 模型 | 许可待核 | **外置下载**（用户点一下），不进 exe |
| 第三方账号/密钥 | 隐私 | **用户自带 key（BYO）**，我们不做代理、不收集 |

## 4. BYO key（operator 2026-10-29 定调）

- 模型调用使用**用户自己的** DeepSeek key（`config/ai.json`），我们**不提供共享 key、不做中转**；
- key 只落本地文件，**不进日志、不进诊断包**（`incident.py` 只收 trace/env/plan）；
- 界面上明文只显示尾 4 位；
- 若将来做"代付"模式，另立计费与风控文档，不在本次范围。

## 5. 公服边界

- 安装器首启弹窗与 `docs/USER-QUICKSTART.md` 都写明：**只用于单机 / 自建服 / 朋友服**；
- 多数公共服务器把"自动操作"视为宏/脚本，使用会封号 —— 风险由使用者承担，但我们要**显著提示**；
- 后续可加"检测到公网地址弹二次确认"（待产品决定是否硬拦）。

## 6. 支持闭环（operator 2026-10-29 定调）

- 崩溃自动留证：`runs/crash/<ts>/`（默认**只存本地**）；
- **opt-in 上传**：`config/support.json` 里 `enabled=true` 且配了 `endpoint` 才会 POST，诊断包**不含密钥**；
- 面板「诊断」页提供"是否自动上报"的勾选与说明。

## 7. 发布前检查单

- [ ] `LICENSE` / `NOTICE` / `THIRD-PARTY.md` / `SOURCE-OFFER.txt` / 本文件 全部进安装包；
- [ ] 安装包内不含任何 Mojang 资源、Paper jar、模型文件；
- [ ] BYO key：日志/诊断包抽查 3 份，确认无 key；
- [ ] 公服提示在首启弹窗 + 文档 + 面板三处可见；
- [ ] 许可待核两项（UnionClef、SenseVoice）已有明确结论或"暂缓自动分发"的替代方案。
