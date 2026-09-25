# semantic-understanding

> 让 AI 听懂你的**表意习惯**，而不只是你的字面用词。

提示词工程优化的是「AI 怎么说」。这个技能处理另一半问题 —— **AI 怎么听**。

> **在线预览**：https://yang20040317-svg.github.io/semantic-understanding/
>
> 打开的就是仓库里的 [`docs/index.html`](docs/index.html) —— 用示例数据渲染的 demo 面板。
> Pages 只是把这个文件当网页呈现，**不是第二份内容**，改仓库它就跟着变。

## 它解决什么

你和 AI 反复改同一件事，往往不是它不懂技术，而是它把你的话解释错了：

| 你说 | 它理解成 | 你其实要 |
|---|---|---|
| 「这版看着很平」 | 换一套配色和圆角 | 信息层级与分区结构不对 |
| 「弄一下」 | 按字面最小范围处理 | 结论先行 + 下一步动作 |
| 「根本用不起」 | 一句抱怨或嫌贵 | 成本是硬约束，要的是降本方案而非解释 |

调一次 prompt 只能修一次。**这个技能记的是「纠正」本身** —— 每次它解释错、你纠正它，
就把「你的原话 → 字面误读 → 真实意图 → 可泛化规则」存成一条结构化案例。下次遇到同类
说法，规则先于字面理解生效。

一句话：它不是更丰富的提示词，是一份**关于你怎么说话的个人档案**，会随对话持续生长。

## 机制

**三级行为协议**

| 级别 | 触发 | 行为 |
|---|---|---|
| L0 静默应用 | 命中已确认规则 | 直接按真意执行，不复述、不啰嗦 |
| L1 复述确认 | 未命中，或涉及写文件 / push / 删除等不可逆动作 | 动手前只问一行「我理解为：___」 |
| L2 纠偏留档 | 你说了「不对 / 我说的是 X」，或它刚做的被推翻 | 当场记成候选，收尾给你一份清单挑 |

**双通道生效**

- **常驻档案** `profile/expression-profile.md` —— 高置信规则，每会话读一次，静默生效
- **长尾检索** `cases/cases.jsonl` —— 按当轮话题现查现用，不占用固定上下文

**候选取证闸门**：候选**未经你确认不入库**。这是唯一的防噪机制 —— 单次口误不该变成
长期规则。

## 快速开始

```bash
git clone https://github.com/yang20040317-svg/semantic-understanding.git
cd semantic-understanding

# 可选：用示例数据起步，方便先看效果
cp cases/cases.example.jsonl cases/cases.jsonl
```

零外部依赖，只用 Python 标准库（3.10+）。

### 记下你的第一条纠正

```bash
python su.py capture \
  --surface "你那句原话" \
  --literal "它当时理解成了什么" \
  --intended "你其实要什么" \
  --correction "你怎么纠正的" \
  --rule "可泛化的规则" \
  --domain communication --tags "歧义词,短指令"

python su.py pending            # 看候选清单
python su.py confirm <id>       # 确认入库
python su.py promote            # 升级为常驻规则
python su.py viz --open         # 打开可视化面板
```

## 命令速查

```bash
python su.py capture ...        # 记一条候选（原话/误读/真意/纠正/规则）
python su.py pending            # 待确认清单
python su.py confirm <id>       # 确认入库
python su.py reject <id>        # 丢弃候选
python su.py recall --intent "<你正要干的事>"   # 检索相关习惯，输出可注入块
python su.py recall --intent "..." --with-core # 连同常驻规则一起打包
python su.py promote            # 把证据足够的规则升级进常驻档案
python su.py profile            # 查看常驻档案
python su.py pin <case-id>      # 置顶：无视证据阈值，强制进常驻
python su.py forget <id>        # 退役过时或记错的规则
python su.py doctor             # 体检注入机制
python su.py stats              # 概览

python viz.py --open            # 重绘可视化面板（写操作已自动触发）
python parity.py                # 跨语言判据对拍（改了检索逻辑后必跑）
```

## 检索判据

中文没有天然分词，纯 Jaccard 相似度会被长文本稀释、被功能字污染，还会把
「跨语言残词」当成信号。本技能的判据分三层提纯 + 两道门控：

**提纯**

1. **分段取特征** —— 中文段取 2/3/4-gram；拉丁段取整词 + 4-gram，**不切 2/3-gram**
   （否则 `documentary` 会切出 `to`、`en` 这类残词造成假命中）
2. **丢弃功能字 n-gram** —— 整段由「的地得了是不」构成的一律丢弃，它们是中文假命中的主凶
3. **长度加权** —— 2/3/4-gram 权重 0.45 / 0.85 / 1.15；**零重合直接返回 0**

**门控**

4. **领先判据** —— 次要命中须达最高分的 60% 才一并注入，滤掉跟在真命中后的噪音
5. **短句兜底** —— 极短指令（≤6 个汉字）字面判据必然失明，改按结构特征兜底到「短指令」规则

### 已知边界

字面判据分不清**同词异义**：查询里的「技能」撞上案例里的「技能」但所指不同时，
仍会拿到分数。根治需要 IDF 或语义模型，超出「零依赖标准库」的约束，故未实现。

## 目录结构

```
semantic-understanding/
├── SKILL.md                      行为协议（宿主读取）
├── su.py                         存储 / 检索 CLI（零依赖）
├── hook.py                       可选：每轮自动注入命中的习惯
├── viz.py                        可视化面板渲染器
├── parity.py                     跨语言判据对拍
├── config.json                   阈值与门控
├── cases/cases.jsonl             已确认案例（本地生长，不入库）
├── cases/cases.example.jsonl     示例数据（格式参考）
├── pending/candidates.jsonl      待确认候选（本地生长，不入库）
├── profile/expression-profile.md 常驻档案（本地生长，不入库）
├── .session-state/               hook 会话状态（自动生成）
├── viz/index.html                可视化面板（自动生成，本地）
└── docs/index.html               demo 面板（示例数据，Pages 用它对外预览）
```

## 自动注入（可选）

`hook.py` 是 `UserPromptSubmit` hook：每轮把你这句话送去检索，命中的习惯自动进入
AI 的上下文 —— 不需要 AI「记得去读」，也不需要你手动挂载技能。

注入做了三重防膨胀：常驻规则**每会话只发一次** + 与 session 无关的时间窗保险 +
同一组长尾连续命中不重复。任何异常静默放行 —— hook 坏掉最多是不注入，不会阻塞你发言。

在 `~/.workbuddy/settings.json` 中配置（把路径换成你的实际位置）：

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ {
          "type": "command",
          "command": "\"<python.exe 绝对路径>\" \"<skill-dir>/hook.py\"",
          "timeout": 10
      } ] }
    ]
  }
}
```

用 `python su.py doctor` 可体检 hook 是否挂上、解释器路径是否漂移。

## 设计取舍

- **不做自动入库** —— 候选必须经人确认。无闸门的自学习会把噪音固化成规则。
- **不引入依赖** —— 中文检索用自写 n-gram 判据，不装分词器或向量库，保证单文件可跑。
- **面板数字必须可复现** —— `viz.py` 内嵌的 JS 判据与 `su.py` 逐位一致，`parity.py`
  用同一批探针交叉验证。仪表盘算得和终端不一样，就是装饰。

## License

MIT
