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

## 本地核验

```bash
node scripts/check_wxss_tokens.mjs
# 期望：scanned 10 wxss files, 0 violations, N whitelisted entries (...)
```
