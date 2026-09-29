# 更新日志

## 1.1.7

### 修复

- **`sorted(set())` 抹掉传入顺序（试用反馈，最危险）**：`_collect_image_paths` 按字典序重排路径——`vision_compare` 的图1/图2 编号被调换，before/after 对比**静默给出反向结论还落库**（实测：[zz_red, aa_blue] 传入，模型收到的图1 是 aa_blue）。现保持传入顺序（`dict.fromkeys` 首现去重），目录展开时内部排序保确定性；`vision_read` 的 `result_ids` 顺序同样跟随传入。工具描述注明「传入顺序即编号顺序」。
- **相对路径静默丢弃**：`os.path.exists` 按 AstrBot 进程 CWD 解析（agent 猜不到），找不到就丢弃且不回显。现 `_collect_image_paths` 返回 `(found, missing)`：未找到/格式不支持的传入路径在报错中列出（并点明相对路径基准），成功响应带 `missing_paths`——静默丢图会让对比/批量结论基于不完整集合。
- **坏图错误归因**：0 字节/截断图片此前被判为「所有 VL 模型均调用失败」并 dump provider 链+密钥状态，且每张坏图都跑一遍完整降级链才认输、返回体还丢 `failed` 计数。新增 `ImageDecodeError` 分层（PIL 惰性加载，整个解码+压缩过程统一归因）：解码失败单独计 `decode_failed`，不重试、不降级、不归因模型，响应保留 `failed` 计数。
- **`result_ids` cap 10 静默截断**：超出 10 条时响应带 `unlisted_count` 并在 proposal 说明总数（试用反馈：15 张场景 5 张通过 next_call 永远够不着，且不告知）。

## 1.1.6

### 修复

- **批量命中的 `next_call` 落点（试用反馈）**：1.1.5 修了单图命中，批量场景仍退化成 `recent=N`——按时间倒序撞入库中无关的最新记录，命中集合被漏掉（实测：两条命中只查到一条，另一条是无关图）。现 `vision_read` 全程登记每张图的结果 id（命中给缓存 id、新读给新 id），批量含命中时 `next_call` 给 `{"result_ids": [...]}`（与传入顺序一致，cap 10）；纯新读批量维持 `recent`（新读的本就是最新 N 条）。配套：`vision_query` 新增 `result_ids` 参数（按 id 列表批量精确查询，返回顺序与传入一致，list 模式，只读不刷 hit_count）。
- **`result_id_hint` 三态文案**：原判据只看有无新读，混合批量下写「新结果」但范围起点是命中记录，自相矛盾。现纯新读=「新结果」/ 纯命中=「命中结果」/ 混合=「结果」。

### 测试

- 锁定 phash 等距候选的确定性：候选按 `read_at DESC` 遍历、等距严格小于不替换——同距离恒取最新记录，两次调用返回同一条（试用反馈中「两次命中不同」的根因是两次调用之间库里有新记录落库，行为本身确定；补测试钉死）。
- 新增：批量全命中/混合批量的 result_ids 分流、vision_query result_ids（顺序/跳过不存在 id/不计数/不构造分页）。

## 1.1.5

### 修复

- **缓存命中路径的 `next_call` 落点错误（实测复现的真 bug）**：sha256 精确命中与 phash 近似命中两条路径都在登记 `first_result_id` 之前就 return，导致返回结构的 `next_call` 退化成 `{"recent": 1}`——指向库中最新记录而非命中的记录（实测：命中的是三天前的图，agent 按提示查到的是一张完全无关的图，上下文被污染）。现命中分支登记所命中记录的 `result_id`，单图命中的 `next_call` 精确指向该记录；`result_id_hint` 文案区分「新结果/命中结果」。批量+全命中场景仍走 `recent` 兜底（1.1.6 起已由 `result_ids` 精确落点取代）。

### 文档

- README 补充 phash 已知盲区的实测数据：0.011% 面积的标记点改动距离为 0（误判同图），1.5% 面积色块距离 10（正确区分）；精度敏感场景指引 `force_reread` / `vision_compare`。实测同时再次验证 see_window 禁用 phash 的决定（库中存在不同时刻屏幕截图 phash 完全相同的冷冻证据）。

## 1.1.4

### 修复

- **`reasoning_effort` 参数形态错误（独立审查发现）**：此前嵌套进 `thinking` 对象发送（`{"thinking": {"reasoning_effort": ...}}`），而 DeepSeek 官方形态是**顶层参数** `reasoning_effort`（thinking 对象仅含 type；API 参考页 DOM 层级 + 思考模式指南 + SDK 示例三重印证）——嵌套形态会被服务端静默忽略，思考强度配置（low 省钱防截断 / none 关闭思考）全部落空。已改为顶层发送。
- **跨 provider `detail` 方言映射（`provider_detail`）**：白名单是两家并集，`original` 原样发给 OpenAI（只认 low/high/auto）会 400。现发送侧按 provider 映射：original→OpenAI 发 `high`（精细档，最接近保留原图语义）、high→DeepSeek 发 `original`（官方等价）。压缩档位仍按用户原始档位计算（original=保留原图），缓存键不受发送侧映射影响。
- **文档三处硬不一致**：ARCHITECTURE「四个工具」→ 五个（1.1.0 遗留，含数据流图补 compare/see_window 路径）；registry 的 output_path 描述「插件目录下 exports/」→ 实际写当前工作目录（README 原本就对）；SKILL.md 批量示例 next_call 同时给 result_id+recent → 两条路径互斥不可能产出此组合（与 CHANGELOG 1.0.6 条目自相矛盾），修为只给 recent。README 删除无正文的重复「推荐方式」标题；SKILL 补充 max_batch 上限提示。
- **`target_edge_for_model` 注释 800×800 残留**：与现行文档（1300×1300）及本文件头注释自相矛盾，更正。
- **`ImageTooLargeError` 不再短路降级链（独立审查发现）**：原注释「重试/降级结果都一样」在混合档位链下不成立——压缩档位按模型类型分档（非 DS 2048 / DS 1024），compare 场景 16 张 2048 档大图可破 40MB 内联预算，而 DS 备用的 1024 档本可通过，首次异常即返回把 fallback 掐死。现与 OutputTruncatedError 同语义：同 provider（同档位）重试无意义，但降级到更小档位的模型可能通过。
- **`max_retries`/`concurrency` 整数归一**：与 `_safe_timeout` 同一模式加 `_safe_int`——手编 config.json 写入 "abc"/null 时，下游 `int()` 穿透成不透明的「工具执行失败」，无法定位。provider 链继承与 vl_model 回退分支均归一。
- **`reasoning_effort` 别名补 `ultra→max`**：思考模式指南映射表有此行（API 参考页未列）。客户端映射后发送值恒在合法枚举内，比透传 ultra（遇严格校验有 400 风险）更稳。

### 测试

- conftest 增加 autouse fixture 每测试后重置 tool_config 全局状态（机制保障，不靠纪律维持）；export 默认路径测试清理 cwd 残留文件。
- 新增覆盖：`partial` 状态（部分成功部分失败）、max_batch 等值放行边界、ImageTooLargeError 两侧（read 不重试 / compare 提案）、截图清理边界（恰好 keep 张、keep=0）、reasoning_effort=none + json_mode 组合、provider_detail 映射。

## 1.1.3

### 新增

- **`reasoning_effort` 思考强度配置（DeepSeek 官方参数，默认 `low`）**：仅对 deepseek-flash / v4fve 附加 `thinking` 字段（其他 provider 不识别会 400，不附加）。读图是感知任务，`low` 足够——思维链 token 大减，截断风险、费用、延迟同步下降；`none` 完全关闭思考（此时 max_tokens 基线不抬升，`high`/`max` 可用）。官方别名映射（minimal→low、medium/xhigh→high），非法值回退 low。provider 降级链与 detail 同一模式继承全局配置。
- **截断日志带 `usage`**：`finish_reason=length` 的告警与 `OutputTruncatedError` 现在携带 `completion_tokens` / `reasoning_tokens`——思维链吃了多少额度一目了然，截断排障的关键证据。

### 修复（对齐 DeepSeek/OpenAI 官方行为）

- **`detail=low` 客户端对齐 512**：两家服务商 low 档都是缩到 512×512，此前客户端仍压 1024/2048——更大输入无收益只费带宽，现对齐。
- **DeepSeek 的 `detail=high` 视同 `original`**：官方文档明确 high 等价 original（保留原图），此前走压缩档名不副实；现 DeepSeek 系 high 跳过客户端降采样（OpenAI 的 high 是自带缩放的精细档，仍压 2048 对齐）。

## 1.1.2

### 新增

- **批量读图账单保险丝 `max_batch`（默认 2000）**：误传大目录（如整个盘符）时超过上限直接报错并提示分批——批量读图按张调用 VL 模型计费，此前无任何失控防护。支持 WebUI 配置；非法值/0/负数回退默认。
- **重试/降级链的编码复用**：`read_image` / `read_images` 新增 `image_url(s)` 预编码参数，vision_read / vision_compare 在 (retries+1)×链长 的调用循环内按压缩档位复用同一编码结果——同一张图的压缩+base64 只做一次（多图对比场景的重复压缩浪费不再被图片数放大）。
- **see_window 截图滚动清理**：`data/temp/tool_images/` 只保留最近 50 张（此前永久累积，磁盘泄漏）；清理失败静默，不影响截图主流程。

### 修复

- **`hit_count` 语义纯净**：`get_by_result_id` 查看不再 +1——「查询查看」此前被记入「缓存命中」，把 search 的 `hit_count DESC` 排序与 vision_query 展示的命中数刷得失真。现 hit_count 只统计真缓存命中（find_cached / find_cached_by_phash）。
- **vision_query full 模式 text 截断 2000 → 4000**：与 vision_compare 直返上限对齐，同一记录在两个入口完整度一致。
- **`_conf_schema.json` 运行时改写减噪**：options/labels 与已有值相同时不再触碰文件（此前每次启动都写，provider 列表不变也污染 git 工作区）；写入改为临时文件 + `os.replace` 原子替换（避免崩溃把 schema 截断成半个 JSON）。首次运行或 provider 列表变化时仍会写入——这是 AstrBot 下拉框注入的机制限制，无法根除。

### 文档

- README 补充：GIF 动图只读取第一帧（动画内容不会被完整理解）；`max_batch` 配置项说明。

## 1.1.1

### 修复

- **DeepSeek 思考模式「返回思考内容且截断」**：根因三连——(1) `read_image` 硬编码 `max_tokens=4096`，而 DeepSeek 思考模式默认开启（`reasoning_effort=high`），**思维链与答案共享 max_tokens 额度**（官方未设置时思考模式默认 64K），思考链经常吃掉全部 4096 额度；(2) 代码从不检查 `finish_reason`，截断毫无信号；(3) 1.0.5 的 `reasoning_content` 回退无条件生效——`content` 空时把**被截断的思维链**当答案返回并落库。修复：`finish_reason=length`（含 content 非空的半截 JSON）时放大额度单次重试（2 倍、封顶 16384，DeepSeek 上限 384K）；放大后仍截断抛 `OutputTruncatedError` 并落为 failed（**截断内容不再落库**）；`reasoning_content` 回退收紧到仅非截断场景（兼容某些网关）。
- **思考型模型 `max_tokens` 基线抬升**：`is_v4fve` 命中的模型基线 `max(配置值, 8192)`，从源头减少截断触发。
- **`is_v4fve` 适配现行模型名 `deepseek-flash`**：旧名 `deepseek-v4-flash-vision-exp` 已下线、请求由 flash 承接，但此前只匹配旧名——配置新名的用户丢失 1024 压缩档、`response_format` JSON Output 与基线抬升全部优化。同时更正注释中过时的服务端缩放参数（800×800/384 token → 1300×1300/1024 token，据现行官方文档）。
- **截断错误的降级语义**：`OutputTruncatedError` 在同一 provider 上不重试（额度已内部放大），但会降级到下一个 provider——截断是模型相关的，换非思考型模型可能成功（vision_read / vision_compare 一致）。
- **provider `timeout` 健壮性**：AstrBot provider_config 中 timeout 为字符串/None/非法值时，下游 `float()` / httpx 直接异常；现统一 `_safe_timeout` 归一化（含 vl_model 手动配置回退分支），非法值告警并回退 120s。

## 1.1.0

### 新增

- **`vision_compare` 工具：多图对比询问**。整组图片在**同一次 VL 请求**中发送（DeepSeek 多图契约：多个 image_url 块放同一条 user 消息，每图独立计费 ≤1024 token）——模型同时看到全部图才能做跨图判断，优于逐张读取后由 LLM 自己拼结论。图片间插入「图1/图2（文件名）」文本标记，模型回答可引用具体图片。结论**直接返回**（peek + text≤4000 字 + tags）并落库（`result_id` 前缀 `cmp_`、`result_json.kind="compare"` 含 members 明细），可用 vision_query / vision_export 复查。
- **组指纹缓存**：对比结果的缓存键为成员 sha256 排序后联合哈希（`grp_` 前缀），与 paths 顺序、文件名无关；同组 + 同模型 + 同问题 + 同 detail 才命中，换问题重新对比。相同内容的成员自动去重。phash 近似兑底不参与图组（组记录落库 `phash=""`，被双侧纯色守卫天然排除）。追问（`previous_result_id`）仅当指向同一组（组指纹相同）时注入上文，防跨组污染。
- **多图护栏**：单次 2-16 张（`MAX_COMPARE_IMAGES`，16 张 ≈ 16K token 与请求体的平衡点）；内联 base64 总量 40 MiB 预算（`MAX_INLINE_IMAGES_B64`，DeepSeek 请求体上限 48 MiB 留余量），超预算抛 `ImageTooLargeError`（不重试不降级，与单图超限同一语义）；空内容 JSON 不落库（与 vision_read 同一防护）。
- **`_vl_client` 发送路径收敛**：抽出 `_post_chat`（user 消息 content 块数组 → OpenAI 兼容端点，`response_format`/reasoning_content 回退逻辑单点化），单图 `read_image` 与新增多图 `read_images` 共用；`read_image` 签名与行为不变。

## 1.0.6

### 新增

- **提示词 v3（精简）**：默认读图目标从「为视障人士描述」改为「为智能体提供事实性、可检索的结构化档案」。刻意精简至 ~200 字（v2 曾达 ~700 字）：只保留解析契约（JSON schema）与三条质量护栏（文字逐字原文 / 只依据可见内容、看不清明说 / 中文输出），**不设类型枚举清单**——避免模型被套模板、忽略清单外的细节，描述重点的判断权交给模型。追问模式补充看图素养规则（不编造、原文引用、无法确认明说）。同时降低高并发批量读图的 prompt token 开销。
- **字段重命名 `summary` → `peek`（一句话预览）**，查询模式 `peek` → `list`（避免与字段撞名）：DB 列自动迁移（`RENAME COLUMN`，老数据保留）、VL 模型 JSON schema、query/export 输出、工具描述全链路同步；旧缓存的 `summary` 键解析兜底兼容。**注意**：导出 CSV 列名随之变化，依赖旧列名的外部脚本需适配。
- **结构化读图结果**：读图 prompt 要求模型返回 JSON（`peek` 一句话预览/回答 + `text` 完整内容 + `tags` 内容标签），插件容错解析（容忍代码围栏与杂音，失败回退旧「首行预览」行为，读图永不因此失败）。`tags` 字段此前恒为空，现真正填充，搜索质量提升。v4fve 额外附加官方 `response_format`（DeepSeek JSON Output）。
- **DeepSeek v4fve 官方文档适配**：接入模型为 `deepseek-v4-flash-vision-exp` 时自动触发——压缩长边 2048 → 1024（对齐其服务端 ~800×800 缩放与 384 token/张上限，上传体积省 ~75%）；新增 `detail` 配置项（low/auto/original）透传给支持 detail 的模型。
- **phash 感知哈希近似缓存命中**：sha256 精确未命中后，自动用已落库的 phash 做近似匹配（同 model + 同 question + 同 detail，汉明距离 ≤ 5）。缩尺/重压缩的同一张图不再重复调用 VL 模型，兑现「甚至被压缩过的同图片也命中缓存」的承诺。近似命中返回 `matched_by` / `phash_distance`，且 `vision_read` 响应透传 `cached_via_phash` 计数与提示。
- `vision_query` list 模式结果新增 `question` 字段：同一张图的多条不同问题记录可区分（每个 question 独立成行，不会互相覆盖）。

### 修复

- **phash 纯色守卫覆盖候选侧**：此前只拦查询侧，库中已落库的纯色记录（phash 全 0/全 1）仍会作为候选被正常图片误命中；现两侧都过滤。
- **追问上下文注入位置修正**：`previous_context` 移到 JSON 输出要求之前（此前追加在「不要输出 JSON 以外的任何内容」之后，模型最后看到的不是格式指令）；同时加上限（peek 200 字 / text 1000 字）防超长上文。
- **JSON 骨架占位符照抄防护**：弱模型可能把 prompt 示例中的占位符原文（如「一句话直接回答」）照抄落库，解析时识别并剔除（含 tags 占位符），按兜底路径处理。
- **`vision_query` 无参数分支 mode 残留修正**：else 分支误留 `"peek"`，统一为 `"list"`。
- **批量读图的 `next_call` 不再夹带 `result_id`**：此前批量场景同时给 `recent` + `result_id`，而 `vision_query` 中 `result_id` 优先级最高——agent 被直接带去第一张图的全文，其余图片的预览被跳过。现批量只给 `recent`（list 模式浏览），单图才直接给 `result_id`（full 模式）。
- **空内容 JSON 不落库**：模型返回纯 tags / 空对象等解析后无实质内容时，视为本次读图失败（计入 failed），不落库——否则空记录会永久占用缓存键，后续命中返回空内容且无失败信号。
- **detail 纳入缓存键**：detail 是影响模型输入（从而影响输出）的配置维度，此前改 detail 配置后同图同问题会静默命中旧档位缓存。`''` 与 `'auto'` 语义等价互相兼容，老记录不受影响。
- **`detail=original` 不再被客户端压缩架空**：original 语义为保留原图，现跳过客户端降采样（此前 v4fve 下仍被压到 1024，see_window 读屏小字场景最受其害）。
- **detail 配置经 provider 降级链继承**：`_provider_to_vl_config` 与 concurrency/max_retries 同一模式从全局 vl_model 继承 detail（此前走下拉框链路配 detail 不生效）。
- **detail 枚举归一化**：strip + 小写 + 白名单校验，非法值告警并回退 auto（此前乱配会原样发给 API 导致 400；跨 provider 语义差异：OpenAI 认 high / DeepSeek 认 original）。
- **see_window 禁用 phash 近似兜底**：实测同一 IDE 窗口代码全换后 phash 距离仅 0-4（≤5 必误判），近似命中会返回过期屏幕描述；现 see_window 走 `allow_phash=False`（sha256 精确命中保留）。
- **纯色/低信息图片跳过 phash 兜底**：此类 phash 趋同（白/红/蓝/灰两两距离 0-1），置 1 比特 <4 或 >60 直接不匹配。
- **phash 兜底改两阶段查询**：先轻列扫描候选再取最佳行，避免把 result_json/text 重列全拉内存并持锁。
- **`_ensure_conn` 重连补全初始化**：close 后重连不再丢失 `check_same_thread=False`、PRAGMA（synchronous/busy_timeout）与 SCHEMA/迁移——统一走 `_connect()` 复用（此前潜伏的跨线程 ProgrammingError 排雷）。
- **vision_query / vision_export 的 DB 调用补 offload**：与 vision_read 一致移出事件循环。
- **压缩后超限的错误不再重试/降级**：新增 `ImageTooLargeError`（ValueError 子类），超限即直接失败——此前会在全链路上重试 (retries+1)×链长 次且每次重新压缩。
- **插件专用线程池**：`run_sync` 从共享默认 executor 改为 `irmia_vision` 命名线程池（≤8 线程），批量读图不再饿死宿主的其他 offload 任务。
- **see_window 截图缓存永远 miss**：截图文件名带微秒时间戳，而缓存键含 filename 维度，导致同一画面每次截图都重复调用 VL 模型。缓存键改为 `(sha256, model_id, question)` 纯内容寻址，与文件名无关；filename 仍落库供查询展示。
- **20MB 大小限制误杀大图**：原检查按原始文件字节数在压缩前拦截，25MB 的照片压缩后仅 1-2MB 却被拒绝。大小检查移到 `_compress_image` 压缩结果上，不再限制原始文件；同时删除 `vision_read` 中的重复前置检查。
- **批量读图冻结 AstrBot 事件循环**：`sha256_of_file` / `find_cached` / `insert` / `get_by_result_id` / `_compute_phash` 及 VL 调用内的 `encode_image` 全部是事件循环上的同步 I/O，实测 100 张图造成 5.6 秒连续冻结。现统一通过 `run_sync()` offload 到线程池（同步修复 `run_sync` 弃用的 `get_event_loop` 并真正启用它），同场景最长停滞降至 ~17ms。`SQLiteVisionStore` 相应增加 `threading.RLock` 串行化跨线程访问。

## 1.0.5

### 新增

- **`see_window` 工具：快速查看电脑屏幕或指定窗口**。截取整个屏幕或按窗口标题关键词（支持 `vs code` / `qq` / `wechat` 等常见缩写）截取指定窗口画面，用 VL 模型分析并落库。默认提示词偏向「干活」——搞清楚用户在干什么、屏幕上发生了什么。找不到窗口时返回当前可见窗口列表。仅支持 Windows（非 Windows 平台返回明确错误提示）。截图、读图、缓存、降级链、落库全部复用 `vision_read` 管线，不重复造轮子。

### 修复

- **DeepSeek 推理型视觉模型（`deepseek-v4-flash-vision-exp`）支持**：该模型为思考模式，思维链走 `reasoning_content` 字段（与 `content` 同级）；当 `max_tokens` 不足时思考过程耗尽额度，`content` 为空。修复：`content` 为空时回退到 `reasoning_content`，默认 `max_tokens` 2048 → 4096。已实测确认该模型视觉 API 完全可用。
- **降级链被「空内容」误判为成功而中断**：首选模型返回空 content 时，旧逻辑误判为成功并 `break` 退出整个循环，导致后续降级模型根本没有机会执行，最终误报「所有 VL 模型均未配置 api_key」。现在空内容视为该模型失败，继续降级下一个模型；错误文案同步修正为「所有 VL 模型均返回空内容」。

## 1.0.4

### 修复

- **插件加载早于 provider 初始化时读图失败**：`get_all_providers()` 返回空时从磁盘 `cmd_config.json` 兜底读取 provider 配置，保证降级链能解析出带 api_key 的模型。
- **修复配置文件 UTF-8 BOM 导致兜底解析失败**：磁盘读取改用 `utf-8-sig` 兼容 BOM。
- **`vl_model` 无 `api_key` 时不再进入降级链**：避免空 key 配置导致读图全部失败。
- **失败诊断增强**：全部模型调用失败时返回降级链详情与具体错误信息，方便排查。
- **缓存命中不再依赖 VL 模型配置**：未配置可用模型时仍可命中已读图片的缓存（`find_cached` 对空 `model_id` 放宽为任意模型匹配）。

## 1.0.3

### 新增

- **自动读取 AstrBot 已保存模型**：插件启动时通过 `context.get_all_providers()` 自动获取所有 CHAT_COMPLETION 类型模型，无需手动输入 base_url / api_key。
- **WebUI 三下拉框选择模型**：`vl_provider_1`（首选）、`vl_provider_2`（次选）、`vl_provider_3`（再次选），插件加载时自动拉取可用模型列表写入下拉选项。
- **多模型降级链**：读图时按首选 → 次选 → 再次选顺序逐个尝试，每个模型独立重试，全部失败才报错。
- **零配置开箱即用**：三个下拉框全部留空时，自动使用 AstrBot 中所有已保存的模型。
- `config.py` 新增 `resolve_provider_chain()` 解析降级链，`set_providers()` / `get_providers()` 管理 provider 列表。
- `_vl_client.py` 的 `read_image()` 新增 `vl_config` 参数，支持显式传入 VL 配置。
- `vl_provider_ids` 高级字段保留，手动填写后覆盖下拉框选择。

### 修复

- fallback provider 的 timeout 不再被 primary 的共享 client 截断（取链中最大值）。
- retry sleep 移到 semaphore 外，避免浪费并发槽位。
- `model_id` 缓存标记改为实际成功的 provider，而非始终取 chain[0]。
- provider 的 `key` 字段为字符串时也能正确提取（不再假设一定是列表）。

### 变更

- `_conf_schema.json` 新增 `vl_provider_1/2/3` 三个下拉框字段。
- `vl_model` 手动配置降级为最终回退方案。

## 1.0.2

### 新增

- `vision_export`：批量导出已读图结果为 JSON/CSV，方便脚本批量处理。
- 异步并发 VL 读图：通过 `asyncio.Semaphore` 控制并发，支持 `concurrency` / `timeout` / `max_retries` 配置。
- 大图片自动压缩：长边超过 2048 时按比例缩放，减少上传体积和 token 消耗。
- 自适应并发：未配置 `concurrency` 时，根据 `timeout` 自动估算（最高 200）。
- 存储后端抽象：新增 `tools/_store.py` 的 `VisionStore` 基类，默认 `SQLiteVisionStore`，预留 PostgreSQL / MongoDB 扩展路径。
- 工具描述改为 LLM 视角，明确由 LLM 自主决定何时调用。
- `vision_query` 增加 peek / full 双模式：列表查询只返回 `result_id`/`filename`/`summary`，`result_id` 精确查询才返回完整 `path`/`text`/`tags`。

### 修复

- 单图场景下 `vision_read` 的 `next_call` 直接返回 `result_id`。
- 明确 `previous_result_id` 仅作用于与之前同一张图片（sha256 相同）。
- 为 `.bmp` 图片补充 `image/bmp` MIME 类型。

## 1.0.0

### 新增

- 插件初始化：AstrBot 插件入口、配置加载、数据库初始化。
- `vision_read`：批量读取图片，支持文件/文件夹/多路径，命中缓存则跳过 VL 调用。
- `vision_query`：按 `result_id`、`filename`、`path`、`query`、`recent` 查询已读图结果，支持分页。
- 缓存策略：按 `(sha256, filename, model_id, question)` 缓存，换文件名/模型/问题会重新读图。
- 追问模式：通过 `previous_result_id` 基于已有理解继续提问。
- 强制重读：`force_reread` 忽略缓存。
- 默认中文图片描述 prompt，客观、生动、逐字提取可见文字。
- 单张图片 20MB 大小限制。
- `skills/vision-read/SKILL.md` 提供 LLM 工作流提示。
