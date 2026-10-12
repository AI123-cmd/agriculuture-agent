# 适老化样式 token 与公共类

本目录的 `tokens.wxss` 是全项目**唯一**允许出现字号、行距、热区、间距字面量的文件。
页面与组件的 WXSS 只允许使用这里的 `var(--token)` 或 `app.wxss` 中的公共类。

依据：产品说明书 4.7.2 显示规范、9.1 样式层约束、附录 A10。

## token 清单

| token | 值 | 用途 |
| --- | --- | --- |
| `--font-caption` | 32rpx | 辅助说明、单位、来源标注（字号下限） |
| `--font-body` | 36rpx | 正文、提示文案、对话气泡 |
| `--font-title` | 40rpx | 卡片小标题、页面副标题 |
| `--font-heading` | 44rpx | 区块标题、卡片标题 |
| `--font-display` | 52rpx | 页面主标题、核心数值 |
| `--line-tight` | 1.4 | 与 40rpx 及以上字号搭配 |
| `--line-body` | 1.5 | 与 36rpx 正文字号搭配（实际行距 54rpx） |
| `--space-para` | 72rpx | 段间距（≥ 36 × 1.5 × 1.3 = 70.2rpx） |
| `--space-heading` | 24rpx | 标题与其所属正文之间 |
| `--space-block` | 72rpx | 模块间距 |
| `--space-inline` | 24rpx | 行内水平间隔 |
| `--tap-min` | 88rpx | 可点击热区下限（宽高同时满足） |
| `--btn-primary-height` | 96rpx | 关键操作按钮高度 |
| `--tabbar-height` | 120rpx | 底部导航高度 |
| `--text-primary` | #1a1a1a | 主文字（白底 ~17.4:1） |
| `--text-body` | #333333 | 正文（白底 ~12.6:1） |
| `--text-muted` | #595959 | 辅助文字（白底 ~7.0:1） |
| `--text-inverse` | #ffffff | 深色底上的反白文字 |
| `--text-brand` | #2e7d32 | 品牌绿 |
| `--bg-page` | #eef5ec | 页面背景 |
| `--bg-card` | #ffffff | 卡片背景 |
| `--line-divider` | #e0e0e0 | 分隔线 |

> 若调整 `--font-body`，`--space-para` 须同步满足「字号 × 行距 × 1.3」的下限，两者在
> `tokens.wxss` 内以注释互相标注。

## 公共类（定义在 `app.wxss`）

- 字号类：`.t-caption` / `.t-body` / `.t-title` / `.t-heading` / `.t-display`
- 热区类：`.tap-88`（外层撑起 88rpx，内层负责视觉尺寸）
- 关键按钮：`.btn-primary`
- 卡片：`.card` / `.card-title`；模块间距：`.block`
- 勾选：`.check-circle`（视觉 56rpx，热区由外层 `.tap-88` 撑起）

## 纪律

- 页面 WXSS 首行必须 `@import` 指向 `styles/tokens.wxss`；`custom-tab-bar` 因是自定义
  组件不继承页面样式，需 `@import '../styles/tokens.wxss';` 自行导入。
- **不锁定字号**：token 值用 rpx 而非 px，不使用 `text-size-adjust`，不写 `!important`
  覆盖字号，保证跟随系统字号缩放。

## 前端不得截断（T4-05 配套）

提示文案的长度上限与问诊选项的数量上限都由后端在生成侧收敛（见 `app/broadcast/limits.py`
与 `app/agent/contracts/interview.py`）。前端只按后端给定内容渲染，**不做任何裁剪**——
留到前端截断会在适老化验收时反复返工。三条禁止：

1. 提示正文不得使用 `-webkit-line-clamp`、`text-overflow: ellipsis`、`white-space: nowrap`
   做截断。历史列表的 `.conv-preview` 允许 `ellipsis`，因为它是列表摘要而非提示正文，
   且点击后可见全文。
2. 问诊选项不得由前端丢弃。后端给几个就渲染几个；若版面放不下，改为两行或纵向排列，
   而不是删除选项。
3. 前端不得写 `slice` / `substring` 处理面向农户的文案。历史列表标题/预览若用到
   `slice`，必须在代码注释中写明"仅用于列表预览，不用于展示正文"。

可检查落点：`grep -rn "line-clamp\|text-overflow\|\.slice(\|substring(" miniprogram/pages`
的结果逐条评审，确认命中项都不涉及提示正文与问诊选项。

## 白名单申请流程

门禁脚本 `scripts/check_wxss_tokens.mjs` 会把所有字面量命中报为违规。确有必要保留的例外
登记在 `scripts/wxss-token-whitelist.json`，**只允许三类**：

1. `decoration` —— 纯装饰图形（无文字、不可点击，如定位 pin、状态点、装饰图标）
2. `divider` —— 结构性 1rpx 分隔线
3. `safe-area` —— `calc()` 中与安全区相关的偏移量

凡是承载文字的元素、凡是可点击元素，一律不允许进白名单；可点击元素的视觉尺寸若小于
88rpx，应改用「外层 `.tap-88` 撑热区 + 内层保持视觉尺寸」的写法。

新增条目格式（每条含 `file`、`match`、`reason`、`category`）：

```json
{
  "file": "miniprogram/pages/<page>/<page>.wxss",
  "match": "选择器或片段的正则",
  "reason": "为什么必须保留字面量",
  "category": "decoration"
}
```

`category` 若落在三类之外，需在 PR 描述里写明理由，供评审判断。

### 当前登记条目（与 `scripts/wxss-token-whitelist.json` 一致）

| 文件 | match | 类别 | 理由 |
| --- | --- | --- | --- |
| `miniprogram/app.wxss` | `\.check-circle` | decoration | 勾选圆圈视觉 56rpx，热区由外层 `.tap-88` 撑满 |
| `miniprogram/app.wxss` | `card-title` | decoration | 卡片标题左侧装饰条为纯装饰图形，不可点击 |
| `miniprogram/components/sprout-mascot/index.wxss` | `^` | decoration | 小禾 IP 组件整文件为纯装饰图形，无文字、无交互 |
| `miniprogram/custom-tab-bar/index.wxss` | `tab-icon\|home-icon\|history-icon\|mine-icon\|clock-hand` | decoration | Tab 图标为装饰图形，热区由 `.tab-item` 保证 |
| `miniprogram/pages/ai/chat.wxss` | `msg-avatar` | decoration | 对话头像为展示性装饰，不可点击 |
| `miniprogram/pages/ai/chat.wxss` | `vp-` | decoration | 语音喇叭为装饰，热区由 `.voice-play-btn`（`--tap-min`）保证 |
| `miniprogram/pages/env/env.wxss` | `refresh-icon\|location-pin\|metric-icon\|device-dot\|footer-placeholder\|data-label` | decoration | 刷新/定位/指标/状态点图标、数据标签圆点与底部占位均为装饰 |
| `miniprogram/pages/index/index.wxss` | `location-pin\|tip-callout` | decoration | 地点定位图标与提示要点前置圆点为装饰，不可点击 |
| `miniprogram/pages/login/login.wxss` | `\.checkbox` | decoration | 协议复选框视觉 44rpx，热区由外层 `.tap-88` 撑满 |
| `miniprogram/pages/mine/mine.wxss` | `avatar-edit\|cam-body\|cam-lens\|tag-leaf\|section-label\|menu-icon\|mi-\|footer-dot\|sheet-bar` | decoration | 我的页图标、装饰点与弹层把手均为装饰 |
| `miniprogram/pages/mine/mine.wxss` | `menu-divider` | divider | 菜单项之间的结构性 1rpx 分隔线 |
| `miniprogram/pages/profile/farmer-profile.wxss` | `footer-placeholder` | decoration | 底部滚动占位高度，非交互元素 |

## 本地核验

```bash
node scripts/check_wxss_tokens.mjs
# 期望：scanned 10 wxss files, 0 violations, N whitelisted entries (...)
```
