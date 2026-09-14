# House Design System · 房子的长相

> **这份文档的唯一目的**：把「这个房子长什么样」写下来。
> 以后说「给我做一个日记页」，不是凭空设计一个日记页，
> 而是 —— **「按照现有 House Design System，给这个房子增加一个日记房间。」**
>
> ⚠️ **本文所有数值都是从 `web/index.html` 实际 CSS 里抄出来的，不是设计出来的。**
> 每条都带行号，可回查。**改之前先读这份；改之后必须回来更新这份。**
>
> 提取日期：2026-09-13 · 来源：`web/index.html:38-142`（token 区）+ 各组件定义处
> 状态：**已提取、未重构**。`index.html` 依然是单文件，本文只做"记录"，不动代码。

---

## 零、一页速览（如果只看一段）

| 维度 | 一句话 |
|---|---|
| **风格名** | **珍珠潮汐**（PEARL TIDE）· 备选主题 **海港**（harbor） |
| **材质观** | 🫧 **玻璃拟态是灵魂** —— 面板永远"半透明 + 模糊"，让壁纸透出来 |
| **色彩观** | 低饱和墨蓝 / 雾灰蓝 / 米白。**没有一处饱和色**（唯一例外是危险色赭红） |
| **字体现** | **全衬线** —— 英文 Cormorant Garamond + 中文 Noto Serif SC。这是"信笺感"的来源 |
| **圆角观** | 小元素 10-13px · 卡片 18px · 气泡 14-20px · 弹层 20px · 胶囊 999px |
| **间距观** | 4 的倍数（4/8/12/16/24/32/40）；**主容器一律 `clamp()` 流式** |
| **动效观** | 只做"出现/消失/位移"。150ms（快）/ 260ms（常规），统一 `--ease-soft` |
| **情绪观** | **盐系手帐** —— 留白多、细线分隔、弱对比、不吵 |

---

## 一、Colors（颜色）

### 1.1 主题机制（这是最重要的结构）

**双主题 = 同一组变量名，两套取值。** 主题切换靠 `:root[data-theme="harbor"]` 覆盖。

```css
:root { /* PEARL TIDE · 珍珠潮汐（默认，Lily 用的这个） */ }
:root[data-theme="harbor"] { /* HARBOR · 海港（通用开源主题）*/ }
```

🔴 **铁律：harbor 只覆盖"带色相"的变量，其余继承 PEARL TIDE。**
所以新增颜色 token 时，**必须同时在两个块里给出取值**（或明确写注释说明为什么可以继承）。

### 1.2 语义色（两套主题对照）

| Token | PEARL TIDE（默认） | HARBOR（海港） | 用途 |
|---|---|---|---|
| `--bg` | `#F7FAFC` | `#F4F2EF` | 页面底 |
| `--header-bg` | `#F7FAFC` | `#F4F2EF` | 顶栏底 |
| `--text` | `#253447` | `#36404B` | 主文字（墨蓝） |
| `--text-soft` | `#5E7080` | `#5E6B78` | 次要文字 |
| `--text-faint` | `#8A99A8` | `#9197A0` | 最弱文字 / 提示 |
| `--hairline` | `rgba(120,142,165,.24)` | `rgba(74,93,108,.20)` | 细线分隔 |
| `--accent` | `#4C6378` | `#4A5D6C` | 强调色 / 选中态 |
| `--accent-fg` | `#ffffff` | `#ffffff` | 强调色之上的文字 |
| `--send-bg` | `#2C4056` | `#4A5D6C` | 发送按钮 |
| `--tick` | `#7E93A4` | `#7E8E9C` | 状态小标 |
| `--status-dot` | `#5fbf8f` | `#6FA98C` | 在线绿点（唯一的绿） |
| `--danger` | **`#c0553f`** | 同左（继承） | 🔴 危险操作（删除）。**两主题共用**，故意的 |
| `--composer-bg` | `#EEF4FA` | `#EFEBE8` | 输入区底 |
| `--field-bg` | `rgba(247,250,252,.92)` | `rgba(244,242,239,.92)` | 输入框底 |
| `--field-line` | `rgba(151,169,181,.18)` | （继承） | 输入框描边 |
| `--slider-thumb` | `#44627b` | `#4A5D6C` | 滑块 |

### 1.3 气泡色（消息气泡专用）

| Token | PEARL TIDE | HARBOR | 用途 |
|---|---|---|---|
| `--bubble-ai-bg` | `#ECEEF3` | `#EAE6E4` | AI 气泡底（冷灰） |
| `--bubble-ai-fg` | `#253447` | `#36404B` | AI 气泡字 |
| `--bubble-ai-line` | `transparent` | （继承） | 气泡描边（当前无） |
| `--bubble-human-bg` | `#DFE5EE` | `#E1E0E3` | 人类气泡底（略深） |
| `--bubble-human-fg` | `#253447` | `#36404B` | 人类气泡字 |
| `--bubble-human-line` | `transparent` | （继承） | 气泡描边（当前无） |

> ⚠️ **注意：AI 气泡与人气泡只差一点点明度**（`#ECEEF3` vs `#DFE5EE`）。
> 这是刻意的 —— 靠**左右位置 + 小尖角**区分，而不是靠颜色。**不要为了"更清楚"去拉大色差**，那会毁掉柔和感。

### 1.4 思考态色（`thinking` 消息）

| Token | PEARL TIDE | HARBOR |
|---|---|---|
| `--think-flourish` | `rgba(99,121,142,0.52)` | `rgba(74,93,108,0.48)` |
| `--think-label` | `#6A7E8E` | `#5E6B78` |
| `--think-body` | `#56697A` | `#4F5D6A` |

### 1.5 材质色（玻璃拟态 —— 这是灵魂）

| Token | 取值 | 用途 |
|---|---|---|
| `--card-bg` | `rgba(244,248,250,.42)` | 卡片底（**42% 透明度 + blur**） |
| `--card-line` | `rgba(150,168,182,.12)` | 卡片描边 |
| `--card-shadow` | `0 10px 28px rgba(70,92,108,.05)` | 卡片投影（极淡） |
| `--seg-track` | `rgba(140,160,176,.11)` | 分段控件轨道 |
| `--seg-thumb` | `rgba(255,255,255,.68)` | 分段控件滑块 |
| `--seg-thumb-shadow` | `0 2px 8px rgba(40,60,78,.08)` | 滑块投影 |
| `--row-press` | `rgba(140,160,176,.09)` | 行按压反馈 |
| `--slider-track` | `rgba(143,162,176,.28)` | 滑块轨道 |
| `--info-ring` | `rgba(143,162,176,.38)` | 信息图标圆环 |

### 1.6 投影与遮罩

| Token | 取值 |
|---|---|
| `--shadow` | `0 18px 46px rgba(74,93,108,0.10)` |
| `--soft-shadow` | `0 14px 34px rgba(86,104,118,0.08)` |
| `--scrim` | `linear-gradient(180deg, rgba(255,255,255,0.38), rgba(255,255,255,0.12) 30%, rgba(255,255,255,0.04) 68%, rgba(255,255,255,0.22))` |

> `--scrim` 是**顶栏那层渐变遮罩**，让文字浮在壁纸上仍可读。它是白色系的，不是黑色 —— 记住这个方向。

### 1.7 通话态（语音浮动层，当前按钮已隐藏但 token 保留）

| Token | 取值 |
|---|---|
| `--call-glass-top` | `rgba(255,255,255,.46)` |
| `--call-glass-bot` | `rgba(255,255,255,.20)` |
| `--call-glass-edge` | `rgba(255,255,255,.58)` |
| `--call-glass-hi` | `rgba(255,255,255,.72)` |
| `--call-glass-glow` | `rgba(150,172,196,.20)` |
| `--call-glass-shadow` | `0 20px 50px rgba(74,93,108,.16)` |
| `--call-scrim` | `rgba(64,82,102,.06)` |
| `--call-hangup-bg` | `#cf8d92`（挂断粉） |
| `--call-hangup-shadow` | `rgba(176,96,104,.34)` |

---

## 二、Typography（字体）

### 2.1 字族（全衬线 —— 不可更换）

```css
--font-en: "Cormorant Garamond", Georgia, serif;
--font-cn: "Noto Serif SC", "Songti SC", "STSong", "SimSun", serif;
```

🔴 **新页面必须沿用。** 这是"信笺感 / 手帐感"的全部来源。
唯一例外：代码块用了等宽（见 §6.4）。

### 2.2 字号阶梯（实测使用值）

| 场景 | 取值 | 来源 |
|---|---|---|
| 正文（body） | `clamp(14px, 1.45vw, 16px)` | `body{}` |
| 气泡正文 | `clamp(14px, 1.55vw, 16px)`，`line-height:1.58` | `.bubble` |
| 卡片标题（`.confirm-title`） | `15.5px` | `.confirm-title` |
| 卡片正文（`.confirm-body`） | `13px`，`line-height:1.6` | `.confirm-body` |
| 设置分组标签 | `12.5px` | `.settings-label` |
| 分段控件 | `clamp(10.5px, 2.9vw, 12.5px)` | `.segmented button` |
| 提示文字（`.push-hint`） | `11.5px`，`line-height:1.55` | `.push-hint` |
| 脚注（`.confirm-note`） | `11.5px`，`line-height:1.55` | `.confirm-note` |
| 按钮（`.confirm-btn`） | `13px` | `.confirm-btn` |

**行高习惯**：正文 `1.5` · 气泡 `1.58` · 卡片正文 `1.6` · 提示 `1.55`。
→ 规律：**越小的字，行高越大**。

### 2.3 字重

只用两档：**默认（400）** 和 **500/600/700**（强调）。
`.segmented button.active` → `500` · `.bubble strong` → `700` · `#confirmCancel` → `600`。
**没有 300、没有 800。** 别引入。

---

## 三、Spacing（间距）

### 3.1 基础刻度

**4 的倍数**：`4 / 8 / 12 / 16 / 24 / 32 / 40`

### 3.2 主容器一律流式 `clamp()`

| Token | 取值 | 用途 |
|---|---|---|
| `--header-h` | `clamp(56px, 10vw, 120px)` | 顶栏高 |
| `--side-pad` | `clamp(16px, 4vw, 40px)` | 左右安全边距 |
| `--avatar-size` | `clamp(32px, 5vw, 48px)` | 头像 |
| `--bubble-radius` | `clamp(14px, 2vw, 20px)` | 气泡圆角 |
| `--composer-h` | `clamp(44px, 6vw, 72px)` | 输入区高 |
| `--edge-fade-top` | `clamp(24px, 5vw, 52px)` | 顶部渐隐 |
| `--edge-fade-tail` | `clamp(16px, 3vw, 30px)` | 底部渐隐 |

🔴 **新页面一律用 `clamp()` 做流式**，不要写死 px。这是从桌面到 iPhone 都能舒服的唯一原因。

### 3.3 常用实测间距

| 场景 | 取值 |
|---|---|
| 卡片内边距（`.settings-card`） | `15px 16px` |
| 弹层内边距（`.confirm-card`） | `20px 20px 14px` |
| 气泡内边距 | `clamp(8px,1.05vw,12px) clamp(13px,1.65vw,18px) clamp(8px,1.1vw,13px)` |
| 分段控件内边距 | `3px`，gap `3px` |
| 按钮组 gap（`.confirm-acts`） | `8px` |
| 标签下边距 | `11px` |

---

## 四、Radius（圆角）

| 层级 | 取值 | 用在哪 |
|---|---|---|
| **胶囊** | `999px` | 按钮（`.confirm-btn`）、滑块轨道 |
| **弹层** | `20px` | `.confirm-card` |
| **卡片** | `18px` | `.settings-card` |
| **气泡** | `clamp(14px,2vw,20px)` | `.bubble`（主） |
| **气泡小尖** | `2px` | `.row.ai.tail .bubble` 左下角 / human 右下角 |
| **分段控件** | 轨 `13px` / 滑块 `10px` | `.segmented` |
| **图片** | `14px` | `.att-img` |

> 规律：**小元素 10-13px · 卡片 18px · 弹层/气泡 20px · 胶囊 999px**。

---

## 五、Shadows（投影）

| 名称 | 取值 | 场合 |
|---|---|---|
| card | `--card-shadow` = `0 10px 28px rgba(70,92,108,.05)` | 卡片（最淡） |
| soft | `--soft-shadow` = `0 14px 34px rgba(86,104,118,0.08)` | 次级浮层 |
| main | `--shadow` = `0 18px 46px rgba(74,93,108,0.10)` | 主浮层 |
| bubble | `0 10px 28px rgba(78,94,108,0.08)` | 气泡（写在 `.row.ai/.human .bubble` 里，非 token） |
| modal | `0 24px 60px rgba(30,45,62,.30)` | 弹窗（最重，唯一的深投影） |
| seg-thumb | `--seg-thumb-shadow` = `0 2px 8px rgba(40,60,78,.08)` | 分段滑块 |

🔴 **投影全部是"低透明度 + 大扩散"** —— 没有一处是硬阴影。
**唯一的例外是 `.confirm-card` 的 `0 24px 60px rgba(30,45,62,.30)`**，因为它是模态、必须"浮起来"。

---

## 六、Components（组件）

> 标注说明：✅ 已有可直接复用 · ⚠️ 已有但需参数化 · ❌ 缺，新页面要新建

### 6.1 Buttons（按钮）

**A. `.confirm-btn` — 胶囊按钮**（当前最完整的按钮实现）

```css
/* 常规 */
border:1px solid rgba(140,160,176,.28); border-radius:999px; padding:8px 16px;
background:rgba(140,160,176,.12); color:var(--text-soft); font-size:13px;
/* 主按钮 */  .primary → background:var(--accent); border-color:var(--accent); color:var(--accent-fg,#fff)
/* 危险按钮 */ .danger  → background:var(--danger,#c0553f); color:#fff
/* 按压 */   :active → transform:scale(.98)
/* 默认焦点（取消） */ #confirmCancel → font-weight:600; background:rgba(255,255,255,.72); border-color:rgba(120,142,165,.42)
```

> 🔴 **`#confirmCancel` 那条是设计决定，不是样式**：默认焦点给"取消"，
> 是为了**手滑按回车不该误删**。新页面的危险操作要沿用同一个模式。

**B. `.topbtn` — 顶栏图标按钮**

```css
/* 尺寸 32px（移动端），svg 24×24（.more 是 22×22） */
/* 按下 */ .topbtn:active{ transform: scale(.9); }
/* 选中 */ .topbtn.terminal.active / .call.active → color: var(--accent)
```

**C. `.session-act` — 列表行内小图标按钮**（改名/归档/删除）

```css
/* svg 15×15 · hover 时 color:var(--accent) + background:rgba(140,160,176,.13) */
/* 危险态 */ .session-act.danger:hover → color: var(--danger, #c4553f)
```

**D. `.segmented button` — 分段控件按钮**（见下）

> ❌ **缺 `.k-btn` 通用按钮类**。新页面要做按钮时，从上面三个里挑最接近的抄，
> 或新建 `.k-btn` 并回填到本文档。

### 6.2 Segmented Control（分段控件）✅

```css
.segmented{ display:grid; grid-auto-flow:column; grid-auto-columns:1fr;
  gap:3px; background:var(--seg-track); border-radius:13px; padding:3px; }
.segmented button{ border:0; background:transparent; color:var(--text-soft);
  border-radius:10px; padding:8px 3px; font-size:clamp(10.5px,2.9vw,12.5px);
  transition: background .22s ease, color .22s ease, box-shadow .22s ease; }
.segmented button.active{ background:var(--seg-thumb); color:var(--text);
  box-shadow:var(--seg-thumb-shadow); font-weight:500; }
```

**用途**：主题切换 / 通知开关 / 联系方式切换 / 未来：参数选择、栏目切换。

### 6.3 Cards（卡片）

**A. `.settings-card` — 玻璃卡片**（信息分组的主力）

```css
background:var(--card-bg);          /* rgba(244,248,250,.42) */
border:1px solid var(--card-line);
border-radius:18px;
box-shadow:var(--card-shadow);
padding:15px 16px;
backdrop-filter: blur(14px);
```

**B. `.confirm-card` — 模态弹层**（⚠️ **历史教训在注释里**）

```css
width:min(92vw,360px); padding:20px 20px 14px; border-radius:20px;
background:rgba(252,253,255,.96);                       /* ← 实色，不用变量 */
backdrop-filter: blur(24px) saturate(1.5);
border:1px solid rgba(150,168,182,.20);
box-shadow:0 24px 60px rgba(30,45,62,.30);
/* harbor 覆盖 */ :root[data-theme="harbor"] .confirm-card{ background:rgba(250,248,246,.96); border-color:rgba(74,93,108,.18); }
```

> 🔴🔴 **为什么这里的背景是"实色 + 高透明度"而不是 `var(--panel-bg)`？**
> **因为第一版写的就是 `var(--panel-bg)`，而那个变量在这个文件里从来没被定义过。**
> CSS 变量未定义 → **该属性被整条静默丢弃**（不报错、控制台无提示）→ 卡片没有背景
> → 底下的聊天记录直接透上来，字看不清。Lily 手机截图报的正是这个。
> **同一个 bug 也打在 `.session-pop` 上。**
>
> **通用规则：引用一个 token 前，先在文件里搜它的「定义」（`--name:` 形式），别只看引用处像不像对。**
> （踩坑细节：`--seg-line` 也不存在；但 `--seg-track` 是存在的，别顺手"修"错。）

### 6.4 Message Bubbles（消息气泡）✅

```css
.bubble{
  max-width: min(61vw, 506px);
  padding: clamp(8px,1.05vw,12px) clamp(13px,1.65vw,18px) clamp(8px,1.1vw,13px);
  border-radius: var(--bubble-radius);        /* clamp(14px,2vw,20px) */
  font-family: var(--font-cn);
  font-size: clamp(14px,1.55vw,16px); line-height:1.58;
  animation: pop .24s cubic-bezier(.2,.8,.2,1) both;
}
.row.ai .bubble    { background:var(--bubble-ai-bg);    box-shadow:0 10px 28px rgba(78,94,108,0.08); }
.row.human .bubble { background:var(--bubble-human-bg); box-shadow:0 10px 28px rgba(78,94,108,0.08); }
/* 每段最后一条收小尖 */
.row.ai.tail .bubble    { border-bottom-left-radius: 2px; }
.row.human.tail .bubble { border-bottom-right-radius: 2px; }
```

**入场动画（只对新消息播一次，虚拟列表复用不重播）：**

```css
@keyframes enterHuman{ from{ transform:translate(6px,9px) scale(.95); opacity:0;} 55%{opacity:1;} to{transform:none;opacity:1;} }
@keyframes enterAi   { from{ transform:translate(-6px,9px) scale(.95);opacity:0;} 55%{opacity:1;} to{transform:none;opacity:1;} }
/* 时长 .44s · 曲线 cubic-bezier(.34,1.46,.5,1)（轻微回弹） */
```

> 🔴 细节：`transform-origin` 也不同 —— human 是 `bottom right`，ai 是 `bottom left`。
> 气泡**从自己那一侧长出来**。

**气泡内行的排版**：

```css
.bubble .txt a{ color:inherit; text-decoration:underline; text-underline-offset:2px; }
.bubble .txt strong{ font-weight:700; }
/* 代码块：等宽字体（全衬线的唯一例外） */
```

**带附件的气泡**：

```css
.bubble.has-att{ display:flex; flex-direction:column; gap:6px; }
.bubble.att-only{ background:transparent!important; padding:0!important; box-shadow:none!important; }
.att-img{ border-radius:14px; width:min(64vw,260px); max-height:340px; object-fit:cover; }
```

### 6.5 Inputs（输入）

**A. `.composer` — 输入区**

| 项 | 取值 |
|---|---|
| 高度 | `--composer-h` = `clamp(44px, 6vw, 72px)` |
| 占位区总高 | `--composer-zone` = `calc(var(--composer-h) + 16px + env(safe-area-inset-bottom) + var(--keyboard-offset))` |
| 底色 | `--composer-bg` |
| 输入框底 | `--field-bg` |
| 描边 | `--field-line`，`focus-within` 时同为 `--field-line` |

> 🔴 **`--composer-zone` 是"单一真相源"**：滚动区底部 padding 与边缘渐隐**共用同一个来源**，
> 保证两者永远对齐。新页面有底部固定输入区时，沿用这个模式。

**B. Slider（滑块）**

```css
.context-slider{ -webkit-appearance:none; width:100%; height:4px; border-radius:999px; }
/* 轨道 */--slider-track: rgba(143,162,176,.28)
/* 滑块 */--slider-thumb: #44627b
```

**C. 焦点可见性**

```css
.composer textarea:focus,
.composer textarea:focus-visible,
.composer button:focus-visible{ /* 见 index.html:942-943 */ }
```

> 键盘用户必须能看见焦点。**不要 `outline:none` 了事。**

### 6.6 Lists & Rows（列表与行）

| 组件 | 现状 | 说明 |
|---|---|---|
| `.action-row` | ✅ | 可点击行（列表项）。`.status` 变体是纯展示行 |
| `.session-pop` | ✅ | 气泡式下拉浮层（⚠️ 同样踩过 `--panel-bg` 未定义的坑） |
| `.menu-panel` | ✅ | 抽屉 / 主菜单（全屏、`.open` 切换） |
| `.profile-panel` | ✅ | 全屏页骨架（**新页面可以参考它的结构**） |
| **`.k-list`** | ❌ 缺 | 长列表（朋友圈 / 日记列表）—— 新页面要新建 |
| **`.k-tag`** | ❌ 缺 | 标签（日记分类 / 记忆类型）—— 新页面要新建 |

**按压反馈统一用 `--row-press` = `rgba(140,160,176,.09)`。**

### 6.7 States（状态）

| 状态 | 现有做法 | Token |
|---|---|---|
| **空状态** | ⚠️ 部分有（`#empty`） | — |
| **加载中** | `renderStatus()` 三态：`online` / `connecting…` / `offline` | `--status-dot` |
| **降级提示** | `#sessionNotice` + `setSessionNotice()`（第 5 处家装新增） | 文案用 `--text-faint` |
| **危险操作** | 二次确认弹层，默认焦点给"取消" | `--danger` |
| **按压** | `:active { transform: scale(.98~.9) }` 或 `background: var(--row-press)` | — |
| **禁用** | 未系统化 | ❌ 缺 |

> 🔴 **失败必须给可理解提示，不许静默降级。**
> （这是从"会话列表点开全空、但不报错"那个坑来的 —— 原版是静默 `catch`。）

### 6.8 Panels & Overlays（浮层）

**两档浮层，别混用：**

| 档 | 背景 | 模糊 | 圆角 | 用在哪 |
|---|---|---|---|---|
| **玻璃档** | `--card-bg`（42% 透明） | `blur(14px)` | `18px` | 卡片、信息分组 |
| **模态档** | `rgba(252,253,255,.96)`（近实色） | `blur(24px) saturate(1.5)` | `20px` | 确认框、会话面板 |

> ⚠️ **模态档必须用近实色** —— 玻璃档太透会让底下内容干扰阅读。
> （这正是 `--panel-bg` 那个 bug 的教训：**弹层不能用低透明度底**。）

---

## 七、Motion（动效）

| Token | 取值 | 用途 |
|---|---|---|
| `--motion-fast` | `150ms` | 微交互（按压、hover） |
| `--motion-normal` | `260ms` | 面板开合 |
| `--ease-soft` | `cubic-bezier(0.22, 1, 0.36, 1)` | **统一缓动曲线** |

**实测到的其它时长/曲线**：

| 场景 | 取值 |
|---|---|
| 气泡出现（`pop`） | `.24s cubic-bezier(.2,.8,.2,1)` |
| 新消息入场 | `.44s cubic-bezier(.34,1.46,.5,1)`（轻微回弹） |
| 分段控件切换 | `.22s ease` |
| 面板开合 | `transform:none; opacity:1` + `--motion-normal` |

🔴 **动效观：只做"出现 / 消失 / 位移"。**
不做旋转、不做色彩循环、不做装饰性动画。
**唯一的"花哨"是新消息入场的轻微回弹 —— 因为它承载情绪。**

---

## 八、Assets（壁纸与图像）

| Token | 文件 | 说明 |
|---|---|---|
| `--wall-light` / `--wall-default` | `chat-light.webp` | 聊天页壁纸（珍珠潮汐） |
| `--wall-harbor` | `chat-harbor.webp` | 聊天页壁纸（海港） |
| `--menu-wall-light` / `--menu-wall-default` | `menu-light.webp` | 菜单页壁纸 |
| `--menu-wall-harbor` | `menu-harbor.webp` | 菜单页壁纸（海港） |
| `--avatar-default` | `avatar-sea.png` | 默认头像 |

**新页面加壁纸时**：必须同时准备 **light + harbor 两张**，
并加对应的 `--xxx-wall-default` 和一个 `:root[data-theme="harbor"]` 覆盖。

---

## 九、页面骨架参考

### 9.1 新页面的文件结构（已定，见规划 §10.5）

```
web/
├── index.html          ← 聊天页（🔴 不动）
├── theme.css           ← 🆕 从 index.html 抽出的变量（所有页面共享）
├── components.css      ← 🆕 基础组件类
├── moments.html        ← 🆕 朋友圈
├── diary.html          ← 🆕 日记
└── sw.js               ← 原样（⚠️ 见下）
```

**抽取时机**：`index.html` 现在是 ~273KB / 5787 行。
🔴 **现在不拆**（虚拟滚动 / SSE / 语音全靠时序，拆错极难排查）。
**等第三个新页面出现时再抽** —— 那时有真实需求驱动，抽得准。

### 9.2 🔴 Service Worker 的坑（必须记住）

`sw.js` 里「**不拦截 `/relay/`**」这条规则是 **SSE 能工作的前提**。
**新页面接 SSE 时必须沿用同一前缀规则**，否则流会被 SW 缓存住 —— **极难排查**。

### 9.3 页面骨架要包含什么（参照 `.profile-panel`）

1. 背景：`--wall-default`（带主题切换）
2. 顶部：`--header-h` 高 + `--scrim` 渐变遮罩
3. 左右：`--side-pad` 安全边距
4. 底部（如有输入）：`--composer-zone` 单一真相源
5. 滚动区：底部 padding 与边缘渐隐共用 `--composer-zone`

---

## 十、待补 / 已知缺口

| 缺口 | 影响 | 建议 |
|---|---|---|
| ❌ 无 `.k-btn` | 新页面按钮要靠抄 | 抽通用按钮类 |
| ❌ 无 `.k-list` | 长列表要重写 | 抽列表类 |
| ❌ 无 `.k-tag` | 标签要重写 | 抽标签类 |
| ⚠️ `.k-empty` 只有部分 | 空状态不统一 | 统一空状态组件 |
| ⚠️ 禁用态未系统化 | — | 定义 `--disabled` 系列 |
| ⚠️ token 未外提 | 新页面无法共享 | 建 `theme.css`（**建议：下一个新页面时顺手做**）|

---

## 十一、给「下一个新页面」的开工清单

做**日记页**（或其他任何新房间）时，按这个顺序：

1. **读这份文档**（不要靠看 `index.html` 猜）
2. 建 `diary.html`，**新建**，不碰 `index.html`
3. 从本文档 §1 复制需要的颜色 token（**两个主题都要给值**）
4. 字体用 `--font-cn`（衬线，不可换）
5. 布局用 `clamp()` 流式，间距用 4 的倍数
6. 面板选"玻璃档"还是"模态档"（§6.8），**弹层必须用近实色**
7. 动效只用 `--motion-fast` / `--motion-normal` + `--ease-soft`
8. 接 SSE 的话，确认 `sw.js` 的前缀规则覆盖到你
9. **改完回来更新这份文档**（新增的 token / 组件要写进来）

---

## 附：事实来源

| 结论 | 证据 |
|---|---|
| token 区 190 行 | `index.html:38-142` |
| 双主题覆盖结构 | `index.html:118-142`（harbor 只覆盖色相变量） |
| 气泡实测值 | `index.html:607-647` |
| 卡片实测值 | `index.html:1118-1126` |
| 弹层实测值 + `--panel-bg` 教训 | `index.html:379-406` |
| 分段控件实测值 | `index.html:1146-1161` |
| 产品规划中的设计系统章节 | `架构与产品路线规划.md` §10 |
